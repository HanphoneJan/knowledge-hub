"""文档端点。契约见 `docs/DESIGN-API.md` 第 4 节。

## 校验顺序（D38：能降级就降级）

```
上传时  格式白名单 → 415     同步拒绝
        >50MB      → 413     同步拒绝（D30）
        >20 个文件 → 413     同步拒绝（D45）
        加密 PDF   → **不拒绝**，入队后转视觉路径
入队后  >2000 页   → 422     doc.status=failed（D30）
        扫描件 >50 → 422     doc.status=failed（D37）
        完全损坏   → 400     status_msg 记原因
```

## 依赖注入写法

路由函数用 `Annotated[X, Depends(...)]`（即 `CurrentUser` / `DbSession`），
**不要给它们写默认值**。写了默认值 FastAPI 就不会注入，会拿到 `None`。

多文件上传用 `Annotated[list[UploadFile], File(...)]` 而不是
`files: list[UploadFile] = File(...)` —— 后者与"无默认值的注入参数"
在签名顺序上冲突（Python 语法：默认参数不能排在无默认参数前面）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select, update as sa_update

from ...common.auth import assert_kb_access, assert_kb_write, can_write_kb
from ...common.deps import CurrentUser, DbSession
from ...common.errors import (
    FileTooLarge,
    InvalidRequest,
    ResourceNotFound,
    UnsupportedFormat,
    ValidationFailed,
)
from ...common.jobs import KIND_INGEST, enqueue
from ...common.models import Document, Job, KnowledgeBase
from ...common.pagination import run_paginated
from ...common.schemas import (
    MAX_FILES_PER_REQUEST,
    ChunkOut,
    DocumentDetail,
    DocumentOut,
    PurgeResp,
    UploadResp,
)
from ...common.storage import (
    blob_path,
    preview_path,
    register_blob_async,
    store_bytes,
    thumb_path,
)
from ...ingest.parsers import IMAGE_SUFFIXES, SUPPORTED_SUFFIXES
from ...ingest.preview import render_page
from ...ingest.purge import PurgeStats, purge_documents_async, purgeable_stmt
from ...ingest.versions import current_by_name, next_version_id_async, supersede
from ..routers._shared import (
    cancel_jobs_for_doc_async,
    enqueue_async,
    guess_mime,
    load_document,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

FilesArg = Annotated[list[UploadFile], File(description="最多 20 个，每个 ≤50MB")]


# ============================================================
# 上传
# ============================================================


@router.post(
    "/kb/{kb_id}/documents",
    response_model=UploadResp,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（202 后异步摄取）",
)
async def upload_documents(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    files: FilesArg,
) -> UploadResp:
    """上传一个或多个文件（D45：单请求最多 20 个）。

    返回 **202** 而不是 201：摄取是异步的（colSmol 15s/图），
    前端靠轮询 `GET /kb/{kb_id}/documents` 看状态。
    """
    await assert_kb_write(session, ctx, kb_id)

    if len(files) > MAX_FILES_PER_REQUEST:
        raise FileTooLarge(
            f"单请求最多 {MAX_FILES_PER_REQUEST} 个文件（D45），收到 {len(files)} 个"
        )

    batch_id = f"bat_{uuid.uuid4().hex[:16]}"
    created: list[DocumentOut] = []
    for uf in files:
        created.append(await _ingest_one(uf, kb_id, ctx, session))
    return UploadResp(batch_id=batch_id, documents=created)


async def _ingest_one(uf: UploadFile, kb_id: int, ctx, session) -> DocumentOut:
    """处理一个上传文件 → 建 Document 行 + 入队摄取。"""
    name = uf.filename or "unnamed"
    suffix = Path(name).suffix.lower()

    # ---- 同步校验：能在上传时发现的，别拖到 worker ----
    if suffix not in SUPPORTED_SUFFIXES and suffix not in IMAGE_SUFFIXES:
        raise UnsupportedFormat(
            f"不支持的格式 {suffix or '(无扩展名)'}；支持 "
            f"{', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    data = await uf.read()
    if not data:
        raise ValidationFailed(f"{name} 是空文件")
    limit = 50 * 1024 * 1024
    if len(data) > limit:
        raise FileTooLarge(
            f"{name} 有 {len(data) / 1024 / 1024:.1f}MB，超过上限 50MB（D30）"
        )

    # D166 第一层：扩展名 vs 实际内容（保守嗅探，识别不出就放行）。
    # ⚠️ 只在**确凿**时拒绝（见 `validate.format_mismatch` 的保守原则）——
    #   把"内容看着不像该格式但确实合法"的文件挡在门外比放进坏数据更糟。
    from ...ingest.validate import format_mismatch

    mismatch = format_mismatch(suffix, data)
    if mismatch:
        raise UnsupportedFormat(f"{name}：{mismatch}")

    # 内容寻址存储：同名同内容自动去重，返回 sha256
    sha, size = store_bytes(data)
    # ⚠️ 必须登记 blob 行 —— `documents.blob_sha` 有外键指向 `blobs.sha256`。
    # 漏了这一步会直接报 `documents_blob_sha_fkey` 外键错误（踩过）。
    # ⚠️ 一定要用 async 版本：在 AsyncSession 上调同步的 execute 不报错，
    #    只是返回一个从未被 await 的 coroutine，INSERT 根本没发出去。
    await register_blob_async(session, sha, size)

    # 覆盖式更新（D28）：同名旧版本进回收站
    # 🔑 判据与动作来自 `ingest/versions.py`（唯一实现）—— 与目录导入路径
    #   **共用同一份**。第一版这里写死 `scalar_one_or_none()`，而导入路径
    #   允许出现两个同名 current 文档（那边没做 supersede）→
    #   于是那个库之后每次上传同名文件都 `MultipleResultsFound` → **500**，
    #   且症状与原因隔了一周（"上周导过一次照片"）。
    #   现在用 `.first()`（脏数据取第一个并 warning），见 `find_current` 的说明。
    rows = (
        await session.execute(
            select(Document).where(*current_by_name(kb_id, name))
        )
    ).scalars().all()
    if len(rows) > 1:
        logger.warning(
            "库 %s 有 %d 个同名 current 文档（%s），取 id=%s 继续；请清理多余版本",
            kb_id, len(rows), name, rows[0].id,
        )
    if rows and supersede(rows[0]):
        logger.info("覆盖式更新：旧 doc=%s 进回收站", rows[0].id)

    doc = Document(
        kb_id=kb_id,
        file_name=name,
        content_hash=sha,
        blob_sha=sha,
        mime_type=uf.content_type or guess_mime(name),
        size_bytes=size,
        doc_source="upload",
        status="pending",
        uploaded_by=ctx.user_id,
        # D192：覆盖式更新的版本号（同名第 N 次上传就是第 N 版）。
        version_id=await next_version_id_async(session, kb_id, name),
    )
    session.add(doc)
    await session.flush()

    await enqueue_async(session, KIND_INGEST, doc_id=doc.id, kb_id=kb_id)
    await session.commit()
    return DocumentOut.model_validate(doc)


# ============================================================
# 列表 / 详情
# ============================================================



def _preview_source(doc) -> tuple[Path, bool]:
    """这份文档的"预览文件"：`(路径, 是不是转换版)`。

    D34 v1.1：PDF 与图片用**原件**；Office 用 LibreOffice 转出的
    `preview.pdf`（转换可能还没跑完 → 报 415 并说清去哪儿看任务状态）。

    🔑 **三个端点共用这一份**（`/preview`、`/pages/{n}`、带框预览）：
    各自判断一遍的话，总有一个端点会漏掉 Office，
    症状是"页面能看、下载打不开"这类说不清的分裂。
    """
    original = blob_path(doc.blob_sha)
    if doc.file_name.lower().endswith(".pdf") or _is_image(doc.file_name):
        return original, False
    pv = preview_path(doc.blob_sha)
    if pv.exists():
        return pv, True
    raise UnsupportedFormat(
        f"{doc.file_name} 还没有预览：Office 文档要先经 LibreOffice 转成 PDF，"
        f"转换任务还没跑完或失败了"
        f"（用 GET /jobs?doc_id={doc.id}&kind=pdf_preview 看状态；"
        f"没有 LibreOffice 的话装 libreoffice-writer/impress/calc）"
    )


def mime_type_for(file_name: str) -> str:
    """文件名 → Content-Type。

    ⚠️ 复用 `parsers.guess_mime`（`mimetypes`）而不是自己维护映射表 ——
    自己那份必然漏项，症状是浏览器把 PDF 当成下载而不是内联预览。
    """
    from ...ingest.parsers import guess_mime

    return guess_mime(Path(file_name)) or "application/octet-stream"


def _is_image(file_name: str) -> bool:
    """🔑 判据用 `IMAGE_SUFFIXES`，**不**再手写一份后缀元组（D140）。

    ⚠️ 这里原来硬编码了同样的 6 个后缀，而它被**三处**用
    （`_preview_source` 的原件判断、下载的 media_type、还有一个预览分支）。
    危害是"派发表加了新图片格式，这三处**悄悄不认**"——
    症状是"上传了、检索不到图、下载出来是 text/plain"，完全指不到格式清单。
    """
    return (file_name or "").lower().endswith(tuple(sorted(IMAGE_SUFFIXES)))


@router.get("/kb/{kb_id}/documents", response_model=list[DocumentOut], summary="文档列表")
async def list_documents(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    doc_status: Annotated[str | None, Query(alias="status")] = None,
    page: int = 1,
    page_size: int = 50,
) -> list[DocumentOut]:
    await assert_kb_access(session, ctx, kb_id)

    stmt = select(Document).where(
        Document.kb_id == kb_id,
        Document.is_current.is_(True),
        Document.deleted_at.is_(None),
    )
    if doc_status:
        stmt = stmt.where(Document.status == doc_status)
    rows, _ = await run_paginated(
        session, stmt.order_by(Document.created_at.desc()), page, page_size
    )
    return [DocumentOut.model_validate(r) for r in rows]


@router.get("/documents/{doc_id}", response_model=DocumentDetail, summary="文档详情")
async def get_document(doc_id: int, ctx: CurrentUser, session: DbSession) -> DocumentDetail:
    doc = await load_document(session, ctx, doc_id)
    return DocumentDetail(
        **DocumentOut.model_validate(doc).model_dump(),
        content_hash=doc.content_hash,
        preview_url=f"/api/v1/documents/{doc_id}/preview",
    )


@router.get(
    "/documents/{doc_id}/chunks",
    response_model=list[ChunkOut],
    summary="文档的 chunk 列表（含 L3 坐标）",
)
async def list_chunks(
    doc_id: int,
    ctx: CurrentUser,
    session: DbSession,
    page: int = 1,
    page_size: int = 100,
) -> list[ChunkOut]:
    """chunk 及其坐标 —— 前端"看切分质量"用。

    坐标格式见 `CONVENTIONS.md` §6：正文单框、表格多框带 row/col。
    """
    from ...common.models import Chunk

    await load_document(session, ctx, doc_id)
    rows, _ = await run_paginated(
        session,
        select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index),
        page,
        page_size,
    )
    return [ChunkOut.model_validate(r) for r in rows]


# ============================================================
# 预览（L3 前端的底图）
# ============================================================


@router.get(
    "/documents/{doc_id}/pages/{page_no}",
    summary="渲染单页为图片（L3 底图）",
    response_class=Response,
)
async def get_page_image(
    doc_id: int,
    page_no: int,
    ctx: CurrentUser,
    session: DbSession,
    width: int = 1200,
) -> Response:
    """把指定页渲染成 JPEG。

    坐标已归一化（D33），所以前端可以用任意 `width` 调这个端点，
    再按 `width / 实际图宽` 换算框的位置 —— 不需要重新请求坐标。
    """
    doc = await load_document(session, ctx, doc_id)
    path = blob_path(doc.blob_sha)
    if not path.exists():
        raise ResourceNotFound(detail={"doc_id": doc_id, "reason": "blob 缺失"})
    # 🔑 判格式看 `documents.file_name`，**不能看 `path.suffix`** ——
    # blob 路径是 `{sha}/original`，扩展名是空的（内容寻址不保留原文件名）。
    # 踩过：所有 PDF 的页图端点都返回 415 "只有 PDF 支持逐页渲染"。
    src, converted = _preview_source(doc)
    if width < 100 or width > 3000:
        raise ValidationFailed("width 必须在 100~3000 之间")

    try:
        data = render_page(src, page_no, width=width, as_jpeg=True)
    except IndexError as exc:
        # ⚠️ 越界页码是**请求错**（422），不是服务端故障（500）。
        #   Office 走转换版时页数可能与原件不同，所以更要说清楚是哪一份的页数
        raise ValidationFailed(
            f"页码 {page_no} 超出范围（{'转换版' if converted else '原件'}"
            f"共 {getattr(src, 'name', '')} 的页数）"
        ) from exc
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/documents/{doc_id}/preview",
    summary="带 L3 高亮框的页图（调试/验证用）",
    response_class=Response,
)
async def get_preview_with_boxes(
    doc_id: int,
    ctx: CurrentUser,
    session: DbSession,
    page: int | None = None,
    chunk_index: int | None = None,
    width: int = 1200,
) -> Response:
    """两种形态（契约 §4.4）：

    - **不带 `page` / `chunk_index`** → 返回**预览文件本身**
      （Office 返回 LibreOffice 转换出的 PDF，PDF/图片返回原件，MD 返回纯文本）
    - **带 `page`（可带 `chunk_index`）** → 返回页图；给了 `chunk_index`
      就把该 chunk 的坐标框画进同一页（调试/验证 L3 用）

    ⚠️ `page` 刻意**不给默认值**：给了默认值的话"不传参"会悄悄变成"渲染第 1 页"，
    于是 `/preview` 永远返回 JPEG —— 而契约要的是文件本身（Office 的转换 PDF）。
    """
    from ...ingest.preview import render_page_with_boxes

    doc = await load_document(session, ctx, doc_id)

    if page is None and chunk_index is None:
        return await _preview_file(doc)
    path = blob_path(doc.blob_sha)
    if not path.exists():
        raise ResourceNotFound(detail={"doc_id": doc_id, "reason": "blob 缺失"})
    # ⚠️ **画框的坐标来源**：PDF 用**原件**排版算的（D33）；
    #   Office 用**转换版**自己算的（D131）—— 两者都由本项目抽，
    #   所以框与页图**永远同源**，不会"指向错误的文字"。
    if page is None:
        page = 1        # 只给了 chunk_index（少见），按第 1 页算

    from ...ingest.office import needs_conversion

    if doc.file_name.lower().endswith(".pdf"):
        src = path
    elif needs_conversion(doc.file_name, doc.mime_type):
        # 🔑 D131：Office 文档的坐标是在 `KIND_PDF_PREVIEW` 里从**转换版**
        # 补上的（`ingest/relocate.py`）。所以带框预览对它是支持的 ——
        # 前提是坐标真的补上了（定位不到的那些 chunk **故意不给框**）。
        # `_preview_source` 在转换还没跑时抛 415 并说清去哪儿看任务状态。
        src, _converted = _preview_source(doc)
        # ⚠️ 门禁只对**带框**请求生效：普通页图（不给 chunk_index）
        #   对 Office 一直是能看的（D34 v1.1 就支持），第一版把门禁放在
        #   分支最前面 → `?page=1` 也被拒，**回归**了一个本来就好的功能。
        if chunk_index is not None:
            state = await _chunk_boxes_state(session, doc_id, chunk_index)
            if state is None:
                raise ResourceNotFound(detail={"chunk_index": chunk_index})
            if state is False:
                raise UnsupportedFormat(
                    "这一段在转换版里定位不到坐标 —— 宁可不给框也不给错的框"
                    "（D131：定位不到就没有 locations，所以画不出框）"
                )
            logger.info(
                "带框预览用**转换版**坐标 doc=%s chunk=%s page=%s",
                doc_id, chunk_index, page,
            )
    else:
        raise UnsupportedFormat(
            f"{doc.file_name} 不是 PDF，也没有转换版预览，带框预览不可用"
            "（图片类文档只有整页高亮，D60）"
        )

    # ⚠️ 宽窄校验与页码越界处理**两支共用**（第一版只写在 PDF 那一支，
    #   于是 Office 的 `?width=99999` 与越界页码都变成 500 而不是 422）。
    if width < 100 or width > 3000:
        raise ValidationFailed("width 必须在 100~3000 之间")
    try:
        return await _render_optional_boxes(
            src, page, chunk_index, width, session, doc_id=doc_id
        )
    except IndexError as exc:
        raise ValidationFailed(f"页码 {page} 超出范围") from exc



async def _preview_file(doc) -> Response:
    """`GET /preview`（不带 page）→ **预览文件本身**（契约 §4.4）。

    | 文档 | 返回 | Content-Type |
    |---|---|---|
    | PDF | 原件 | `application/pdf` |
    | 图片 | 原件 | 图片 mime |
    | Office（docx/xlsx/pptx…） | **转换版** `preview.pdf`（D34 v1.1） | `application/pdf` |
    | 文本/Markdown | 原件 | `text/plain` |

    ⚠️ Office 返回的是**转换版本**，UI 必须标注（D34 明确接受的代价：
    字体替换/表格重排）。转换还没跑完 → 415 并告诉调用方去看任务状态。
    """
    src, converted = _preview_source(doc)
    data = src.read_bytes()
    if src.suffix.lower() == ".pdf" or converted:
        media = "application/pdf"
    elif _is_image(doc.file_name):
        media = mime_type_for(doc.file_name)
    else:
        media = "text/plain; charset=utf-8"
    return Response(
        content=data,
        media_type=media,
        headers={
            "Cache-Control": "private, max-age=3600",
            # 🔑 如实标注来源：前端要显示"预览为转换版本"（D34）
            "X-Preview-Source": "converted" if converted else "original",
            "Content-Disposition": _inline_disposition(doc.file_name),
        },
    )


def _inline_disposition(file_name: str) -> str:
    """`Content-Disposition`，**中文文件名必须用 RFC 5987 编码**。

    ⚠️ HTTP 头是 latin-1 的：`filename="张三-申请书.docx"` 直接抛
    `UnicodeEncodeError` → **整个响应 500**（而且只在有中文名的文件上出现，
    英文名的测试全绿）。所以：
    - `filename*=` 用 `UTF-8''<percent-encoded>`（RFC 5987，现代浏览器认）
    - `filename=` 放一份 ASCII 回退（老客户端）
    """
    from urllib.parse import quote

    ascii_fallback = (
        file_name.encode("ascii", "replace").decode("ascii").replace('"', "_")
        or "preview"
    )
    return (
        f"inline; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(file_name, safe='')}"
    )


async def _chunk_boxes_state(  # noqa: ANN001 — AsyncSession
    session, doc_id: int, chunk_index: int | None
) -> bool | None:
    """`True` 有坐标 / `False` 有这个 chunk 但没坐标 / `None` **没有这个 chunk**。

    🔑 三态而不是布尔：Office 文档里"定位不到的那几个 chunk"是
    **有**这个 chunk、只是没有坐标（→ 415 说清原因），
    而 `chunk_index` 写错了是**不存在**（→ 404）。
    第一版把两者都答成"没坐标" → 打错索引也回 415，
    于是\"带框预览坏了\"与\"你写错了索引\"在客户端无法区分（§0.4 的形状）。

    ⚠️ 判**内容**而不是 `locations IS NOT NULL`：这一列是 JSONB，
    存进去的常常是 JSON `null` → SQL 侧判不出来，会放行一份没有框的 chunk
    （症状是"框画不出来"而不是报错）。见 `locations.has_locations`。
    """
    if chunk_index is None:
        return None
    from ...common.models import Chunk
    from ...ingest.locations import has_locations

    row = (
        await session.execute(
            select(Chunk.locations).where(
                Chunk.doc_id == doc_id, Chunk.chunk_index == chunk_index
            )
        )
    ).first()
    if row is None:
        return None
    return has_locations(row[0])


async def _render_optional_boxes(
    src: Path, page: int, chunk_index: int | None, width: int,
    session, *, doc_id: int,
) -> Response:
    """页图；给了 `chunk_index` 就把坐标框画进同一页。

    ⚠️ 画框用**调用方传进来的 `src`**，不再自己去找原件（D131）：
    坐标与被渲染的页**必须来自同一个文件** ——
    PDF 的坐标按原件排版算（D33），所以 `src` 是原件；
    Office 的坐标按转换版算（D131），所以 `src` 是 `preview.pdf`。
    🔑 这条不变量是 D106 的核心："框与页图不同源 ⇒ 框可能指错字"。
    D131 之前的实现在这里**重新推导**了一份原件，于是给 Office 画框时
    打开的是 docx → `pymupdf` 报 `is no PDF` → **500**。
    """
    from ...ingest.preview import render_page_with_boxes

    if chunk_index is None:
        data = render_page(src, page, width=width, as_jpeg=True)
        return Response(content=data, media_type="image/jpeg")

    from ...common.models import Chunk
    from ...ingest.locations import has_locations

    chunk = (
        await session.execute(
            select(Chunk).where(
                Chunk.doc_id == doc_id, Chunk.chunk_index == chunk_index
            )
        )
    ).scalar_one_or_none()
    if chunk is None:
        raise ResourceNotFound(detail={"chunk_index": chunk_index})
    # ⚠️ 判**内容**（JSON null / [] 都不算有坐标），见 `locations.has_locations`
    if not has_locations(chunk.locations):
        raise ResourceNotFound(detail={
            "chunk_index": chunk_index,
            "reason": "这一段没有坐标（Office 文档定位不到，或转换还没跑完）",
        })
    data = render_page_with_boxes(
        src, page, chunk.locations, width=width, as_jpeg=True
    )
    return Response(content=data, media_type="image/jpeg")


# ============================================================
# 更新 / 删除 / 恢复
# ============================================================


@router.post(
    "/documents/{doc_id}/reparse",
    status_code=status.HTTP_202_ACCEPTED,
    summary="重新摄取（改了分块配置后）",
)
async def reparse(doc_id: int, ctx: CurrentUser, session: DbSession) -> dict:
    doc = await load_document(session, ctx, doc_id)
    await assert_kb_write(session, ctx, doc.kb_id)

    doc.status = "pending"
    job = await enqueue_async(session, KIND_INGEST, doc_id=doc.id, kb_id=doc.kb_id)
    await session.commit()
    return {"job_id": job.id, "doc_id": doc.id}


@router.post(
    "/documents/{doc_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="失败后重试",
)
async def retry_document(doc_id: int, ctx: CurrentUser, session: DbSession) -> dict:
    doc = await load_document(session, ctx, doc_id)
    await assert_kb_write(session, ctx, doc.kb_id)

    if doc.status != "failed":
        raise InvalidRequest(f"文档状态是 {doc.status}，只有 failed 才需要重试")

    doc.status = "pending"
    doc.status_msg = None
    job = await enqueue_async(session, KIND_INGEST, doc_id=doc.id, kb_id=doc.kb_id)
    await session.commit()
    return {"job_id": job.id, "doc_id": doc.id}


@router.delete(
    "/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档（软删除，30 天回收站）",
)
async def delete_document(doc_id: int, ctx: CurrentUser, session: DbSession) -> None:
    """软删除（D26）。

    ⚠️ 不在这里同步删 Qdrant：几千个点的删除要几秒，会让 DELETE 卡住。
    向量清理走 cleanup 任务。检索侧二次过滤 `is_current`，
    所以对外表现是"立刻搜不到"。
    """
    doc = await load_document(session, ctx, doc_id)
    await assert_kb_write(session, ctx, doc.kb_id)

    doc.deleted_at = dt.datetime.now(dt.UTC)
    doc.is_current = False
    await cancel_jobs_for_doc_async(session, doc_id)
    await session.commit()


@router.get("/kb/{kb_id}/trash", response_model=list[DocumentOut], summary="回收站")
async def list_trash(
    kb_id: int,
    ctx: CurrentUser,
    session: DbSession,
    page: int = 1,
    page_size: int = 50,
) -> list[DocumentOut]:
    await assert_kb_access(session, ctx, kb_id)
    rows, _ = await run_paginated(
        session,
        select(Document)
        .where(Document.kb_id == kb_id, Document.deleted_at.isnot(None))
        .order_by(Document.deleted_at.desc()),
        page,
        page_size,
    )
    return [DocumentOut.model_validate(r) for r in rows]


@router.get(
    "/documents/{doc_id}/thumbs/{page_no}",
    summary="缩略图（D36：入库时预生成，宽 400px）",
    response_class=Response,
)
async def get_thumbnail(
    doc_id: int, page_no: int, ctx: CurrentUser, session: DbSession
) -> Response:
    """按**页**取预生成的缩略图。

    ⚠️ 路径参数是页码，不是 chunk_index —— 契约早期写的 `{chunk_id}` 是漂移
    （缩略图存在 `data/blobs/{aa}/{sha}/thumbs/{page}.jpg`，按页编号）。
    照契约写前端会取不到图。

    为什么预生成而不是按需渲染：列表页一次要显示几十张，
    每张都现渲染会把页面卡死（D36 的原话）。
    """
    doc = await load_document(session, ctx, doc_id)
    # ⚠️ 判格式看 file_name，不看 blob 路径（§0.8）——
    # 曾经因为看 path.suffix 导致所有 PDF 的页图端点返回 415
    if not doc.file_name.lower().endswith(".pdf"):
        raise UnsupportedFormat("只有 PDF 有页面缩略图")
    if page_no < 1 or page_no > (doc.page_count or 1):
        raise ValidationFailed(
            f"页码 {page_no} 超出范围（这份文档共 {doc.page_count or 1} 页）"
        )

    path = thumb_path(doc.blob_sha, page_no)
    if not path.exists():
        # 还没生成（worker 还在跑，或该格式不生成）。**404 而不是空图** ——
        # 返回一张占位图会让用户以为"这就是那页的内容"
        raise ResourceNotFound(
            detail={"doc_id": doc_id, "page": page_no, "reason": "缩略图尚未生成"}
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        # 内容寻址：同一份文件同一页永远一样，可以放心长缓存
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.delete("/trash", response_model=PurgeResp, summary="清空回收站（不可撤销）")
async def empty_trash(
    ctx: CurrentUser,
    session: DbSession,
    kb_id: int | None = None,
    dry_run: bool = False,
) -> PurgeResp:
    """物理删除回收站里的文档：删行、删索引、减 df、释放 blob。

    与 worker 的 `KIND_CLEANUP`（30 天到期自动清）**走同一份删除链**
    （`services/ingest/purge.py`）—— 那条链有六步，漏任何一步都不报错，
    复制一遍等于给 PROGRESS §5.29（df 漏减 → idf 被压平 → 质量悄悄变差）
    造第二条绕过路径，而且这条是不可逆的。

    @param kb_id 只清这个库；不给则清"我能读且能写"的全部库
    @param dry_run 只报告会删多少，不动数据。前端用它渲染确认框 ——
        DELETE 没有请求体，没法"先问一下"
    """
    if kb_id is not None:
        await assert_kb_write(session, ctx, kb_id)
        scope = [kb_id]
    else:
        scope = await _writable_kb_ids(session, ctx)

    # ⚠️ dry_run 必须在**动手之前**返回。写成
    #    `stats = await purge(...)` 再 `if dry_run: return stats`
    #    就是"先删了再报告" —— 第一版正是这么写的。
    if dry_run:
        return await _trash_preview(session, scope)

    if not scope:
        return PurgeResp(dry_run=False)

    stats = await purge_documents_async(session, kb_ids=scope)
    await session.commit()

    if stats.qdrant_failed:
        # 不吞：PG 已删但索引里还留着点（D7 允许短暂不一致，
        # 但需要有人知道）。阶段 4 的对账端点也会捡到它们。
        logger.warning(
            "清空回收站：%s 份文档的 Qdrant 点未清（PG 已删）docs=%s",
            len(stats.qdrant_failed), stats.qdrant_failed[:20],
        )
    logger.info(
        "清空回收站 scope=%s → %s 份 / %s chunk / 释放 %s 字节",
        scope, stats.docs, stats.chunks, stats.bytes_freed,
    )
    return PurgeResp(
        docs=stats.docs,
        chunks=stats.chunks,
        blobs_dropped=stats.blobs_dropped,
        bytes_freed=stats.bytes_freed,
        doc_ids=stats.doc_ids,
        ids_truncated=stats.ids_truncated,
        qdrant_failed=stats.qdrant_failed,
        dry_run=False,
    )


async def _trash_preview(session, kb_ids: list[int]) -> PurgeResp:
    """只算不删：数一数"会删几份、多大、哪些 id"。**`dry_run` 的实现**。

    ⚠️ 这里的 WHERE 条件必须与 `purge.purgeable_stmt` **一致** ——
    两处各写一遍，dry_run 报的数字就会与真删的不一样，而用户是照着
    dry_run 的数字点确认的。所以条件用同一个构造器。
    """
    if not kb_ids:
        return PurgeResp(dry_run=True)
    stmt = purgeable_stmt(kb_ids=kb_ids)
    rows = (
        await session.execute(
            select(Document.id, func.coalesce(Document.size_bytes, 0)).where(
                *stmt.whereclause.clauses
            )
        )
    ).all()
    ids = [r[0] for r in rows]
    return PurgeResp(
        docs=len(ids),
        bytes_freed=sum(r[1] for r in rows),
        doc_ids=ids[:PurgeStats.ID_LIST_CAP],
        ids_truncated=len(ids) > PurgeStats.ID_LIST_CAP,
        dry_run=True,
    )


async def _writable_kb_ids(session, ctx) -> list[int]:
    """"我能读且能写"的库 id。

    ⚠️ 可见性判定只在 `auth.can_write_kb` 一处（§0.6），这里**不重算**
    三档语义，只把它应用到"哪些行要拿出来"上。
    """
    rows = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None))
        )
    ).scalars().all()
    return sorted(
        k.id for k in rows if ctx.can_read_kb(k.id) and can_write_kb(ctx, k)
    )


@router.post(
    "/documents/{doc_id}/restore",
    response_model=DocumentOut,
    summary="从回收站恢复",
)
async def restore_document(
    doc_id: int, ctx: CurrentUser, session: DbSession
) -> DocumentOut:
    """恢复文档并重新入队摄取。

    ⚠️ 恢复后**必须重跑摄取**：D44 定了旧版本向量只留 3 天，
    超过就没了。即使还在也要重跑 —— 期间改过 KB 的 chunk_size 的话，
    旧 chunk 的切法已经过时。
    """
    doc = (
        await session.execute(
            select(Document).where(
                Document.id == doc_id, Document.deleted_at.isnot(None)
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise ResourceNotFound(detail={"doc_id": doc_id})
    await assert_kb_access(session, ctx, doc.kb_id)

    # 同名文档还在的话，恢复会与它冲突（D28 覆盖式）
    conflict = (
        await session.execute(
            select(Document).where(
                Document.kb_id == doc.kb_id,
                Document.file_name == doc.file_name,
                Document.is_current.is_(True),
                Document.id != doc_id,
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise InvalidRequest(
            f"已有一个同名文档是当前版本（doc={conflict.id}），请先删除它再恢复"
        )

    doc.deleted_at = None
    doc.is_current = True
    doc.status = "pending"
    await enqueue_async(session, KIND_INGEST, doc_id=doc.id, kb_id=doc.kb_id)
    await session.commit()
    return DocumentOut.model_validate(doc)
