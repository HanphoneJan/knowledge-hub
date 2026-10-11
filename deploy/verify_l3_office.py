"""评估：L3 坐标能不能覆盖 Office 文档（D131 · CONVENTIONS §6.10 的待评估项）。

用法：

    cd ~/develop-projects/knowledge-hub
    PYTHONPATH=. uv run python deploy/verify_l3_office.py

## 它先纠正了这个问题**问错了**的地方

§6.10 写的是"Office 文档坐标：走 LibreOffice 转 PDF 后排版会变，
**需重新评估**（D34 排 v1.1）"，`services/ingest/office.py` 的模块 docstring
也写着"本模块不做的事：把 L3 坐标从原件迁移到转换版 PDF"。

而查代码发现：**`.docx/.xlsx/.pptx` 的解析器根本不产坐标**
（`parsers.py::_parse_docx` 的 docstring 第一行就是"⚠️ **无坐标**（D34 排 v1.1）"，
只有 `_parse_pdf` 才走 `parse_pdf_with_locations`）。

⇒ 所以"把**原件**坐标迁移到转换版"这个问题**不成立**：没有原件坐标可迁移。
真正的问题是另一个，而且更好答：

> **转换版 PDF 里能不能为已存好的每个 chunk 文本定位？**

答"能"的话，`/preview?chunk_index=N` 就能对 Office 文档开放 ——
而且框**天生对齐**，因为用户看到的页图就是那份转换 PDF，
文本与坐标来自**同一次渲染**（"重排"从此不是问题）。

## 判据：原文 chunk 的文字，有多少能在转换版里定位

逐字符做**贪心最长匹配**：从当前位置起，取能在转换版任何一行里找到的
最长片段（最长 40 字，最短 8 字），命中就把它标成"已定位"并前进。

指标 = **覆盖率**：

| 覆盖率 | 结论 |
|---|---|
| ≥ 95% | 可以做：几乎每个 chunk 都能拿到框 |
| 80~95% | 可以做，但要处理剩下的（多栏/表格/连字符）|
| < 80% | 正式记成"不做"，并写清数字 |

⚠️ **第一版这个脚本自己也量错了**（§5.126 的同款）：
用 `Chunk.locations IS NOT NULL` 判"有没有坐标"，而列里存的是
JSON `null` 而不是 SQL `NULL` → 数出来 8/8"有坐标"，与解析器的说明矛盾。
🔑 所以下面判"有没有坐标"看的是**内容**（能不能取出至少一个框），
不是 SQL 的 nullness（§5.57：数字只能来自真实来源）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_PIECE = 40     # 贪心匹配的最长片段
MIN_PIECE = 8      # 短于此就不试了（撞车太多）


def _norm(s: str) -> str:
    """比较用的规范化：去掉所有空白（转换会重新折行、重排空格）。"""
    return "".join(s.split())


def _pick_office_doc() -> tuple[int, str, str, str]:
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import Document
    from services.common.storage import blob_path, preview_path

    with sync_session_scope() as s:
        rows = (
            s.execute(
                select(Document)
                .where(Document.file_name.like("%.docx"))
                .order_by(Document.id.desc())
            ).scalars().all()
        )
        if not rows:
            raise SystemExit("库里没有 .docx（先上传一份 Office 文档）")
        doc = rows[0]
        return (
            doc.id, doc.file_name,
            str(blob_path(doc.blob_sha)), str(preview_path(doc.blob_sha)),
        )


def _chunks_of(doc_id: int) -> list[tuple[int, str, object]]:
    from sqlalchemy import select

    from services.common.db import sync_session_scope
    from services.common.models import Chunk

    with sync_session_scope() as s:
        return [
            (c.chunk_index, c.content_norm or "", c.locations)
            for c in s.execute(
                select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index)
            ).scalars().all()
        ]


def _line_index(pdf_path: Path) -> list[tuple[str, list[float], int]]:
    """转换版里的每一行（文本、归一化 bbox、页号）。"""
    from services.ingest.locations import parse_pdf_with_locations

    parsed = parse_pdf_with_locations(pdf_path)
    out: list[tuple[str, list[float], int]] = []
    for pc in parsed.chunks:
        for loc in pc.locations or []:
            bbox = loc.get("bbox")
            if not bbox:
                continue
            out.append((_norm(pc.text), [float(x) for x in bbox], int(loc.get("page") or 0)))
    return out


def _coverage(needle: str, lines: list[tuple[str, list[float], int]]) -> tuple[float, list[tuple[float, int]]]:
    """贪心最长匹配：返回 (覆盖率, 命中的框列表)。"""
    if not needle:
        return 0.0, []
    covered = 0
    boxes: list[tuple[float, int]] = []
    i = 0
    n = len(needle)
    while i < n:
        hit = False
        for ln in range(min(MAX_PIECE, n - i), MIN_PIECE - 1, -1):
            piece = needle[i:i + ln]
            if not piece:
                continue
            for text, bbox, page in lines:
                if piece in text:
                    covered += ln
                    if bbox not in [b for b, _p in boxes]:
                        boxes.append((bbox, page))
                    i += ln
                    hit = True
                    break
            if hit:
                break
        if not hit:
            i += 1      # 这一字定位不到，跳过
    return covered / n, boxes


def main() -> int:
    doc_id, name, src, prev = _pick_office_doc()
    print(f"素材：doc #{doc_id} · {name}")
    print(f"原件：{src}")
    print(f"转换版：{prev}")

    prev_p = Path(prev)
    if not prev_p.exists():
        print("\n⚠️ 库里还没有转换产物（KIND_PDF_PREVIEW 没跑过）。")
        print("   先让 worker 摄取一次这份文档再跑本脚本。")
        return 0

    chunks = _chunks_of(doc_id)
    lines = _line_index(prev_p)
    print(f"转换版行数：{len(lines)} · 原件解析出 {len(chunks)} 个 chunk")

    # ---- 事实 ①：原件 chunk 到底有没有坐标（看**内容**，不看 SQL nullness）----
    usable = sum(1 for _i, _t, loc in chunks if loc)
    print(f"\n原件 chunk 里**能取出框**的：{usable}/{len(chunks)}")
    if usable == 0:
        print("⇒ §6.10 问的「把原件坐标迁到转换版」**不成立**：没有原件坐标可迁。"
              "\n   `_parse_docx` 的 docstring 第一行就写着「⚠️ 无坐标（D34 排 v1.1）」。")

    # ---- 事实 ②：原文的每个 chunk 能在转换版里定位多少 ----
    rows = []
    for idx, text, _loc in chunks:
        cov, boxes = _coverage(_norm(text), lines)
        pages = sorted({p for _b, p in boxes})
        rows.append((cov, idx, pages, text))
    rows.sort(reverse=True)

    avg = sum(r[0] for r in rows) / len(rows) if rows else 0.0
    full = [r for r in rows if r[0] >= 0.95]
    print(f"\n平均覆盖率 {avg:.0%}；≥95% 的 chunk {len(full)}/{len(rows)}")
    for cov, idx, pages, text in rows:
        flag = "✅" if cov >= 0.95 else ("⚠️" if cov >= 0.8 else "✗")
        print(f"  {flag} chunk#{idx} 覆盖 {cov:5.0%} 页={pages}  «{text[:26]}»")

    verdict = (
        "可以做：从**转换版**抽坐标（文本与坐标同源，天然对齐）"
        if avg >= 0.8 else
        "不做：转换版重排导致大部分文字定位不到（数字见上），"
        "维持 D106（带框预览只支持 PDF）"
    )
    print(f"\n判读：{verdict}")
    print(
        "⚠️ 无论结论如何，先把 §6.10 的「待评估」改成结论 + 上面的数字 ——\n"
        "   留成待评估的代价是：每轮都有人重新想一遍（§5.57）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())