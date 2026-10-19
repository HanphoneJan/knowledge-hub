"""D127 验证脚本：文档状态**永远到不了 `failed`** —— 端到端复现并验证修复。

用法（需 API 在跑；worker 由脚本**接管**）：

    cd ~/develop-projects/knowledge-hub
    PYTHONPATH=. uv run python deploy/verify_visual_failure.py

## 为什么要有这个脚本（而不是只靠 pytest）

`deploy/verify_visual_leg.py` 修好页渲染封顶之后**不再复现**这个症状
（它造的场景现在会成功），而它是**唯一**一个能在真 worker 上跑视觉腿的脚本。
所以要自己造一个**必然失败**的场景 —— 这里是：

    上传扫描件 → 摄取入队 2 个 embed_visual → **把 blob 文件移走**
    → 每个视觉任务都真的失败（`blob 缺失`，不是超时）

⚠️ **不要用"超时"当失败原因**：那是症状不是原因。移走 blob 文件
   得到的是真实错误（`handle_visual` 第一步就查它），于是 `status_msg`
   能给出可核对的原因，用户点「重试」也知道在重试什么。

## 为什么脚本要**接管 worker**

视觉任务是**异步**的：上传返回 202 之后由 worker 慢慢跑。
要让"什么时候失败"完全确定，队列必须只有一个消费者。所以脚本会：

1. 等队列排空（worker 正常跑着）
2. **停掉 worker**，然后在**本进程**里调 `services.worker.main.run_once()`
   —— 那是 worker 主循环的循环体，走的是同一份真实代码路径
   （真实 handler、真实 Qdrant、真实任务状态流转）
3. 结尾**把 worker 重新拉起来**（与 `deploy/dev.sh` 相同的命令），
   然后跑 `monitor.py` 自检：清场不干净的话，**做这件事的脚本自己会红**

⚠️ 停下来之前必须等队列排空：队列里排着的别人的任务会被本进程处理掉。
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select, text as sql_text

API = "http://127.0.0.1:8010/api/v1"
ROOT = Path(__file__).resolve().parents[1]

fails = 0
worker_stopped = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    global fails
    print(f"{'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""),
          flush=True)
    if not ok:
        fails += 1
    return ok


# ============================================================
# worker 接管
# ============================================================


def _worker_pids() -> list[int]:
    out = subprocess.run(
        ["ps", "-eo", "pid,args"], capture_output=True, text=True,
    ).stdout
    pids = []
    for line in out.splitlines():
        if ".venv/bin/python3 -u -m services.worker.main" in line:
            pids.append(int(line.split()[0]))
    return pids


def queue_busy() -> int:
    from services.common.db import sync_session_scope
    from services.common.models import Job

    with sync_session_scope() as s:
        return int(s.execute(
            select(func.count()).select_from(Job)
            .where(Job.status.in_(("pending", "running")))
        ).scalar_one())


def stop_worker() -> None:
    """停掉真实 worker（只停**执行代码的那个** python，见 `dev.sh` 的坑 #3）。"""
    global worker_stopped
    for _ in range(20):
        if queue_busy() == 0:
            break
        time.sleep(1)
    busy = queue_busy()
    if busy:
        raise SystemExit(
            f"队列里还有 {busy} 个未完成任务 —— 本脚本会在本进程里处理它们，"
            "那不是你以为在做的事。先等 worker 排空再跑（`/jobs?status=pending`）。"
        )
    pids = _worker_pids()
    if not pids:
        print("worker 本来就没在跑（继续）", flush=True)
        return
    for p in pids:
        os.kill(p, signal.SIGTERM)
    for _ in range(30):
        if not _worker_pids():
            break
        time.sleep(1)
    if _worker_pids():
        for p in _worker_pids():
            os.kill(p, signal.SIGKILL)
        time.sleep(1)
    worker_stopped = True
    print(f"已停 worker（原 pid {pids}），本进程接管队列", flush=True)


def start_worker() -> None:
    """与 `deploy/dev.sh` 相同的启动方式（日志同一份文件）。"""
    if not worker_stopped or _worker_pids():
        return
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    subprocess.Popen(
        ["uv", "run", "python", "-u", "-m", "services.worker.main"],
        cwd=ROOT, env=env, stdout=open("/tmp/pkh_worker.log", "ab"),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        if _worker_pids():
            print("worker 已重新拉起", flush=True)
            return
        time.sleep(1)
    print("⚠️ worker 没起来，看 /tmp/pkh_worker.log", flush=True)


def drain(max_rounds: int = 400) -> int:
    """在**本进程**里跑 worker 主循环，直到没有任务可领。返回处理了几个。

    ⚠️ 必须先 `enable_visual(True)`：`services/worker/main.py::main()` 才会开
    那个开关（D50：colSmol 只许在 worker 进程里加载），而这里直接调
    `run_once()`，跳过了 `main()`。
    不开的话每个视觉任务都会失败于「视觉模型未启用」——
    那个失败**恰好**是本脚本要验的症状之一，但恢复那一步会假红，
    而"因为开关没开所以失败"与"因为 blob 缺失所以失败"必须分得清。
    """
    from services.retrieval.pool import enable_visual
    from services.worker.main import run_once

    enable_visual(True)
    n = 0
    for _ in range(max_rounds):
        if not run_once():
            break
        n += 1
    return n


def doc_row(doc_id: int):
    from services.common.db import sync_session_scope
    from services.common.models import Document

    with sync_session_scope() as s:
        doc = s.get(Document, doc_id)
        return None if doc is None else (
            doc.id, doc.status, doc.status_msg, doc.scan_pages, doc.blob_sha
        )


def jobs_of(doc_id: int) -> list[tuple]:
    from services.common.db import sync_session_scope
    from services.common.models import Job

    with sync_session_scope() as s:
        return [
            (j.id, j.kind, j.status, j.payload, (j.error or "")[:80])
            for j in s.execute(
                select(Job).where(Job.doc_id == doc_id).order_by(Job.id)
            ).scalars().all()
        ]


# ============================================================
# 素材：自己造，**不**从库里挑（§5.110）
# ============================================================


def make_scanned_pdf(n_pages: int = 2) -> bytes:
    """`n_pages` 页**没有文本层**的 PDF（就是扫描件）。

    ⚠️ 素材**不**从 blob 里挑：`deploy/verify_visual_leg.py` 第一版就是
    「按 sorted() 取第一个 PDF」，于是它把自己上一轮的产物当素材，
    页面尺寸每轮翻倍，最后被 pymupdf 顶死（PROGRESS §5.110）。
    这里用 PIL 现画 —— 没有外部依赖，也就没有自喂的可能。
    """
    import pymupdf
    from PIL import Image, ImageDraw

    out = pymupdf.open()
    try:
        for i in range(n_pages):
            img = Image.new("RGB", (900, 1200), "white")
            d = ImageDraw.Draw(img)
            d.rectangle([60, 60, 840, 240], outline="black", width=3)
            for y in range(320, 1100, 60):
                d.line([60, y, 840 - (i + 1) * 60, y], fill="black", width=2)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            page = out.new_page(width=900, height=1200)
            page.insert_image(pymupdf.Rect(0, 0, 900, 1200),
                              stream=buf.getvalue())
        return out.tobytes()
    finally:
        out.close()


def make_broken_pdf() -> bytes:
    """扩展名对、内容彻底坏掉的文件（D38「文件完全损坏」那一条）。"""
    return b"%PDF-1.4\n" + b"this is not a pdf at all\n" * 20


# ============================================================
# 跑起来
# ============================================================

c = httpx.Client(base_url=API, timeout=httpx.Timeout(120.0, connect=5.0),
                 trust_env=False)
STAMP = time.strftime("%H%M%S")
shas: set[str] = set()

stop_worker()

r = c.post("/auth/register", json={
    "email": f"d127-{STAMP}@t.local", "display_name": "D127 终态验证",
    "password": "pw12345678",
})
if r.status_code not in (200, 201):
    raise SystemExit(f"注册失败 {r.status_code} {r.text[:200]}")
h = {"Authorization": f"Bearer {r.json()['api_key']}"}
ws_id = c.get("/workspaces", headers=h).json()[0]["id"]
scan_kb = c.post("/kb", headers=h, json={
    "workspace_id": ws_id, "name": f"D127 扫描件库 {STAMP}", "source_kind": "file",
}).json()["id"]
check("注册 + 建库", bool(scan_kb), f"kb={scan_kb}")

# ---------- ① 视觉任务全失败 → 必须转 failed ----------
print("\n① 扫描件：移走 blob → 视觉任务必然失败", flush=True)
r = c.post(f"/kb/{scan_kb}/documents", headers=h, files={
    "files": (f"扫描件 {STAMP}.pdf", make_scanned_pdf(2), "application/pdf")})
check("上传 2 页扫描件 → 202", r.status_code == 202, f"HTTP {r.status_code}")
scan_doc = r.json()["documents"][0]["id"]

# 🔑 只跑**一条**任务（摄取），不跑视觉任务 ——
#   否则视觉任务在下面"移走 blob"之前就已经跑完了（每页 15s，跑的是真的）。
#   队列在 `stop_worker()` 里验过是空的，所以这一条必然是刚上传那份的摄取。
drain(max_rounds=1)
row = doc_row(scan_doc)
shas.add(row[4])
check("摄取完成后是 pending（有视觉任务在排队 = 那个承诺）",
      row[1] == "pending", f"status={row[1]} scan_pages={row[3]}")
vis = [j for j in jobs_of(scan_doc) if j[1] == "embed_visual"]
check("派了 2 个 embed_visual（每页一个）", len(vis) == 2,
      str([(j[3], j[2]) for j in vis]))

from services.common.storage import blob_path  # noqa: E402

blob_file = blob_path(row[4])
hidden = blob_file.with_suffix(".hidden")
blob_file.rename(hidden)
try:
    drain()
finally:
    hidden.rename(blob_file)

vis = [j for j in jobs_of(scan_doc) if j[1] == "embed_visual"]
check("视觉任务全部终态失败（原因是 blob 缺失，不是超时）",
      all(j[2] == "failed" for j in vis) and vis
      and all("blob 缺失" in j[4] for j in vis),
      str([(j[3], j[2], j[4][:40]) for j in vis]))

row = doc_row(scan_doc)
check("🔑 文档转 failed（不再是永远 pending）", row[1] == "failed",
      f"status={row[1]}")
msg = row[2] or ""
failed_ids = [j[0] for j in vis if j[2] == "failed"]
check("status_msg 说清是哪些任务、什么原因",
      bool(msg) and all(str(i) in msg for i in failed_ids[:1]) and "blob 缺失" in msg,
      msg[:160])
check("status_msg 说清「应有几个页级点、实际有几个」",
      "页级点" in msg, msg[:120])

# ---------- ② /retry 从死代码变成活的 ----------
# ⚠️ 判据只能看**这次重试**的任务：`enqueue` 的去重只比未完成的任务，
#   所以上一次那两条 failed 会永远留在表里（PROGRESS §5.89 的同一形状）——
#   拿"这份文档所有 embed_visual 都 done"当判据，会把**正确的恢复**判成失败
#   （§5.114：判据指错性质，已经发生第三次了）。
before_retry = max(j[0] for j in jobs_of(scan_doc))
r = c.post(f"/documents/{scan_doc}/retry", headers=h)
check("POST /documents/{id}/retry 返回 202（D127 之前永远 400）",
      r.status_code == 202, f"HTTP {r.status_code} {r.text[:100]}")
drain()
row = doc_row(scan_doc)
fresh = [j for j in jobs_of(scan_doc)
         if j[1] == "embed_visual" and j[0] > before_retry]
check("重试后回到 ready，且这次重试的视觉任务全 done",
      row[1] == "ready" and len(fresh) == 2 and all(j[2] == "done" for j in fresh),
      f"status={row[1]} 本次任务={[j[2] for j in fresh]}")

from services.common.qdrant import COLL_VISUAL, get_client  # noqa: E402

qc = get_client()
res = qc.post(f"/collections/{COLL_VISUAL}/points/scroll", json={
    "limit": 100, "with_payload": True, "with_vector": False,
    "filter": {"must": [{"key": "kb_id", "match": {"value": scan_kb}}]},
}).json()["result"]
pages = sorted(p["payload"].get("page") for p in res["points"])
check("kb_visual 有 2 个页级点（恢复闭环真的建回来了）", pages == [1, 2], str(pages))

# ---------- ③ 摄取失败 → 也必须转 failed（同一个洞的另一半）----------
print("\n② 完全损坏的 PDF：摄取任务重试耗尽", flush=True)
r = c.post(f"/kb/{scan_kb}/documents", headers=h, files={
    "files": (f"坏文件 {STAMP}.pdf", make_broken_pdf(), "application/pdf")})
check("上传损坏 PDF → 202（上传时只查格式白名单，D38）",
      r.status_code == 202, f"HTTP {r.status_code}")
bad_doc = r.json()["documents"][0]["id"]
shas.add(doc_row(bad_doc)[4])
drain()

row = doc_row(bad_doc)
ing = [j for j in jobs_of(bad_doc) if j[1] == "ingest"]
check("摄取任务终态失败", ing and ing[-1][2] == "failed",
      str([(j[2], j[4][:60]) for j in ing]))
check("🔑 文档转 failed（解析失败不再是永远 parsing）", row[1] == "failed",
      f"status={row[1]}")
check("status_msg 给出原因", bool(row[2]) and str(ing[-1][0]) in (row[2] or ""),
      (row[2] or "")[:140])

# ---------- ④ 清场：自己造的数据自己删，而且要验证删干净了 ----------
print("\n③ 清场（删用户 → 删 blob 行 → 删 blob 文件 → 清三腿孤儿点与 df）", flush=True)
from services.common.db import sync_session_scope  # noqa: E402
from services.common.models import Document  # noqa: E402
from services.common.storage import delete_blob  # noqa: E402
from services.worker.handlers import handle_reindex  # noqa: E402

with sync_session_scope() as s:
    s.execute(sql_text("DELETE FROM users WHERE email LIKE :p"),
              {"p": f"d127-{STAMP}%"})
    # ⚠️ 只删**自己造的那几个** blob 行。写 `DELETE FROM blobs WHERE sha256 NOT IN
    #   (SELECT …)` 会连带删掉历史遗留的无引用行，而它们的**文件还在磁盘上**
    #   —— 那正是 §5.112 那个坑（删行不删文件 = 文件永久留下）。
    #   别人的垃圾不归我清；我只保证自己不留垃圾。
    for sha in sorted(shas):
        s.execute(sql_text(
            "DELETE FROM blobs WHERE sha256 = :s "
            "AND sha256 NOT IN (SELECT blob_sha FROM documents)"
        ), {"s": sha})
print(f"  已删用户；删自己那 {len(shas)} 个 blob 行；删文件…", flush=True)

n_file = 0
with sync_session_scope() as s:
    for sha in sorted(shas):
        if s.execute(sql_text(
            "SELECT 1 FROM documents WHERE blob_sha=:s LIMIT 1"), {"s": sha}
        ).first():
            continue
        if not blob_path(sha).exists():
            continue
        n_file += int(delete_blob(sha))
print(f"  已删 blob 文件 {n_file} 个", flush=True)

with sync_session_scope() as s:
    handle_reindex(s, doc_id=None, payload={}, progress=lambda _x: None)
    s.commit()

with sync_session_scope() as s:
    left_docs = int(s.execute(sql_text(
        "SELECT count(*) FROM documents d JOIN knowledge_bases k ON k.id=d.kb_id "
        "JOIN users u ON u.id=k.created_by WHERE u.email LIKE :p"
    ), {"p": f"d127-{STAMP}%"}).scalar_one())
    left_files = sum(1 for sha in shas if blob_path(sha).exists())
check("PG 里没有残留文档", left_docs == 0, f"{left_docs} 行")
check("磁盘上没有残留 blob 文件（只删行会把文件永久留下，§5.112）",
      left_files == 0, f"{left_files} 个")

start_worker()

p = subprocess.run([sys.executable, "deploy/monitor.py", "--json"],
                   cwd=ROOT, capture_output=True, text=True,
                   env=dict(os.environ, PYTHONPATH=str(ROOT)))
rep = json.loads(p.stdout or "{}")
# ⚠️ 先确认 monitor 自己跑起来了：它崩掉时 stdout 是空的，
#   而"空报告 = 没有 fail"会让这一项**恒绿**（§5.112 那个形状）。
check("monitor 自己跑起来了（空报告不算通过）", bool(rep.get("checks")),
      (p.stderr or p.stdout)[-120:])
bad = [c for c in rep.get("checks", []) if c["level"] == "fail" and not c["ok"]]
check("monitor 无 fail 项（清场没留下孤儿点 / df 漂移）", not bad,
      "；".join(f"{c['name']}：{c['detail'][:60]}" for c in bad))
for name in ("视觉腿对账（kb_visual）", "BM25 df 一致（检索质量）"):
    row_ = next((c for c in rep.get("checks", []) if c["name"] == name), None)
    if row_:
        check(f"{name}：{row_['detail']}", row_["ok"])

print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ {fails} 项失败'}")
print("提示：确认 worker 还在（`tail -20 /tmp/pkh_worker.log`；dev.sh 只有 start/restart/stop）")
sys.exit(1 if fails else 0)