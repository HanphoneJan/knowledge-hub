"""scan 意图重测（P2 的最后一行）—— 从**真实 PDF** 派生一份"扫描件"再测视觉腿。

## 为什么需要它

路由表的 `scan` 行（`visual 2.0 + sparse 1.2`）一直**没有语料可测**：库里
`kb_visual` 的页级点全是**照片**，没有扫描页。这份脚本把一份**真实 PDF**
的几页**栅格化成图**、包成一份**没有文本层**的 PDF（就是"扫描件"），
由此得到可判真值的扫描语料。

## 为什么是脚本而不是并进 retrieval_eval

它要**真编码**（视觉服务 ~15s/页）与真 Qdrant 写，成本高、且依赖本地 blob，
属"手工验证脚本"一族（与 `colsmol_bench.py` / `rerank_probe.py` 同级）。

## 跑法

```bash
bash deploy/dev.sh start           # 需要视觉服务 :8020 可达
uv run python eval/scan_recheck.py
```

⚠️ 用 `VISUAL_WRITE_VIA_SERVICE=true` 走**视觉服务**编码（本进程不加载 colSmol）。
产出屏幕表格 + `reports/scan_recheck.json`。跑完清掉自己的评测账号。
"""

from __future__ import annotations

import os

# 🔴 必须在导入 config/services 之前设：让视觉**写侧**走远端服务（本进程零 colSmol）。
os.environ.setdefault("VISUAL_WRITE_VIA_SERVICE", "true")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _eval_corpus import EVAL_EMAIL  # noqa: E402

# 🔴 扫描件重测的问题集是**真实语料相关** ⇒ 从不被追踪的 `_local_data.py` 读
#   （没有则退回合成示例）。仓库里不许出现这些真实值。
try:  # noqa: SIM105
    import _local_data as _LD
except ModuleNotFoundError:
    import _local_data_example as _LD

from services.common.auth import UserContext  # noqa: E402
from services.common.db import dispose_engines, session_scope, sync_session_scope  # noqa: E402
from services.retrieval import engine  # noqa: E402
from services.retrieval.dense import search_dense  # noqa: E402
from services.retrieval.sparse import search_sparse  # noqa: E402
from services.retrieval.trgm import search_trgm  # noqa: E402
from services.retrieval.visual import search_visual  # noqa: E402

SCAN_PAGES = 4
REPORT = ROOT / "reports" / "scan_recheck.json"
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


#: 扫描件重测的问题集（真实语料相关，见 `_local_data.py`）。
SCAN_QUERIES = list(_LD.SCAN_QUERIES)


def _find_pdf_blob() -> Path | None:
    for f in sorted((ROOT / "data" / "blobs").rglob("original")):
        try:
            if f.read_bytes()[:4] == b"%PDF":
                return f
        except OSError:
            continue
    return None


def _make_scanned_pdf(src: Path, out: Path, pages: int = SCAN_PAGES) -> int:
    """把真实 PDF 的前 `pages` 页栅格化成图、包成**无文本层**的 PDF（=扫描件）。"""
    import io

    import pymupdf

    src_doc = pymupdf.open(src)
    out_doc = pymupdf.open()
    try:
        n = min(pages, src_doc.page_count)
        for i in range(n):
            pix = src_doc[i].get_pixmap(matrix=pymupdf.Matrix(2, 2))  # ~144dpi
            png = pix.tobytes("png")
            rect = src_doc[i].rect
            page = out_doc.new_page(width=rect.width, height=rect.height)
            page.insert_image(rect, stream=png)
        out.parent.mkdir(parents=True, exist_ok=True)
        out_doc.save(out)
    finally:
        src_doc.close()
        out_doc.close()
    return n


def _build_scan_kb(pdf_path: Path) -> dict:
    from services.api.routers._shared import guess_mime
    from services.common.config import get_settings
    from services.common.models import KnowledgeBase, User, Workspace
    from services.ingest.pipeline import ingest_document
    from services.worker.handlers import _DocRef, _create_doc  # noqa: F401

    with sync_session_scope() as s:
        user = User(email=EVAL_EMAIL, password_hash="x", display_name="扫描评测")
        s.add(user)
        s.flush()
        ws = Workspace(name="扫描评测ws", owner_id=user.id)
        s.add(ws)
        s.flush()
        kb = KnowledgeBase(
            workspace_id=ws.id, name="扫描评测-真实派生", visibility="private",
            embed_model=get_settings().text_embed_model, chunk_size=500,
            chunk_overlap=50, created_by=user.id,
        )
        s.add(kb)
        s.flush()
        data = pdf_path.read_bytes()
        doc, _ = _create_doc(
            s, kb=kb, file_name=pdf_path.name, data=data,
            mime_type=guess_mime(pdf_path) or "application/pdf",
            doc_source="upload", source_path=str(pdf_path), uploaded_by=user.id,
        )
        s.flush()
        # queue_visual=False：视觉编码由本脚本用视觉服务显式跑（不让 worker 抢）
        res = ingest_document(s, doc.id, queue_visual=False)
        page = 1
        pages = list(res.scan_pages)
        s.flush()
        ids = {"kb_id": kb.id, "user_id": user.id, "ws_id": ws.id,
               "doc_id": doc.id, "scan_pages": pages, "is_scanned": res.is_scanned}
    return ids


def _encode_pages(ids: dict) -> int:
    from services.worker.handlers import handle_visual

    done = 0
    for p in ids["scan_pages"]:
        with sync_session_scope() as s:
            handle_visual(s, doc_id=ids["doc_id"], payload={"page": p},
                          progress=lambda *_: None)
            s.commit()
            done += 1
    return done


def _ranked(cfg: str, ctx: UserContext, kb_id: int, q: str, limit: int = 10):
    async def _go():
        async with session_scope() as s:
            if cfg == "hybrid":
                out = await engine.search(s, ctx, q, kb_id=kb_id, mode="hybrid", top_k=limit)
                return [(h.doc_id, h.chunk_index) for h in out.items]
            if cfg == "dense":
                r = await search_dense(ctx, q, kb_id=kb_id, limit=limit, session=s)
            elif cfg == "sparse":
                r = await search_sparse(s, ctx, q, kb_id=kb_id, limit=limit)
            elif cfg == "trgm":
                r = await search_trgm(s, ctx, q, kb_id=kb_id, limit=limit)
            else:
                r = await search_visual(ctx, q, kb_id=kb_id, limit=limit)
            return [(h.doc_id, h.chunk_index) for h in r.hits]
    return run(_go())


def main() -> int:
    from _reset import reset_users_and_bm25

    reset_users_and_bm25([EVAL_EMAIL])
    blob = _find_pdf_blob()
    if blob is None:
        print("没找到真实 PDF blob —— 跳过（不是失败）")
        return 0
    scan_pdf = ROOT / "data" / "imports" / "eval_scan" / "scanned.pdf"
    n = _make_scanned_pdf(blob, scan_pdf)
    print(f"派生扫描件：{n} 页 ← {blob.relative_to(ROOT)}")

    ids = _build_scan_kb(scan_pdf)
    print(f"扫描页判出：{ids['scan_pages']}（is_scanned={ids['is_scanned']}）")
    encoded = _encode_pages(ids)
    print(f"已用视觉服务编码 {encoded} 页")

    ctx = UserContext(
        user_id=ids["user_id"], display_name="scan", email=EVAL_EMAIL,
        api_key_id=None, workspace_ids=frozenset({ids["ws_id"]}),
        readable_kb_ids=frozenset({ids["kb_id"]}),
    )
    kb_id = ids["kb_id"]
    gold = {ids["doc_id"]}          # 真值 = 这份扫描件（doc 级）

    from retrieval_metrics import score_query, summarize

    results = {}
    for cfg in ("dense", "sparse", "trgm", "visual", "hybrid"):
        rows = []
        for q in SCAN_QUERIES:
            ranked = list(dict.fromkeys(d for d, _ci in _ranked(cfg, ctx, kb_id, q)))
            rows.append(score_query(q, "scan", ranked, gold))
        results[cfg] = summarize(rows)
        o = results[cfg]["overall"]
        print(f"  [{cfg:<7}] Hit@1={o['hit_at'][1]:.3f} Hit@5={o['hit_at'][5]:.3f}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps({"scan_pages": ids["scan_pages"], "results": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reset_users_and_bm25([EVAL_EMAIL])
    dispose_engines()
    if not _LOOP.is_closed():
        _LOOP.close()
    print(f"报告写入 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
