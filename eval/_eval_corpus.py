"""真实语料评测库的构建（**在进程内走真实摄取路径**）。

## 为什么用真实语料，而不是合成语料

文章《RAG 优化》要求评测集覆盖"真实文档分布"：表格、标题层级、口语化提问。
合成语料能精确断言，但**逼不出**真实解析/切分/召回里的问题（表格错位、
页眉污染、编号类查询）。所以本评测库直接摄取 `data/` 下的**真实文档**
（个人资料 / 博客 / 项目文档的 Markdown 版），问题集见 `eval/_qset.py`。

## 为什么走 `_create_doc` + `handle_ingest`

这两条正是**上传端点与 worker** 用的同一条链（blob 登记 → 解析 → 切分 →
嵌入 → 写 Qdrant → 登记 BM25 词表）。评测要问"真实链路虚不虚"，就不能自己
拼 `upsert_points` 绕过它 —— 那样测出来的分数与线上没有关系。

## 与合成的 `tests/test_retrieval_e2e.py` 的分工

| | 合成（pytest） | 真实（本模块 + `retrieval_eval.py`） |
|---|---|---|
| 目的 | 腿的正确性 / fail-closed | 召回**质量**与回放 |
| 期望 | 逐字可判、零维护 | 人工标注（`_qset.py`），需维护 |
| 跑在哪 | 每次 pytest | `uv run python eval/retrieval_eval.py`（本机全量）|
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 🔴 离线加载模型（理由见 tests/conftest.py）：否则每篇文档都会去 huggingface.co
#   重试 5 次，整套评测慢到不可用。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.config import get_settings  # noqa: E402

#: 评测库的归属账号。⚠️ 用 `@t.local` 域 ⇒ 每轮基线的 `_reset` 会把它清掉
#: （与 1b/1c/1d 同一套清场约定），不会污染长期数据。
EVAL_EMAIL = "retr-eval@t.local"

#: 真实语料来源（相对 `data/`）。都可读、体积小、解析确定（Markdown）。
#: ⚠️ `imports/eval_chat` 由 `materialize_chat_days()` 生成（真实聊天按天渲染）。
CORPUS_DIRS: tuple[str, ...] = (
    "docs_light", "blogs", "imports/readme_demo", "imports/eval_chat",
)

#: 真实聊天记录源（date/chat 意图的语料来源）。
CHAT_SRC = "chat_messages_clean.jsonl"


def materialize_chat_days(n_days: int = 8) -> dict[str, str]:
    """把真实聊天的若干天各渲染成一个 Markdown 文档，返回 `{日期: 代表性片段}`。

    🔑 路由表的 **chat / date 行**此前没有语料可测。这里用**真实聊天**
    （`data/chat_messages_clean.jsonl`）每天一份文档，让"某天聊了什么"这类
    **日期字面量**查询有确定真值（真值 = 那一天的那份文档）。

    ⚠️ 写进 `data/imports/eval_chat/`（`data/` 不进 git）—— 幂等，已存在就覆盖。
    返回空 dict 表示没有聊天源（跳过 chat/date 段，不是错误）。
    """
    import json as _json

    root = get_settings().data_path
    src = root / CHAT_SRC
    out_dir = root / "imports" / "eval_chat"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return {}

    by_day: dict[str, list[str]] = {}
    for line in src.read_text(encoding="utf-8").splitlines():
        try:
            m = _json.loads(line)
        except (ValueError, TypeError):
            continue
        d = str(m.get("date") or "").strip()
        t = str(m.get("text") or "").strip()
        if d and len(t) >= 8 and "\n" not in t:
            by_day.setdefault(d, []).append(t)

    markers: dict[str, str] = {}
    for d in sorted(by_day)[:n_days]:
        msgs = by_day[d]
        marker = max(msgs, key=len)          # 代表性片段：当天最长的一条
        body = "\n".join(f"[{d}] {t}" for t in msgs[:200])
        (out_dir / f"chat-{d}.md").write_text(
            f"# 聊天记录 {d}\n\n{body}\n", encoding="utf-8"
        )
        markers[d] = marker
    return markers

#: 只摄取文本类。扫描件/照片的视觉腿由 keeper 库单独覆盖（不在本评测里）。
_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def corpus_files() -> list[tuple[str, Path]]:
    """真实语料清单：`(相对 data/ 的路径, 绝对路径)`，按路径排序（可复现）。"""
    data_path = get_settings().data_path
    out: list[tuple[str, Path]] = []
    for d in CORPUS_DIRS:
        base = data_path / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES:
                out.append((p.relative_to(data_path).as_posix(), p))
    return out


def build(*, kb_name: str, limit: int | None = None) -> dict:
    """建 1 用户 + 1 工作区 + 1 知识库，并把真实语料逐篇摄取。

    ⚠️ `file_name` 用**相对路径**（含 `/`）而不是 basename：语料里存在同名文件
    （如同一篇 `.docx.md` 与 `.pdf.md`），而同一库内同名会触发 `supersede`
    （`_create_doc` 的增量索引语义）—— 那会让后摄取的版本把先摄取顶进回收站，
    评测库凭空少文档。相对路径同时让"这条 query 命中哪份文件"可读。

    @return `{kb_id, user_id, ws_id, files: {相对路径: doc_id}, n_docs}`
    """
    from services.api.routers._shared import guess_mime
    from services.common.db import sync_session_scope
    from services.common.models import KnowledgeBase, User, Workspace
    from services.worker.handlers import _create_doc, handle_ingest

    files = corpus_files()
    if limit is not None:
        files = files[:limit]

    with sync_session_scope() as s:
        user = User(email=EVAL_EMAIL, password_hash="x", display_name="检索评测")
        s.add(user)
        s.flush()
        ws = Workspace(name="检索评测工作区", owner_id=user.id)
        s.add(ws)
        s.flush()
        kb = KnowledgeBase(
            workspace_id=ws.id,
            name=kb_name,
            visibility="private",
            embed_model=get_settings().text_embed_model,
            chunk_size=500,
            chunk_overlap=50,
            created_by=user.id,
        )
        s.add(kb)
        s.flush()

        file_to_doc: dict[str, int] = {}
        failed: list[str] = []
        for rel, path in files:
            data = path.read_bytes()
            try:
                # 🔑 每一篇一个 **SAVEPOINT**：某篇"解析后为空"时只回滚它自己，
                #   不能把用户/工作区/知识库（同一事务里）一起回滚掉 ——
                #   否则后续文档全部撞 `documents_kb_id_fkey`。
                with s.begin_nested():
                    doc, _outcome = _create_doc(
                        s,
                        kb=kb,
                        file_name=rel,
                        data=data,
                        mime_type=guess_mime(path) or "text/markdown",
                        doc_source="upload",
                        source_path=str(path),
                        uploaded_by=user.id,
                    )
                    if doc is None:  # 同内容重复 → 跳过
                        continue
                    s.flush()
                    handle_ingest(
                        s, doc_id=doc.id, payload={}, progress=lambda *_: None
                    )
                file_to_doc[rel] = doc.id
            except Exception as exc:  # noqa: BLE001
                # 真实语料里确有"解析后为空"的文件（如纯表格模板）。评测库缺一篇
                # 不影响其它 query，记下来即可 —— 但**不静默**：调用方据此知道
                # "这篇没进库"，而不是以为它进了。
                failed.append(f"{rel}: {type(exc).__name__}: {exc}")
        s.flush()
        ids = {"kb_id": kb.id, "user_id": user.id, "ws_id": ws.id}

    ids["files"] = file_to_doc
    ids["n_docs"] = len(file_to_doc)
    ids["failed"] = failed
    return ids


__all__ = ["CORPUS_DIRS", "EVAL_EMAIL", "build", "corpus_files"]
