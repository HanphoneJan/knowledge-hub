"""视觉腿（D124）验证脚本 —— 端到端走真实 API，不 mock 任何东西。

用法（需 API+worker 在跑，且 VDR_ENABLED 未关）：
    cd ~/develop-projects/knowledge-hub
    PYTHONPATH=. uv run python deploy/verify_visual_leg.py

⚠️ 这一份**不在** `eval/` 里，因为它是**手工**验证脚本而不是基线：
它要真跑 colSmol（4 张图 ≈60s + 2.5GB 内存），不适合每次提交都跑。
基线里对视觉腿的覆盖是 `tests/test_visual_leg.py`（18 项，不加载模型）。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:8010/api/v1"
# 真实 PDF（库里已有的一份扫描件指南），用来造"真照片"与"扫描件"
def _pick_source_pdf() -> Path:
    """挑一份**稳定的**真实 PDF 当素材源。

    ⚠️⚠️ 第一版是 `next(p for p in sorted(...) if 是 PDF)` ——
    **取路径排序后的第一个**，而 blob 是按 sha 命名的，于是这个脚本
    会把**自己上一轮生成的扫描件**当成素材源（sha 排序恰好落在前面）。
    后果是**失控的正反馈**：每跑一次，页面尺寸就翻一倍 ——
    A4 1075pt → 2240 → 4667 → 9723（此时 150dpi 要 20256×28642px，
    pymupdf 直接 `Overly large image`，视觉任务全 failed，
    扫描件文档永远停在 `pending`）。
    症状很有欺骗性：脚本前几轮全绿，从某一轮开始突然 4 项红，
    而**看起来像是视觉腿坏了**（其实是验证脚本在自喂）。

    ✅ 判据：选**首页面积最小**的那份，并且**硬性断言**它 150dpi 的
    长边在一个正常范围内 —— 不满足就直接报错退出，不要"将就"，
    因为将就出来的素材会一路放大到炸（§5.57：数字只能来自真实来源）。
    """
    import pymupdf

    cands = [p for p in sorted(Path("data/blobs").glob("*/*/original"))
             if p.read_bytes()[:4] == b"%PDF"]
    scored = []
    for p in cands:
        try:
            doc = pymupdf.open(p)
            r = doc[0].rect
            scored.append((r.width * r.height, p))
            doc.close()
        except Exception:          # 坏 blob 不当素材，也不该让脚本崩
            continue
    if not scored:
        raise SystemExit("库里没有可用的 PDF 素材（先跑一次摄取）")
    scored.sort()
    src = scored[0][1]
    doc = pymupdf.open(src)
    long_pt = max(doc[0].rect.width, doc[0].rect.height)
    n_pages = doc.page_count
    doc.close()
    long_px = long_pt * 150 / 72
    if long_px > 3000:
        raise SystemExit(
            f"素材源 {src} 的页面太大了（150dpi 长边 {long_px:.0f}px）—— "
            "拒绝拿它当素材：那正是本脚本上一轮的正反馈产物"
        )
    print(f"素材源：{src}（{n_pages} 页，150dpi 长边 {long_px:.0f}px）", flush=True)
    return src


REAL_PDF = _pick_source_pdf()

c = httpx.Client(base_url=API, timeout=httpx.Timeout(120.0, connect=5.0),
                 trust_env=False)
STAMP = time.strftime("%H%M%S")
fails = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global fails
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""),
          flush=True)
    if not ok:
        fails += 1
    return ok


def make_photo() -> bytes:
    """真照片：把真实 PDF 的一页渲染成 JPEG（不是纯色方块）。"""
    import pymupdf
    from PIL import Image

    pdf = pymupdf.open(REAL_PDF)
    try:
        pix = pdf[0].get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72))
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    finally:
        pdf.close()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def make_scanned_pdf(n_pages: int = 3) -> bytes:
    """扫描件：把 3 页渲染成图片再拼成**没有文本层**的 PDF。

    ⚠️ 嵌 **JPEG** 而不是 PNG：三页 150dpi 的位图 PNG 有 60MB+，
    会被 D30 的 50MB 上限挡成 413（真实扫描件本来就是 JPEG）。
    """
    import pymupdf
    from PIL import Image

    src = pymupdf.open(REAL_PDF)
    out = pymupdf.open()
    try:
        for i in range(n_pages):
            pix = src[i].get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72))
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            page = out.new_page(width=pix.width, height=pix.height)
            page.insert_image(pymupdf.Rect(0, 0, pix.width, pix.height),
                              stream=buf.getvalue())
        return out.tobytes()
    finally:
        src.close()
        out.close()


def wait_jobs(h: dict, kb_id: int, *, want: int, timeout: float = 600):
    t_end = time.time() + timeout
    while time.time() < t_end:
        r = c.get("/jobs", headers=h, params={"kb_id": kb_id, "page_size": 100})
        items = r.json().get("items", []) if r.status_code == 200 else []
        vis = [j for j in items
               if j["kind"] in ("ingest", "embed_visual", "thumbnail")]
        if len(vis) >= want and all(
            j["status"] in ("done", "failed", "cancelled") for j in vis
        ):
            return vis
        time.sleep(2)
    raise SystemExit(f"等任务超时（{timeout}s）")


# ---------- 1. 注册 + 建两个库 ----------
r = c.post("/auth/register", json={
    "email": f"vdr-{STAMP}@t.local", "display_name": "视觉腿验证",
    "password": "pw12345678",
})
if r.status_code not in (200, 201):
    raise SystemExit(f"注册失败 {r.status_code} {r.text[:200]}")
key = r.json()["api_key"]
h = {"Authorization": f"Bearer {key}"}
check("注册并拿到 API Key", bool(key), key[:8] + "…")

ws_id = c.get("/workspaces", headers=h).json()[0]["id"]
photo_kb = c.post("/kb", headers=h, json={
    "workspace_id": ws_id, "name": f"验证照片库 {STAMP}", "source_kind": "photo",
}).json()["id"]
scan_kb = c.post("/kb", headers=h, json={
    "workspace_id": ws_id, "name": f"验证扫描件库 {STAMP}", "source_kind": "file",
}).json()["id"]
check("建两个库", bool(photo_kb and scan_kb), f"photo={photo_kb} scan={scan_kb}")

# ---------- 2. 上传 ----------
r = c.post(f"/kb/{photo_kb}/documents", headers=h, files={
    "files": (f"真页面照片 {STAMP}.jpg", make_photo(), "image/jpeg")})
check("上传照片 → 202", r.status_code == 202, f"HTTP {r.status_code}")

r = c.post(f"/kb/{scan_kb}/documents", headers=h, files={
    "files": (f"扫描件 {STAMP}.pdf", make_scanned_pdf(3), "application/pdf")})
check("上传 3 页扫描件 → 202", r.status_code == 202, f"HTTP {r.status_code}")

# ---------- 3. 等任务（每图 ≈15s） ----------
print("\n等视觉任务（每图 ~15s，4 张）…", flush=True)
t0 = time.time()
jobs = wait_jobs(h, photo_kb, want=2) + wait_jobs(h, scan_kb, want=5)
check("全部任务终态", all(j["status"] == "done" for j in jobs),
      str([(j["kind"], j["status"], (j.get("error") or "")[:60]) for j in jobs
           if j["status"] != "done"]) or f"{len(jobs)} 个，用时 {time.time()-t0:.0f}s")

# ---------- 4. 断言 ----------
from services.common.qdrant import COLL_VISUAL, get_client  # noqa: E402

qc = get_client()


def visual_points(kb_id: int) -> list[dict]:
    res = qc.post(f"/collections/{COLL_VISUAL}/points/scroll", json={
        "limit": 100, "with_payload": True, "with_vector": False,
        "filter": {"must": [{"key": "kb_id", "match": {"value": kb_id}}]},
    }).json()["result"]
    return [{"page": p["payload"].get("page"),
             "chunk_index": p["payload"].get("chunk_index")} for p in res["points"]]


pp, sp = visual_points(photo_kb), visual_points(scan_kb)
check("照片库 kb_visual 有点", len(pp) == 1, str(pp))
check("扫描件库 kb_visual 有点（3 页 = 3 个点）", len(sp) == 3,
      f"{[p['page'] for p in sp]}")
if pp:
    check("页级点用负 chunk_index（对账靠它分腿）", pp[0]["chunk_index"] < 0, str(pp[0]))

d = c.get(f"/kb/{photo_kb}/documents", headers=h).json()[0]
check("照片文档状态回 ready（不再是永远 pending）", d["status"] == "ready",
      f"status={d['status']}")
d2 = c.get(f"/kb/{scan_kb}/documents", headers=h).json()[0]
check("扫描件文档状态回 ready", d2["status"] == "ready", f"status={d2['status']}")

rep = c.get("/admin/consistency", headers=h).json()
for coll in ("kb_dense", "kb_sparse", "kb_visual"):
    leg = rep.get("legs", {}).get(coll, {})
    check(f"对账 {coll} 腿一致", leg.get("consistent") is True,
          f"points={leg.get('points')} missing={leg.get('missing_count')} "
          f"dangling={leg.get('dangling_count')}")
check("对账顶层（文本腿并集）一致", rep.get("consistent") is True,
      f"pg={rep.get('pg_chunks')} qdrant={rep.get('qdrant_points')}")

r = qc.post(f"/collections/{COLL_VISUAL}/points/scroll", json={
    "limit": 1, "with_payload": False, "with_vector": True,
    "filter": {"must": [{"key": "kb_id", "match": {"value": photo_kb}}]},
})
v = r.json()["result"]["points"][0]["vector"]["text"]
check("点里存的是 (n_tokens, 128) 多向量", len(v) > 100 and len(v[0]) == 128,
      f"{len(v)}×{len(v[0])}")

# ---------- 5. 清场：自己造的数据自己删 ----------
# 🔑 不清的话每跑一次留一个用户 + 两个库 + 4 个文档。
#   —— 与 `eval/_reset.py` 同一个纪律：**验收脚本要自己清场**。
#
# ⚠️⚠️ 第一版这里只 `DELETE FROM users` + 清无引用 blob，结果**把巡检弄红了**：
#   用户被 CASCADE 删掉 → PG 里没有那些文档了，可
#     ① Qdrant 三条腿的点还在（视觉腿报「多 4」），
#     ② BM25 的 df 还留着那一份（`n_docs=498 vs chunk 行 497`）。
#   两者都是**静默**的：脚本自己照样全绿，下一轮 monitor 才红，
#   而看到红的人会以为是"视觉腿又坏了"。
#
#   ✅ 正确形状：清场必须走**唯一**的删除链 —— `handle_reindex`
#   （D124 已把孤儿清理扩到三条腿）。**不要**在这里自己写第二套清理逻辑
#   （§0.3 检索只有一个实现 / §5.29 删除链只有一份）。
print("\n清场（删用户 → 走 handle_reindex 清三腿孤儿点与 df）…", flush=True)
from sqlalchemy import text as _sql  # noqa: E402

from services.common.db import sync_session_scope  # noqa: E402
from services.worker.handlers import handle_reindex  # noqa: E402

# ⚠️ 必须**先**记下本次文档的 blob sha：删完用户就查不到它们了。
#   而只删 `blobs` 行会**把文件留在磁盘上**（blob 没有反向外键指向
#   documents，CASCADE 够不到文件；实测跑完脚本后 data/blobs 里
#   留下一堆没有行的目录）。所以顺序是：记 sha → 删行 → 删文件。
from services.common.storage import blob_path, delete_blob  # noqa: E402

with sync_session_scope() as s:
    mine = set(s.execute(_sql(
        "SELECT DISTINCT d.blob_sha FROM documents d "
        "JOIN knowledge_bases k ON k.id = d.kb_id "
        "JOIN users u ON u.id = k.created_by "
        "WHERE u.email LIKE :p AND d.blob_sha IS NOT NULL"
    ), {"p": f"vdr-{STAMP}%"}).scalars().all())
    s.execute(_sql("DELETE FROM users WHERE email LIKE :p"), {"p": f"vdr-{STAMP}%"})
    n_row = s.execute(_sql(
        "DELETE FROM blobs WHERE sha256 NOT IN (SELECT blob_sha FROM documents)"
    )).rowcount
print(f"  已删用户、无引用 blob 行 {n_row} 个；删文件…", flush=True)

n_file = 0
with sync_session_scope() as s:
    for sha in sorted(mine):
        still = s.execute(_sql(
            "SELECT 1 FROM documents WHERE blob_sha = :s LIMIT 1"
        ), {"s": sha}).first()
        if still or not blob_path(sha).exists():
            continue
        n_file += int(delete_blob(sha))
print(f"  已删 blob 文件 {n_file} 个（含预览图/缩略图）；清三腿孤儿点…", flush=True)

with sync_session_scope() as s:
    handle_reindex(s, doc_id=None, payload={}, progress=lambda _x: None)
    s.commit()
print("  清场完成（三条腿的点与 df 都已对齐）", flush=True)

print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
sys.exit(1 if fails else 0)
