"""🔑 导入端点：`POST /kb/import/chat`（D47）+ `POST /kb/{kb_id}/import/dir`（D48）。

## 为什么这两个端点是"入队 + 202"而不是同步做完

- **87k 条聊天消息**（16MB）→ 1740 个文档 → 每个还要跑一次摄取（embedding）。
  同步做会把 API 进程占住几分钟（D50 的教训：API 进程不能被重活占死）。
- 同步做还**没法报告进度**：契约要求 `GET /jobs/{id}` 能轮询
  `payload.processed_msgs`，那只有任务才有。

## 🔑 路径安全（D92）

两个端点收的都是**服务器上的路径**。没有白名单的话，任何租户都能
`{"path": "/etc"}` 或 `{"path": "../../另一个租户的数据"}` 把别人的文件
读进自己的知识库 —— 那不是"路径穿越的小瑕疵"，是跨租户数据泄漏。

所以 `import_roots()`（配置项 `IMPORT_ROOTS`，默认 `<data_dir>/imports`）
是**硬门禁**，在请求时就拒绝，并如实告诉用户允许的目录在哪。

## 库类型必须匹配（不是可选校验）

- `import/chat` → 目标库 `source_kind` 必须是 `chat`
- `import/dir` → 目标库 `source_kind` 必须是 `photo`

理由：`documents.doc_source` 是**有约束的枚举**
（`upload` / `chat_import` / `photo_dir`）。把聊天记录导进一个 `file` 库
会让"这个库里有什么"这件事变得不可推理，而 `source_kind` 又是
**检索路由的输入**（`routing.py` 的 `prefer_source_kinds`）——
类型错配会让路由按错误的类型去过滤。

## 估算只给"量级"，handler 自己再扫一遍

请求时扫一次目录/数一遍行数只是为了回 `estimated_files` /
`estimated_docs`（让用户知道要等多久）。handler 会**重新扫**：
请求与执行之间文件可能变了，而 payload 里塞几千个路径既臃肿又可能过期。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...common.audit import ACTION_UPLOAD, write_audit
from ...common.auth import assert_kb_access
from ...common.deps import CurrentUser, DbSession
from ...common.errors import InvalidRequest
from ...common.jobs import KIND_IMPORT_CHAT, KIND_IMPORT_DIR, enqueue_async
from ...common.models import KnowledgeBase
from ...common.schemas import JobOut
from ...ingest.importers import (
    DEFAULT_MESSAGES_PER_DOC,
    count_messages,
    import_roots,
    resolve_import_path,
    scan_dir,
)
from ...ingest.parsers import IMAGE_SUFFIXES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["import"])


# ============================================================
# 响应模型
# ============================================================


class ImportChatResp(BaseModel):
    """`POST /kb/import/chat`（契约 §8）。"""

    job_id: int
    #: 估算的文档数（`total_msgs // messages_per_doc` 上取整）。
    #: ⚠️ 是**估算**：handler 重新读文件时行数可能变了。
    estimated_docs: int
    total_msgs: int
    messages_per_doc: int


class ImportDirResp(BaseModel):
    """`POST /kb/{kb_id}/import/dir`（契约 §9）。"""

    job_id: int
    estimated_files: int
    recursive: bool


# ============================================================
# 请求体
# ============================================================


class ImportChatReq(BaseModel):
    kb_id: int
    source_path: str = Field(
        description="服务器上的 jsonl 路径，必须在 IMPORT_ROOTS 白名单内（D92）"
    )
    messages_per_doc: int = Field(
        default=DEFAULT_MESSAGES_PER_DOC, ge=1, le=1000,
        description="每个文档切多少条消息（D41 = 50）",
    )


class ImportDirReq(BaseModel):
    path: str = Field(
        description="服务器上的目录，必须在 IMPORT_ROOTS 白名单内（D92）"
    )
    recursive: bool = True


# ============================================================
# 端点
# ============================================================


@router.post(
    "/kb/import/chat",
    response_model=ImportChatResp,
    status_code=202,
    summary="导入聊天记录（D47：服务端读 jsonl，不走 HTTP 上传）",
)
async def import_chat(
    body: ImportChatReq, ctx: CurrentUser, session: DbSession
) -> ImportChatResp:
    await assert_kb_access(session, ctx, body.kb_id)
    _require_source_kind(await _get_kb(session, body.kb_id), "chat")

    src = resolve_import_path(body.source_path, expect="file")
    if src.suffix.lower() != ".jsonl":
        # 不是"不支持就默默当文本读" —— 那会把一个目录或一个 md
        # 当聊天记录导进去，产生一堆空文档
        raise InvalidRequest(f"{src.name} 不是 .jsonl 文件")

    total = count_messages(src)
    if total == 0:
        raise InvalidRequest(f"{src.name} 没有可用消息行（空文件或全是空行）")

    job = await enqueue_async(
        session,
        KIND_IMPORT_CHAT,
        kb_id=body.kb_id,
        payload={
            "kb_id": body.kb_id,
            "source_path": str(src),
            "messages_per_doc": body.messages_per_doc,
            "total_msgs": total,
            # 🔑 记下发起人：导入出来的文档也该有 provenance
            #   （"这份聊天记录是我在 10-03 导入的"）。
            #   没有它就只能在文档列表里看到一堆匿名导入的记录。
            "uploaded_by": ctx.user_id,
        },
    )
    await session.commit()
    await write_audit(
        session,
        action=ACTION_UPLOAD,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[body.kb_id],
        detail={"op": "import_chat", "source": str(src), "total_msgs": total,
                "job_id": job.id},
    )
    logger.info(
        "导入聊天入队：kb=%s %s（%s 条消息 → 约 %s 个文档）",
        body.kb_id, src.name, total,
        -(-total // body.messages_per_doc),
    )
    return ImportChatResp(
        job_id=job.id,
        estimated_docs=-(-total // body.messages_per_doc),
        total_msgs=total,
        messages_per_doc=body.messages_per_doc,
    )


@router.post(
    "/kb/{kb_id}/import/dir",
    response_model=ImportDirResp,
    status_code=202,
    summary="从服务器目录批量导入图片（D48）",
)
async def import_dir(
    kb_id: int, body: ImportDirReq, ctx: CurrentUser, session: DbSession
) -> ImportDirResp:
    await assert_kb_access(session, ctx, kb_id)
    _require_source_kind(await _get_kb(session, kb_id), "photo")

    src = resolve_import_path(body.path, expect="dir")
    files = scan_dir(src, recursive=body.recursive, suffixes=IMAGE_SUFFIXES)
    if not files:
        # 报"没找到"而不是排个空任务：空任务会显示"导入完成、0 个文件"，
        # 用户会以为目录已经扫过了（§3.3 kind 的登记纪律：空结果要说清原因）
        raise InvalidRequest(
            f"{src} 下没有找到图片（支持 "
            f"{', '.join(sorted(IMAGE_SUFFIXES))}"
            f"{'，含子目录' if body.recursive else '，仅本层'}）"
        )

    job = await enqueue_async(
        session,
        KIND_IMPORT_DIR,
        kb_id=kb_id,
        payload={
            "kb_id": kb_id,
            "path": str(src),
            "recursive": body.recursive,
            "total_files": len(files),
            "uploaded_by": ctx.user_id,
        },
    )
    await session.commit()
    await write_audit(
        session,
        action=ACTION_UPLOAD,
        user_id=ctx.user_id,
        api_key_id=ctx.api_key_id,
        kb_ids=[kb_id],
        detail={"op": "import_dir", "path": str(src), "files": len(files),
                "job_id": job.id},
    )
    logger.info("目录导入入队：kb=%s %s（%s 个文件）", kb_id, src, len(files))
    return ImportDirResp(
        job_id=job.id, estimated_files=len(files), recursive=body.recursive
    )


async def _get_kb(session: DbSession, kb_id: int) -> KnowledgeBase:
    """取 KB 行（**可见性判定已经由 `assert_kb_access` 做过**）。

    ⚠️ 这里不再判权限 —— §0.6：租户过滤只有 `auth.py` 那两个入口。
    先断言访问、再取行，顺序反了就会出现"没鉴权就查库"。
    """
    kb = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    if kb is None:                      # 理论上断言已挡住
        raise InvalidRequest(f"知识库 {kb_id} 不存在")
    return kb


def _require_source_kind(kb: KnowledgeBase, want: str) -> None:
    """目标库的 `source_kind` 必须匹配（见模块 docstring 的三条理由）。

    ⚠️ 报错要给出**该怎么做**（"先建一个 source_kind=photo 的库"），
    只说"类型不对"的话用户不知道下一步。
    """
    if kb.source_kind == want:
        return
    raise InvalidRequest(
        f"这个导入端点只能用于 source_kind={want} 的知识库"
        f"（当前是 {kb.source_kind}）：documents.doc_source 是受约束的枚举"
        f"（upload/chat_import/photo_dir），而且 source_kind 是检索路由的输入。"
        f"请新建一个 source_kind={want} 的库，或改用对应的上传端点。"
    )


@router.get(
    "/kb/import/roots",
    response_model=dict,
    summary="允许导入的目录（白名单，D92）",
)
async def list_import_roots(ctx: CurrentUser) -> dict:
    """把白名单列出来。

    ⚠️ 这个端点**刻意不需要 `workspace_id` 之外的任何上下文**：
    它返回的是**服务器配置**，不是租户数据。只暴露 `IMPORT_ROOTS`
    本身 —— 不含任何文件列表，所以不构成信息泄漏（真正的门禁在
    `resolve_import_path`）。
    """
    roots = [str(r) for r in import_roots()]
    return {"roots": roots, "env": "IMPORT_ROOTS"}


__all__ = ["router", "ImportChatReq", "ImportDirReq", "JobOut"]