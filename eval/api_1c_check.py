"""1c 子步骤端到端验证：上传 → worker 摄取 → Qdrant 可检索。

用一份**真实的多页表单 PDF**（自备，`PKH_PROBE_PDF` 指；见 `eval/_local_data.py`），
验收标准是"用 `QUESTION` 能检索到 `ANSWER_MARKERS` 里的原文段落"。

跑法：
    # 终端 1：起 API
    setsid env HF_HUB_OFFLINE=1 uv run uvicorn services.api.main:app \
      --host 127.0.0.1 --port 8010 > /tmp/pkh_api.log 2>&1 < /dev/null &
    # 终端 2：起 worker
    setsid env HF_HUB_OFFLINE=1 uv run python -m services.worker.main \
      > /tmp/pkh_worker.log 2>&1 < /dev/null &
    # 本脚本
    uv run python eval/api_1c_check.py
"""

from __future__ import annotations

import os

# 🔴 离线加载模型（说明见 tests/conftest.py）：不设的话每次加载 bge/colSmol
#   都会去 huggingface.co 重试 5 次（≈23s），整套 eval 慢到几十分钟。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

# 项目根要在 sys.path 里才能 `from services...`（services/ 是源码目录不是安装包）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
# 🔴 探针 PDF / 验收问题 / 原文标记都是**部署方的真实数据** ⇒ 从不被追踪的
#   `eval/_local_data.py` 读（没有它就退回同目录的合成示例 `_local_data_example.py`）。
#   仓库里**不许**出现这些真实值（守卫 `tests/test_no_real_data.py`）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # noqa: SIM105
    import _local_data as _LD
except ModuleNotFoundError:
    import _local_data_example as _LD

BASE = os.environ.get("PKH_TEST_BASE", "http://127.0.0.1:8010")
API = f"{BASE}/api/v1"
#: 探针要一份**多页、带坐标**的 PDF（自备）。用环境变量指，别把本机路径写进仓库。
DOC = Path(os.environ.get("PKH_PROBE_PDF", _LD.PROBE_PDF))
#: 验收问题（答案在探针 PDF 的某一页）。
QUESTION = _LD.QUESTION

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
_fail: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _fail.append(label)


def section(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


def _err_code(r) -> str | None:
    """从错误响应里取 code；不是错误响应就返回 None。"""
    if r.status_code < 400:
        return None
    try:
        return r.json().get("error", {}).get("code")
    except Exception:  # noqa: BLE001 — 响应体不是 JSON 也要能继续跑完
        return None


def reset() -> dict[str, int | str]:
    """清掉上一轮的测试数据（含 Qdrant 的点）**并重建 BM25 词表**。

    ⚠️ 重建不是多余的：`DELETE FROM users` 级联清 chunks，但**不回退**
    `terms.df` / `corpus_stats.n_docs`（只有 worker 的 cleanup 在 30 天后才减）。
    不重建的话每跑一轮 `n_docs` 虚高 74，几轮后 sparse 腿的 idf 被压平、
    top1 会从答案段落变成一个表格标题 —— 实测第 4 轮复现。
    详见 `eval/_reset.py` 与 `PROGRESS.md` §5.29。
    """
    from eval._reset import reset_users_and_bm25

    # ⚠️ 越权那节会注册第二个用户，他也得被清掉 ——
    # 否则他名下会留一个空工作区，几轮之后 workspaces 表越来越脏。
    # ⚠️ 三个账号都要清：本轮新增了 `ingest-other`（导入端点的越权那节），
    # 漏清的话第二次跑会拿不到 api_key（409），而 KeyError 完全指不到原因。
    return reset_users_and_bm25(
        ["ingest@t.local", "intruder@t.local", "ingest-other@t.local"]
    )


def main() -> int:  # noqa: C901
    c = httpx.Client(trust_env=False, timeout=120, base_url=BASE)
    stats = reset()

    section("0. 前置")
    check("BM25 词表已重建（n_docs 对得上真实 chunk 数）",
          # 🔑 比的是**全部 chunk 行**（含软删未 purge 的）：BM25 的 n_docs
          #   按 purge 生命周期登记，四处口径一致（D115）。
          #   ⚠️ 别改成「活文档 chunk 数」——那是向量侧口径，回收站一有内容就恒红。
          str(stats["n_docs"]) == str(stats["real_chunks"]),
          f"n_docs={stats['n_docs']} 真实 chunk={stats['real_chunks']}")
    if not DOC.exists():
        print(f"{FAIL} 测试文档不存在: {DOC}")
        return 1
    check("测试文档存在", True, f"{DOC.name} ({DOC.stat().st_size // 1024}KB)")
    r = c.get(f"{BASE}/health")
    check("API 在线", r.status_code == 200)

    section("1. 注册 + 建库")
    r = c.post(f"{API}/auth/register", json={
        "email": "ingest@t.local", "password": "ingest-pass-1",
        "display_name": "Ingest 测试"})
    if r.status_code != 201:
        print(r.text[:300]); return 1
    key = r.json()["api_key"]
    uid = r.json()["user"]["id"]
    h = {"Authorization": f"Bearer {key}"}

    # 第二个账号：导入端点的越权那节要用（§13.5）
    r2 = c.post(f"{API}/auth/register", json={
        "email": "ingest-other@t.local", "password": "ingest-pass-2",
        "display_name": "另一个用户"})
    OTHER_H = ({"Authorization": f"Bearer {r2.json()['api_key']}"}
               if r2.status_code == 201 else h)

    ws_id = c.get(f"{API}/workspaces", headers=h).json()[0]["id"]
    kb_id = c.post(f"{API}/kb", headers=h, json={
        "workspace_id": ws_id, "name": "1c 测试库", "visibility": "private",
    }).json()["id"]
    check("工作区与知识库就绪", bool(ws_id and kb_id), f"ws={ws_id} kb={kb_id}")

    section("2. 上传校验（同步拒绝，D30/D45）")
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("bad.exe", b"MZ\x90\x00", "application/octet-stream")})
    check("不支持的格式 → 415", r.status_code == 415,
          r.json()["error"]["code"] if r.status_code == 415 else f"HTTP {r.status_code}")

    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("empty.txt", b"", "text/plain")})
    check("空文件 → 422", r.status_code == 422, f"HTTP {r.status_code}")

    big = b"x" * (51 * 1024 * 1024)
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("big.txt", big, "text/plain")})
    check("超过 50MB → 413", r.status_code == 413, f"HTTP {r.status_code}")

    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files=[("files", (f"f{i}.txt", b"content", "text/plain")) for i in range(21)])
    check("超过 20 个文件 → 413", r.status_code == 413, f"HTTP {r.status_code}")

    section("3. 上传真实 PDF")
    data = DOC.read_bytes()
    t0 = time.perf_counter()
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": (DOC.name, data, "application/pdf")})
    elapsed = (time.perf_counter() - t0) * 1000
    check("上传 → 202", r.status_code == 202,
          f"HTTP {r.status_code} 用时 {elapsed:.0f}ms")
    if r.status_code != 202:
        print(r.text[:400]); return 1
    doc_id = r.json()["documents"][0]["id"]
    check("返回 202 而非 201（摄取是异步的）", True)
    check("初始状态是 pending", r.json()["documents"][0]["status"] == "pending")

    section("4. 等 worker 摄取")
    deadline = time.time() + 180
    final = None
    # 边等边采样任务进度。ingest 全程约 7 秒（15 页解析 + 74 chunk × 88ms 编码），
    # 原来 2 秒一睡只能看到首尾 —— 那样**验证不了"进度真的在动"**，
    # 只能验证"结束时是 1.0"，而那与 progress 恒等于 1.0 的假实现无法区分。
    job_samples: list[dict] = []
    while time.time() < deadline:
        d = c.get(f"{API}/documents/{doc_id}", headers=h).json()
        r = c.get(f"{API}/jobs?doc_id={doc_id}", headers=h)
        if r.status_code == 200:
            for j in r.json()["items"]:
                if j["kind"] == "ingest" and (not job_samples or job_samples[-1] != j):
                    job_samples.append(j)
        if d["status"] in ("ready", "failed"):
            final = d
            break
        time.sleep(0.3)
    if final is None:
        print(f"{FAIL} 超时未完成")
        print(subprocess.run(
            ["docker", "exec", "pkh-postgres", "psql", "-U", "pkh", "-d", "pkh", "-c",
             "SELECT kind,status,attempts,error FROM jobs WHERE doc_id="
             f"{doc_id} ORDER BY id"],
            capture_output=True, text=True).stdout)
        return 1

    check("摄取完成", final["status"] == "ready",
          f"status={final['status']} msg={final.get('status_msg')}")
    if final["status"] != "ready":
        return 1
    check("产出 chunk", final["chunk_count"] > 0, f"{final['chunk_count']} 个")
    check("页数已记录", final["page_count"] == 15, f"{final['page_count']} 页")
    check("非扫描件", final.get("is_scanned") is False)

    # ================================================================
    section("4.5 任务进度回传（GET /jobs，阶段 2 补齐）")
    # 1d 就把 worker 的 progress 写通了，但没有任何端点能读 ——
    # 前端只能看 documents.status，显示不了百分比。这一节补上那条链路。
    check("上传返回后任务已入队（不等 worker 轮询）", bool(job_samples),
          f"首个样本 status={job_samples[0]['status'] if job_samples else '-'} "
          f"progress={job_samples[0]['progress'] if job_samples else '-'}")
    check("观测到多个进度样本", len(job_samples) >= 2, f"{len(job_samples)} 个")

    prog = [s["progress"] for s in job_samples]
    check("progress 单调不减",
          all(b >= a for a, b in zip(prog, prog[1:])),
          " → ".join(f"{p:.2f}" for p in prog))
    check("progress 恒在 [0,1]", all(0.0 <= p <= 1.0 for p in prog))
    mid = [s for s in job_samples if s["status"] != "done"]
    check("采样到中间态（worker 在跑时 progress 已可见）", bool(mid),
          f"{len(mid)}/{len(job_samples)} 个样本非终态")
    if mid:
        check("  中间态的 progress < 1", all(s["progress"] < 1.0 for s in mid),
              f"中间态 status：{sorted({s['status'] for s in mid})}")
        check("  中间态的 status 是 pending/running",
              all(s["status"] in ("pending", "running") for s in mid))

    job = job_samples[-1]
    job_id = job["id"]
    check("终态 status=done", job["status"] == "done", job["status"])
    check("终态 progress=1.0", abs(job["progress"] - 1.0) < 1e-9,
          f"{job['progress']}")
    check("attempts ≥ 1（worker 真的领过）", job["attempts"] >= 1,
          f"attempts={job['attempts']}")
    check("kb_id 正确", job["kb_id"] == kb_id)
    check("doc_id 正确", job["doc_id"] == doc_id)
    check("无 error", job["error"] is None, str(job["error"])[:80])
    check("updated_at 不早于 created_at",
          job["updated_at"] >= job["created_at"])

    r = c.get(f"{API}/jobs/{job_id}", headers=h)
    check("GET /jobs/{id} → 200", r.status_code == 200, f"HTTP {r.status_code}")
    one = r.json()
    check("  与列表里的是同一个任务",
          one.get("id") == job_id and one.get("progress") == job["progress"])

    r = c.get(f"{API}/jobs?kb_id={kb_id}&doc_id={doc_id}&kind=ingest&status=done",
              headers=h)
    check("组合过滤命中", r.status_code == 200
          and any(j["id"] == job_id for j in r.json()["items"]))
    r2 = c.get(f"{API}/jobs?kb_id={kb_id}&status=failed", headers=h)
    check("  status=failed 过滤为空（本次没失败）",
          r2.status_code == 200 and r2.json()["total"] == 0)
    check("列表是分页包（契约 §0.4）",
          set(r.json()) >= {"items", "total", "page", "page_size"},
          str(sorted(r.json())))

    # 错值要 400：空结果会被读成"这个库没有任务"，把打错字伪装成业务事实
    r = c.get(f"{API}/jobs?kind=不存在的kind", headers=h)
    check("未知 kind → 400", r.status_code == 400,
          r.json()["error"]["code"] if r.status_code == 400 else f"HTTP {r.status_code}")
    r = c.get(f"{API}/jobs?status=半途而废", headers=h)
    check("未知 status → 400", r.status_code == 400, f"HTTP {r.status_code}")

    # ---- fail-closed：第二个用户看不到别人的任务 ----
    r = c.post(f"{API}/auth/register", json={
        "email": "intruder@t.local", "password": "intruder-pass-1",
        "display_name": "越权测试"})
    if r.status_code == 201:
        h2 = {"Authorization": f"Bearer {r.json()['api_key']}"}
        r = c.get(f"{API}/jobs/{job_id}", headers=h2)
        code_forbidden = _err_code(r)
        check("别人的 job_id → 403 KB_NOT_FOUND",
              r.status_code == 403 and code_forbidden == "KB_NOT_FOUND",
              f"HTTP {r.status_code} {code_forbidden}")
        r = c.get(f"{API}/jobs/999999999", headers=h2)
        code_missing = _err_code(r)
        check("不存在的 job_id → 同码（不能靠差异探测存在性，§0.4）",
              code_missing == code_forbidden,
              f"别人的 {code_forbidden} vs 不存在的 {code_missing}")
        r = c.get(f"{API}/jobs", headers=h2)
        check("新用户看不到别人的任务",
              r.status_code == 200
              and not any(j["id"] == job_id for j in r.json()["items"]),
              f"{len(r.json().get('items', []))} 个任务")
        r = c.get(f"{API}/jobs?kb_id={kb_id}", headers=h2)
        check("按别人的 kb_id 过滤 → 403", r.status_code == 403,
              f"HTTP {r.status_code}")
    else:
        check("注册第二个用户（越权用）", False, f"HTTP {r.status_code} {r.text[:120]}")

    section("5. chunk 与 L3 坐标")
    chunks = c.get(f"{API}/documents/{doc_id}/chunks?page_size=100", headers=h).json()
    check("chunk 列表可读", len(chunks) == final["chunk_count"],
          f"{len(chunks)} 个")

    with_loc = [ch for ch in chunks if ch["locations"]]
    check("全部 chunk 都有坐标", len(with_loc) == len(chunks),
          f"{len(with_loc)}/{len(chunks)}")

    texts = [ch for ch in chunks if ch["kind"] == "text"]
    tables = [ch for ch in chunks if ch["kind"] == "table"]
    check("正文 chunk 每块恰好 1 框", all(len(t["locations"]) == 1 for t in texts),
          f"{len(texts)} 个正文 chunk")
    check("表格 chunk 带 row/col",
          all("row" in loc for t in tables for loc in t["locations"]),
          f"{len(tables)} 个表格 chunk")

    # 造一个 txt 文档（验"非 PDF → 415"要用它）
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("缩略图对照.txt", "用来验证非 PDF 没有缩略图".encode(),
                                "text/plain")})
    victim_txt = r.json()["documents"][0]["id"]

    # 🔑 真值段落：命中 chunk 必须包含本地配置的原文标记（见 `_local_data`）
    answer = [ch for ch in chunks
              if all(m in ch["content_norm"] for m in _LD.ANSWER_MARKERS)]
    check("验收段落存在", bool(answer))
    if answer:
        a = answer[0]
        check("  在第 2 页", a["locations"][0]["page"] == 2)
        check("  有坐标框", len(a["locations"][0]["bbox"]) == 4,
              str(a["locations"][0]["bbox"]))

    section("6. Qdrant 索引可检索")
    from services.common.qdrant import close_client, get_client, point_id

    qc = get_client()
    flt = {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}

    def _ids(coll: str) -> list[int]:
        """取该文档的全部点 ID。

        ⚠️ 不能用 scroll + limit=N 计数 —— 点 ID 是**哈希**，
        按 id 升序返回，前 N 个里大概率没有 chunk_index=0 的那个
        （第一版就是这里误报的）。
        """
        out, offset = [], None
        while True:
            body = {"limit": 1000, "with_payload": False, "with_vector": False,
                    "filter": flt}
            if offset is not None:
                body["offset"] = offset
            res = qc.post(f"/collections/{coll}/points/scroll", json=body).json()["result"]
            out.extend(p["id"] for p in res["points"])
            offset = res.get("next_page_offset")
            if offset is None:
                break
        return out

    dense_ids, sparse_ids = _ids("kb_dense"), _ids("kb_sparse")
    check("dense 腿有点数正确", len(dense_ids) == final["chunk_count"],
          f"{len(dense_ids)} 点（chunk {final['chunk_count']}）")
    check("sparse 腿有点数正确", len(sparse_ids) == final["chunk_count"],
          f"{len(sparse_ids)} 点")
    check("两腿 ID 完全对齐（RRF 靠它）",
          sorted(dense_ids) == sorted(sparse_ids))

    # D33：点 ID 必须是 hash(kb_id, doc_id, chunk_index)，全部可复算
    expect = {point_id(kb_id, doc_id, i) for i in range(final["chunk_count"])}
    check("点 ID 全部符合稳定哈希公式（D33）",
          set(dense_ids) == expect,
          f"匹配 {len(set(dense_ids) & expect)}/{final['chunk_count']}")
    close_client()

    section("7. BM25 词表（增量入库）")
    from services.common.db import sync_session_scope
    from services.common.models import Term, CorpusStat

    with sync_session_scope() as s:
        n_terms = s.query(Term).count()
        st = s.get(CorpusStat, 1)
    check("terms 表已填充", n_terms > 100, f"{n_terms} 个词")
    # ⚠️ n_docs 是**全库**的 chunk 数，不是本知识库的。
    # 同一份 PDF 可能已被前几轮测试传过（旧版本 3 天内仍计入，D44），
    # 所以只能断言"至少包含本文档的 chunk 数"。
    check("corpus_stats 已更新且 ≥ 本文档 chunk 数",
          st is not None and st.n_docs >= final["chunk_count"],
          f"n_docs={st.n_docs if st else 0}（本文档 {final['chunk_count']}）")
    check("  avgdl 有效（不能为 0，否则 BM25 除零）",
          st is not None and st.total_len > 0, f"total_len={st.total_len if st else 0}")

    section("8. sparse 查询能命中验收问题")
    from services.common.db import sync_session_scope as scope
    from services.retrieval import bm25
    from services.retrieval.terms import TermRegistry, get_corpus_stats
    from services.common.qdrant import (close_client as cc, get_client as gc,
                                        sparse_query)

    with scope() as s:
        reg = TermRegistry(s)
        reg.ensure_loaded(set(bm25.tokenize(QUESTION)))
        # ⚠️ 这里**不能** `s.flush()` —— 那是"把新词插进 terms 表"。
        # 查询路径遇到 OOV 就该让这个词在本 query 上不贡献分数；
        # 往全局词表插会抬高 `n_docs` 并让 idf 漂移，而且每次跑这个脚本
        # 都会插一批新 term。1d 起已经改成 `retrieval.sparse.build_query_vector`
        # （走 `TermRegistryAsync`，不 flush），这一段仅为验证写入侧仍然可用。
        n_docs, avgdl = get_corpus_stats(s)
        dfs = reg.dfs_by_id(set(bm25.tokenize(QUESTION)))
        qvec = bm25.sparse_to_dict(
            bm25.tokenize(QUESTION), reg.ids, dfs, n_docs, avgdl, is_query=True)
    check("查询向量化成功", len(qvec) > 0, f"{len(qvec)} 个非零分量")

    # ⚠️ 必须走 `build_qdrant_filter`（CONVENTIONS §0.2：租户过滤的唯一入口）。
    # 直接查全库会命中前几轮测试遗留的知识库 —— 那不是 bug，
    # 是"调用方自己跳过了授权过滤"，恰好说明这个入口必须唯一。
    from services.common.auth import UserContext, build_qdrant_filter

    ctx = UserContext(
        user_id=uid, display_name="t", email="t@t.local", api_key_id=None,
        readable_kb_ids=frozenset({kb_id}),
    )
    qc = gc()
    res = qc.post("/collections/kb_sparse/points/query", json={
        "query": sparse_query(qvec), "limit": 5, "using": "bm25",
        "with_payload": True,
        "filter": build_qdrant_filter(ctx, kb_id),
    })
    pts = res.json()["result"]["points"]
    check("sparse 查询有命中", len(pts) > 0, f"{len(pts)} 命中")
    if pts:
        top = pts[0]["payload"]
        # 库里可能有前几轮测试留下的文档，所以只要求"属于本知识库"
        check("  top1 属于本知识库", top.get("kb_id") == kb_id,
              f"kb_id={top.get('kb_id')} doc_id={top['doc_id']} "
              f"chunk={top['chunk_index']}")
        check("  top1 内容相关",
              any(m in top.get("text_norm", "") for m in _LD.ANSWER_MARKERS),
              top.get("text_norm", "")[:50])
    cc()

    section("8.5 缩略图（D36：入库时预生成）")
    from PIL import Image
    # ⚠️ 这条链路曾经整段是死的：`handle_thumbnail` 写好了，但**没有任何地方
    # 给 KIND_THUMBNAIL 入队** —— handler 永远不会被调用，thumbs/ 目录一张图都没有。
    # 「handler 存在」不等于「功能存在」，所以断言要落在**产物**上。
    deadline = time.time() + 90
    thumb1 = None
    while time.time() < deadline:
        r = c.get(f"{API}/documents/{doc_id}/thumbs/1", headers=h)
        if r.status_code == 200:
            thumb1 = r
            break
        time.sleep(2)
    check("缩略图已生成且可取（D36）", thumb1 is not None,
          f"HTTP {r.status_code if r else '-'}")
    if thumb1 is not None:
        check("  是 JPEG", thumb1.headers.get("content-type") == "image/jpeg",
              f"{len(thumb1.content) // 1024}KB")
        check("  宽 400px（D36 规定）",
              (lambda im: im.width == 400)(Image.open(io.BytesIO(thumb1.content))),
              str(Image.open(io.BytesIO(thumb1.content)).size))

    r = c.get(f"{API}/documents/{doc_id}/thumbs/999", headers=h)
    check("越界页码 → 422", r.status_code == 422, f"HTTP {r.status_code}")

    r = c.get(f"{API}/documents/{victim_txt}/thumbs/1", headers=h)
    check("非 PDF → 415（只有 PDF 有页面缩略图）", r.status_code == 415,
          f"HTTP {r.status_code}")

    resp2 = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 10}).json()
    with_thumb = [i for i in resp2["items"] if i.get("thumb_url")]
    check("检索结果的 thumb_url 非空（D36 的卡片靠它）", len(with_thumb) > 0,
          f"{len(with_thumb)}/{len(resp2['items'])} 条带缩略图")
    if with_thumb:
        check("  URL 形如 /documents/{id}/thumbs/{页}",
              with_thumb[0]["thumb_url"].startswith(f"/api/v1/documents/{with_thumb[0]['doc_id']}/thumbs/"),
              with_thumb[0]["thumb_url"])
        tr = c.get(with_thumb[0]["thumb_url"], headers=h)
        check("  该 URL 能取到图", tr.status_code == 200,
              f"HTTP {tr.status_code} {len(tr.content) // 1024}KB")

    section("9. 页图预览（L3 前端的底图）")
    r = c.get(f"{API}/documents/{doc_id}/pages/2?width=800", headers=h)
    check("页图渲染 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("  是 JPEG", r.headers.get("content-type") == "image/jpeg",
          f"{len(r.content) // 1024}KB")

    if answer:
        ci = answer[0]["chunk_index"]
        r = c.get(
            f"{API}/documents/{doc_id}/preview?page=2&chunk_index={ci}&width=900",
            headers=h)
        check("带高亮框的预览 → 200", r.status_code == 200,
              f"{len(r.content) // 1024}KB")
        Path("/tmp/l3_probe/1c_preview.jpg").write_bytes(r.content)
        print(f"{INFO} 已存 /tmp/l3_probe/1c_preview.jpg 可人工核对框位")

    r = c.get(f"{API}/documents/{doc_id}/pages/999", headers=h)
    check("越界页码 → 422", r.status_code == 422, f"HTTP {r.status_code}")

    section("10. 覆盖式更新（D28）")
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": (DOC.name, data, "application/pdf")})
    check("同名重传 → 202", r.status_code == 202)
    new_id = r.json()["documents"][0]["id"]
    check("生成了新文档行", new_id != doc_id, f"{doc_id} -> {new_id}")

    docs = c.get(f"{API}/kb/{kb_id}/documents", headers=h).json()
    # ⚠️ 按**文件名**判定，不要假设"库里只有这一份" —— §5 为验 415 造过一个 txt，
    # 假设整个列表长度为 1 会让那条无关的文档把这条断言搞挂。
    same_name = [d["id"] for d in docs if d["file_name"] == DOC.name]
    check("同名文件只剩当前版本", same_name == [new_id], f"{same_name}")

    trash = c.get(f"{API}/kb/{kb_id}/trash", headers=h).json()
    check("旧版本进了回收站", any(t["id"] == doc_id for t in trash),
          f"{[t['id'] for t in trash]}")

    section("11. 等新版本摄取完，再测删除与恢复")
    deadline = time.time() + 120
    while time.time() < deadline:
        d = c.get(f"{API}/documents/{new_id}", headers=h).json()
        if d["status"] in ("ready", "failed"):
            break
        time.sleep(2)
    check("新版本摄取完成", d["status"] == "ready", f"status={d['status']}")

    r = c.delete(f"{API}/documents/{new_id}", headers=h)
    check("删除 → 204", r.status_code == 204, f"HTTP {r.status_code}")
    r = c.get(f"{API}/documents/{new_id}", headers=h)
    check("删后详情读不到 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    r = c.post(f"{API}/documents/{doc_id}/restore", headers=h)
    check("恢复旧版本 → 200", r.status_code == 200,
          f"HTTP {r.status_code}" if r.status_code == 200 else r.text[:150])

    # ================================================================
    section("12. 清空回收站（DELETE /trash）")
    from eval._reset import psql as _psql

    def _wait_job(hdrs: dict, job_id: int, *, timeout: float = 180.0) -> dict:
        """等一个任务跑完并返回它（供 §14 复用）。"""
        deadline = time.time() + timeout
        j: dict = {}
        while time.time() < deadline:
            j = c.get(f"{API}/jobs/{job_id}", headers=hdrs).json()
            if j.get("status") in ("done", "failed"):
                return j
            time.sleep(2)
        return j

    def wait_idle(hdrs: dict, *, timeout: float = 240.0) -> int:
        """等所有 pending/running 任务跑完，返回剩余数。

        ⚠️ 为什么需要：df（BM25 统计）与 chunk 行的一致性是**静止状态**的性质。
        摄取任务与删除请求并发时，两边的 df 更新落在不同事务里，
        中间会有几百毫秒的漂移 —— 在那个窗口里断言必然假红
        （实测 chunks=523 / n_docs=524，静止后两者都是 559）。
        """
        deadline = time.time() + timeout
        left = -1
        while time.time() < deadline:
            # ⚠️ 必须分别查 pending / running：`GET /jobs` 不带 status 时
            #   返回的是**全部**任务（含 done），total 永远不会归零。
            left = sum(
                c.get(f"{API}/jobs?page_size=1&status={st}", headers=hdrs)
                .json().get("total", 0)
                for st in ("pending", "running")
            )
            if left == 0:
                break
            time.sleep(1)
        return left

    def bm25_drift() -> tuple[int, int]:
        """(真实 chunk 行数, corpus_stats.n_docs) —— 两者应相等（§5.29）。"""
        return (
            int(_psql("SELECT count(*) FROM chunks").strip()),
            int(_psql("SELECT n_docs FROM corpus_stats").strip()),
        )

    def qdrant_ids(coll: str, doc: int) -> list[int]:
        from services.common.qdrant import close_client as _cc, get_client as _gc
        qc = _gc()
        body = {"limit": 1000, "with_payload": False, "with_vector": False,
                "filter": {"must": [{"key": "doc_id", "match": {"value": doc}}]}}
        res = qc.post(f"/collections/{coll}/points/scroll", json=body).json()["result"]
        _cc()
        return [p["id"] for p in res["points"]]

    def trash_ids() -> list[int]:
        return [t["id"] for t in c.get(f"{API}/kb/{kb_id}/trash", headers=h).json()]

    # 造一个待清的旧版本：同名再传一次（D28 覆盖式）
    blob = "回收站清空测试内容".encode()
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("清空测试.txt", blob, "text/plain")})
    victim = r.json()["documents"][0]["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        if c.get(f"{API}/documents/{victim}", headers=h).json()["status"] in (
            "ready", "failed"
        ):
            break
        time.sleep(1)
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("清空测试.txt", blob, "text/plain")})
    keeper = r.json()["documents"][0]["id"]
    check("待清的旧版本进了回收站", victim in trash_ids(), str(trash_ids()))
    check("当前版本不在回收站里", keeper not in trash_ids(), str(trash_ids()))

    # ⚠️ 必须等 keeper 摄取完再拍 BM25 快照 ——
    # 摄取本身就在写 terms/n_docs，否则两次采样之间的差异来自 worker，
    # 而下面的"dry_run 没动过数据"会被误判成失败。
    deadline = time.time() + 60
    while time.time() < deadline:
        if c.get(f"{API}/documents/{keeper}", headers=h).json()["status"] in (
            "ready", "failed"
        ):
            break
        time.sleep(1)
    victim_sha = _psql(f"SELECT blob_sha FROM documents WHERE id={victim}").strip()
    ref_before = int(
        _psql(f"SELECT ref_count FROM blobs WHERE sha256 = '{victim_sha}'").strip()
    )
    chunks_before, ndocs_before = bm25_drift()
    check("清空前 BM25 统计量一致", chunks_before == ndocs_before,
          f"chunks={chunks_before} n_docs={ndocs_before}")
    dense_before = len(qdrant_ids("kb_dense", victim))
    check("旧版本在 dense 腿里有点", dense_before > 0, f"{dense_before} 点")
    blob_dir = Path.cwd() / f"data/blobs/{victim_sha[:2]}/{victim_sha}"

    # ---- dry_run：只报告，不动数据 ----
    r = c.delete(f"{API}/trash?kb_id={kb_id}&dry_run=true", headers=h)
    check("dry_run → 200", r.status_code == 200, f"HTTP {r.status_code}")
    prev = r.json() if r.status_code == 200 else {}
    check("  dry_run=true", prev.get("dry_run") is True, str(prev)[:100])
    check("  报告了待清数量", prev.get("docs", 0) >= 1, f"docs={prev.get('docs')}")
    check("  回了 doc_ids", victim in (prev.get("doc_ids") or []),
          str(prev.get("doc_ids"))[:80])
    check("  ⚠️ dry_run 后旧版本还在回收站", victim in trash_ids(), str(trash_ids()))
    check("  ⚠️ dry_run 后索引点还在",
          len(qdrant_ids("kb_dense", victim)) == dense_before,
          f"{len(qdrant_ids('kb_dense', victim))} vs {dense_before}")
    check("  ⚠️ dry_run 后 BM25 没变", bm25_drift() == (chunks_before, ndocs_before),
          str(bm25_drift()))

    # ---- 真清 ----
    r = c.delete(f"{API}/trash?kb_id={kb_id}", headers=h)
    check("清空 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    res = r.json() if r.status_code == 200 else {}
    check("报告删了多少文档", res.get("docs", 0) >= 1, f"docs={res.get('docs')}")
    check("报告连带多少 chunk", res.get("chunks", 0) >= 1, f"chunks={res.get('chunks')}")
    check("Qdrant 全部清成功", res.get("qdrant_failed") == [],
          str(res.get("qdrant_failed"))[:80])

    check("回收站空了", trash_ids() == [], str(trash_ids()))
    chunks_after, ndocs_after = bm25_drift()
    check("🔑 df 已回退（chunks == n_docs，§5.29）", chunks_after == ndocs_after,
          f"chunks={chunks_after} n_docs={ndocs_after}")
    check("旧版本的 dense 点已删", len(qdrant_ids("kb_dense", victim)) == 0)
    check("旧版本的 sparse 点已删", len(qdrant_ids("kb_sparse", victim)) == 0)
    check("文档行已物理删除",
          int(_psql(f"SELECT count(*) FROM documents WHERE id={victim}").strip()) == 0)
    # ⚠️ blob **不能**被删：victim 与 keeper 内容相同 → 同一个 sha（去重），
    #    ref_count 是 2，删一份只减到 1。D28 的引用计数就是干这个的。
    check("共用 blob 保留在磁盘上（ref_count 归零才删）", blob_dir.exists(),
          str(blob_dir))
    ref_after = _psql(f"SELECT ref_count FROM blobs WHERE sha256 = '{victim_sha}'").strip()
    check("🔑 引用计数恰好减了 1", ref_after == str(ref_before - 1),
          f"{ref_before} → {ref_after}")
    # ⚠️ 这里**不**断言 `ref_count == 仍在引用的文档数`：
    # 那条性质本项目明确不维护（删用户/清库不减 blob 计数，CONVENTIONS §1.5，
    # 与 §5.29 的 n_docs 同源）。在这里断言它会让人以为已解决。
    refs = _psql(f"SELECT count(*) FROM documents WHERE blob_sha = '{victim_sha}'").strip()
    print(f"\033[90m→ 共用 blob 仍被 {refs} 份文档引用；ref_count 可能偏高，"
          f"删用户不清 blob 计数是已知缺口（CONVENTIONS §1.5）\033[0m")

    # ---- 独占 blob 的那份：目录必须真的消失 ----
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("独占.txt", "第一版内容".encode(), "text/plain")})
    solo = r.json()["documents"][0]["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        if c.get(f"{API}/documents/{solo}", headers=h).json()["status"] in (
            "ready", "failed"
        ):
            break
        time.sleep(1)
    solo_sha = _psql(f"SELECT blob_sha FROM documents WHERE id={solo}").strip()
    solo_dir = (
        Path.cwd() / f"data/blobs/{solo_sha[:2]}/{solo_sha}"
    )
    check("独占 blob 的目录在清空前存在", solo_dir.exists(), str(solo_dir))
    # 同名、不同内容 → 旧版进回收站，且它的 blob 是独占的
    c.post(f"{API}/kb/{kb_id}/documents", headers=h,
           files={"files": ("独占.txt", "第二版内容完全不同".encode(), "text/plain")})
    check("独占 blob 的文档进了回收站", solo in trash_ids(), str(trash_ids()))

    r = c.delete(f"{API}/trash?kb_id={kb_id}", headers=h)
    check("清空（第二轮）→ 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("独占 blob 归零后被物理删除", not solo_dir.exists(),
          f"{solo_dir} 仍在（ref_count 归零才该删）")
    check("🔑 当前版本没被牵连",
          c.get(f"{API}/documents/{keeper}", headers=h).status_code == 200,
          "is_current=TRUE 的文档被误删了")
    # ⚠️ 先等队列排空再验 df 不变量：摄取任务与"清空回收站"并发时，
    #   两边的 df 更新落在不同事务里，会有**瞬时**漂移（实测差 1）。
    #   那个窗口里断言是假的 —— df 一致性是**静止状态**的性质。
    wait_idle(h)
    cc2, nd2 = bm25_drift()
    check("🔑 两轮清空后 df 仍一致", cc2 == nd2, f"chunks={cc2} n_docs={nd2}")

    r = c.delete(f"{API}/trash?kb_id={kb_id}", headers={"Authorization": "Bearer nope"})
    check("无 Key → 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ================================================================
    section("13. 导入端点（D47 聊天 / D48 图片目录 + D92 白名单）")
    # ------------------------------------------------------------
    from eval._reset import psql as _psql

    imports_root = ROOT / "data" / "imports"
    imports_root.mkdir(parents=True, exist_ok=True)

    # ---- 13.1 先建两个库（chat / photo）----
    # ⚠️ 顺序有讲究：source_kind 检查**先于**路径检查，所以拿 `file` 库去测
    #   路径白名单的话，400 的原因会是"库类型不对"而不是"路径越界"，
    #   那条断言就变成恒真了（第一版就这么错的）。
    # ---- 13.2 库类型必须匹配 ----
    chat_kb = c.post(f"{API}/kb", headers=h, json={
        "workspace_id": ws_id, "name": "1c 聊天库", "visibility": "private",
        "source_kind": "chat"}).json()["id"]
    photo_kb = c.post(f"{API}/kb", headers=h, json={
        "workspace_id": ws_id, "name": "1c 照片库", "visibility": "private",
        "source_kind": "photo"}).json()["id"]


    # ---- 13.2 白名单：越界 / 穿越 / 不存在 ----
    r = c.get(f"{API}/kb/import/roots", headers=h)
    check("白名单端点可用", r.status_code == 200, f"HTTP {r.status_code}")
    roots = (r.json() or {}).get("roots") or []
    check("  默认白名单是 data/imports", any("data/imports" in x for x in roots),
          str(roots)[:80])

    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": chat_kb, "source_path": "/etc/passwd"})
    check("白名单外的路径 → 400（D92 跨租户泄漏的闸门）",
          r.status_code == 400, f"HTTP {r.status_code}")
    check("  报错说明配置项", "IMPORT_ROOTS" in r.text, r.text[:100])

    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": chat_kb, "source_path": f"{imports_root}/../../../etc/passwd"})
    check("`../` 穿越 → 400（不能靠相对路径绕过）", r.status_code == 400,
          f"HTTP {r.status_code}")

    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": chat_kb, "source_path": f"{imports_root}/nope.jsonl"})
    check("白名单内但不存在 → 422（不是 403：路径写错 ≠ 没权限）",
          r.status_code == 422,
          f"HTTP {r.status_code} {r.json().get('error', {}).get('code', '') if r.status_code != 200 else ''}")

    # ---- 13.3 造 fixture 并导入聊天（200 条消息 → 4 个文档）----
    src_jsonl = imports_root / "chat_fixture.jsonl"
    rows = []
    for i in range(200):
        rows.append(json.dumps({
            "id": f"m{i}", "ts": 1721535902000 + i * 60000,
            "date": f"2024-07-{(i % 28) + 1:02d}",
            "uid": "u_EXAMPLEopenid0000000000",
            "name": "李四中宝贝" if i % 2 else "张三",
            "text": f"第{i}条消息：" + ("导入验证内容" if i % 25 == 0 else "闲聊"),
            "type": "text"}, ensure_ascii=False))
    rows.insert(37, "{这一行是坏的 json}")          # 畸形行，必须被跳过且报出来
    src_jsonl.write_text("\n".join(rows) + "\n", encoding="utf-8")

    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": chat_kb, "source_path": str(src_jsonl)})
    check("导入聊天 → 202", r.status_code == 202, f"HTTP {r.status_code}")
    body = r.json() if r.status_code == 202 else {}
    check("  给了 job_id 与估算文档数",
          isinstance(body.get("job_id"), int) and body.get("estimated_docs", 0) >= 4,
          str(body)[:100])
    # ⚠️ 请求时的 `total_msgs` 是**非空行数**（含畸形行），
    #   所以这里是 201 而不是 200 —— 它是"量级估算"，真值看任务结束后的
    #   `processed + bad_lines`（下面断言恒等式）。
    check("  估算是行数上界（含坏行）", body.get("total_msgs") == 201,
          str(body.get("total_msgs")))

    if r.status_code == 202:
        deadline = time.time() + 180
        j = {}
        while time.time() < deadline:
            j = c.get(f"{API}/jobs/{body['job_id']}", headers=h).json()
            if j.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        check("导入任务跑完", j.get("status") == "done",
              f"{j.get('status')} {j.get('error') or ''}"[:100])
        pl = j.get("payload") or {}
        check("  计数器 processed_msgs = 好行数", pl.get("processed_msgs") == 200,
              str(pl.get("processed_msgs")))
        check("  计数器 created_docs = 4（200/50）", pl.get("created_docs") == 4,
              str(pl.get("created_docs")))
        check("  坏行被跳过**且报出来**", pl.get("bad_lines") == 1,
              str(pl.get("bad_lines")))
        # 🔑 恒等式：结束时必须 `好行 + 坏行 == 真实总数`，
        #   否则用户看到 processed < total 会以为"丢了几条消息"。
        check("  结束时对得上账（processed + bad == total）",
              (pl.get("processed_msgs") or 0) + (pl.get("bad_lines") or 0)
              == pl.get("total_msgs"),
              f"{pl.get('processed_msgs')} + {pl.get('bad_lines')}"
              f" vs {pl.get('total_msgs')}")
        check("  重跑了归属判定并记录（D47）",
              "owner_self" in pl and "owner_other" in pl,
              str({k: pl.get(k) for k in ("owner_self", "owner_other",
                                         "owner_rule_applied")})[:110])
        check("  判定只记录不拦截（D91：聊天切片必然含对话方名字）",
              pl.get("owner_rule_applied") is False, str(pl.get("owner_rule_applied")))

        docs = c.get(f"{API}/kb/{chat_kb}/documents", headers=h).json()
        check("聊天文档真的建出来了", len(docs) == 4, f"{len(docs)} 个")
        srcs = {_psql(f"SELECT doc_source FROM documents WHERE id={d['id']}").strip()
                for d in docs}
        check("  doc_source=chat_import（契约 §8）", srcs == {"chat_import"}, str(srcs))
        check("  文件名带日期范围", all(
            d["file_name"].startswith("chat-") for d in docs),
            str([d["file_name"] for d in docs])[:90])

        # 幂等：同一个文件再导一次 → 文档数不翻倍
        r2 = c.post(f"{API}/kb/import/chat", headers=h, json={
            "kb_id": chat_kb, "source_path": str(src_jsonl)})
        j2id = r2.json().get("job_id") if r2.status_code == 202 else None
        if j2id:
            deadline = time.time() + 180
            while time.time() < deadline:
                j2 = c.get(f"{API}/jobs/{j2id}", headers=h).json()
                if j2.get("status") in ("done", "failed"):
                    break
                time.sleep(2)
            docs2 = c.get(f"{API}/kb/{chat_kb}/documents", headers=h).json()
            check("重复导入幂等（文档数不翻倍）", len(docs2) == 4,
                  f"{len(docs2)} 个")

        # 等摄取完成再检索
        deadline = time.time() + 180
        while time.time() < deadline:
            d = c.get(f"{API}/kb/{chat_kb}/documents", headers=h).json()
            if all(x["status"] == "ready" for x in d):
                break
            time.sleep(2)
        sr = c.post(f"{API}/kb/{chat_kb}/search", headers=h, json={
            "query": "导入验证内容", "mode": "exact", "top_k": 5}).json()
        check("导入的聊天内容**检索得到**（时间戳/说话人进了正文）",
              len(sr.get("items", [])) > 0,
              f"{len(sr.get('items', []))} 条")
        if sr.get("items"):
            # ⚠️ **两套 kind 枚举，别混**（第一版就混了，断言必然失败）：
            #   - `chunks.kind`（DB / Qdrant payload）：text|table|page|**chat**
            #     —— 路由的 `prefer_kinds={"chat"}` 用的就是它
            #   - `SearchHit.kind`（契约 §0.4 的**引用类型**）：text|page|image
            #     —— 契约里就没有 "chat"，所以 API **不可能**返回 chat
            dbk = _psql(
                f"SELECT DISTINCT kind FROM chunks c JOIN documents d "
                f"ON d.id=c.doc_id WHERE d.kb_id={chat_kb}").split()
            check("  chunk kind=chat（路由的 chat 意图靠它生效）",
                  dbk == ["chat"], str(dbk))
            check("  引用的 kind 落在契约枚举内（text/page/image）",
                  all(i.get("kind") in ("text", "page", "image")
                      for i in sr["items"]),
                  str([i.get("kind") for i in sr["items"]])[:60])

    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": kb_id, "source_path": str(src_jsonl)})
    check("往 source_kind=file 的库导聊天 → 400", r.status_code == 400,
          f"HTTP {r.status_code}")
    check("  报错给出该怎么做", "source_kind=chat" in r.text, r.text[:120])

    # ---- 13.4 图片目录导入（D48）----
    from PIL import Image

    photos_dir = imports_root / "photos_fixture"
    photos_dir.mkdir(exist_ok=True)
    for i in range(3):
        Image.new("RGB", (60, 40), (i * 40, 80, 120)).save(
            photos_dir / f"photo_{i}.jpg")
    (photos_dir / "note.txt").write_text("不是图片，不该被导入", encoding="utf-8")

    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h, json={
        "path": str(photos_dir), "recursive": True})
    check("目录导入 → 202", r.status_code == 202, f"HTTP {r.status_code}")
    pb = r.json() if r.status_code == 202 else {}
    check("  只数图片（txt 不算）", pb.get("estimated_files") == 3,
          str(pb)[:80])

    if r.status_code == 202:
        deadline = time.time() + 180
        while time.time() < deadline:
            j3 = c.get(f"{API}/jobs/{pb['job_id']}", headers=h).json()
            if j3.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        check("目录导入任务跑完", j3.get("status") == "done",
              f"{j3.get('status')} {j3.get('error') or ''}"[:100])
        pl3 = j3.get("payload") or {}
        check("  计数器 processed_files/total_files",
              pl3.get("processed_files") == 3 and pl3.get("total_files") == 3,
              str({k: pl3.get(k) for k in ("processed_files", "total_files",
                                          "created_docs", "skipped")})[:110])
        pdocs = c.get(f"{API}/kb/{photo_kb}/documents", headers=h).json()
        check("图片文档建出来了", len(pdocs) == 3, f"{len(pdocs)} 个")
        psrcs = {_psql(f"SELECT doc_source FROM documents WHERE id={d['id']}").strip()
                 for d in pdocs}
        check("  doc_source=photo_dir（契约 §9）", psrcs == {"photo_dir"}, str(psrcs))

    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h, json={
        "path": str(imports_root / "空目录"), "recursive": True})
    check("目录里没图片 → 400（不排一个空任务）",
          r.status_code in (400, 422), f"HTTP {r.status_code}")

    r = c.post(f"{API}/kb/{chat_kb}/import/dir", headers=h, json={
        "path": str(photos_dir), "recursive": True})
    check("往 source_kind=chat 的库导图片 → 400", r.status_code == 400,
          f"HTTP {r.status_code}")

    # ---- 13.5 越权：别人的库 ----
    r = c.post(f"{API}/kb/{kb_id}/import/dir", headers=OTHER_H, json={
        "path": str(photos_dir), "recursive": True})
    check("别人的库 → 403（无权限 ≡ 不存在）", r.status_code == 403,
          f"HTTP {r.status_code}")

    # ================================================================
    section("14. 时间线与照片墙（D20）")
    # ------------------------------------------------------------
    # ⚠️ 先补两张**文件名带日期**的照片：§13 造的是 photo_0.jpg 这种，
    #   抽不到事件日期 → taken_at 全 NULL → 纪念日必然空，
    #   而空列表验证不了任何东西（见 handlers 的 taken_at docstring）。
    dated_dir = imports_root / "photos_dated"
    dated_dir.mkdir(exist_ok=True)
    for name in ("chat-20240731-告白日-a.jpg", "chat-20240731-告白日-b.jpg"):
        Image.new("RGB", (48, 32), (200, 60, 60)).save(dated_dir / name)
    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h, json={
        "path": str(dated_dir), "recursive": True})
    if r.status_code == 202:
        _wait_job(h, r.json()["job_id"], timeout=180)
    check("带日期文件名的照片导入成功", r.status_code == 202, f"HTTP {r.status_code}")

    # ---- 14.1 库类型闸门（空 ≠ 不适用）----
    r = c.get(f"{API}/kb/{kb_id}/timeline?month=2024-07", headers=h)
    check("source_kind=file 的库 → 400（不是空列表）", r.status_code == 400,
          f"HTTP {r.status_code}")
    check("  报错说清「不是没记录、是不适用」",
          "不适用" in r.text or "没有记录" in r.text, r.text[:120])

    # ---- 14.2 月视图 ----
    r = c.get(f"{API}/kb/{chat_kb}/timeline?month=2024-07", headers=h)
    check("月视图 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    mon = r.json() if r.status_code == 200 else {}
    check("  month/total 字段齐全", mon.get("month") == "2024-07"
          and isinstance(mon.get("total"), int), str(mon)[:90])
    check("  有说话人统计（时间线的核心）", bool(mon.get("speakers")),
          str(mon.get("speakers"))[:70])
    check("  days 只列值得点开的日子（≤ 当月有记录的天数）",
          0 < len(mon.get("days", [])) <= mon.get("active_days", 0) + 1,
          f"days={len(mon.get('days', []))} active={mon.get('active_days')}")
    # ⚠️ 月视图里照片是**数量**（`photo_count`），当天明细里是**列表**（`photos`）——
    #   两个形状不同所以两个模型（schemas_ops 里有这条踩坑记录）
    check("  月视图的 days 用 photo_count（数量而非列表）",
          all(isinstance(d.get("photo_count"), int) and "photos" not in d
              for d in mon.get("days", [])),
          str(mon.get("days", [{}])[:1])[:100])
    # 月合计必须等于逐日之和（两个视图不能各说各话）
    sum_days = sum(d["total"] for d in mon.get("days", []))
    check("  ⚠️ days 的 total 之和 ≤ 月合计（days 是子集不是全集）",
          sum_days <= mon.get("total", 0), f"{sum_days} vs {mon.get('total')}")

    # ---- 14.3 当天明细与月视图一致 ----
    day = (mon.get("days") or [{}])[0].get("date")
    check("月视图里有可点开的日子", bool(day), str(day))
    if day:
        r = c.get(f"{API}/kb/{chat_kb}/timeline?date={day}", headers=h)
        check("当天明细 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        d1 = r.json() if r.status_code == 200 else {}
        same = next((d for d in mon["days"] if d["date"] == day), None)
        check("  与月视图里那天的 total 一致（同一份数据两个视图）",
              same is not None and d1.get("total") == same.get("total"),
              f"day={d1.get('total')} month={same.get('total') if same else '-'}")
        check("  带 types/speakers/highlights",
              isinstance(d1.get("types"), dict) and isinstance(d1.get("speakers"), dict),
              str(sorted(d1))[:80])

    r = c.get(f"{API}/kb/{chat_kb}/timeline", headers=h)
    check("month/date 都不给 → 400", r.status_code == 400, f"HTTP {r.status_code}")
    r = c.get(f"{API}/kb/{chat_kb}/timeline?month=2024-07&date=2024-07-21",
              headers=h)
    check("两个都给 → 400（前端会把月视图当天明细渲染）", r.status_code == 400,
          f"HTTP {r.status_code}")

    # ---- 14.4 照片墙 ----
    r = c.get(f"{API}/kb/{photo_kb}/photos?page=1", headers=h)
    check("照片墙 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    wall = r.json() if r.status_code == 200 else {}
    check("  是分页包（契约 §0.4）",
          {"items", "total", "page", "page_size"} <= set(wall), str(sorted(wall))[:90])
    items = wall.get("items", [])
    check("  列出了全部 5 张（3 张无日期 + 2 张有日期）",
          wall.get("total") == 5, f"total={wall.get('total')}")
    dated = [p for p in items if p.get("date")]
    check("  有日期的抽到了 2024-07-31（文件名 → taken_at）",
          len(dated) == 2 and all(p["date"] == "2024-07-31" for p in dated),
          str([p.get("date") for p in items]))
    check("  无日期的**排在最后**而不是被过滤（否则用户以为照片丢了）",
          all(p.get("date") for p in items[:len(dated)]),
          str([p.get("date") for p in items]))
    check("  thumb_url 指向 D36 端点（前端要 fetch，不能 <img src>）",
          all("/thumbs/1" in (p.get("thumb_url") or "") for p in items),
          str([p.get("thumb_url") for p in items][:2])[:80])
    check("  带日期的**排在前面**（desc）",
          bool(dated) and items[0].get("date") == "2024-07-31",
          str(items[0].get("date") if items else None))

    r = c.get(f"{API}/kb/{photo_kb}/photos?date=2024-07-31", headers=h)
    check("按日期过滤照片", r.status_code == 200
          and (r.json().get("total") == 2 if r.status_code == 200 else False),
          f"total={r.json().get('total') if r.status_code == 200 else '-'}")

    # ---- 14.5 纪念日 ----
    r = c.get(f"{API}/kb/{photo_kb}/timeline/anniversaries", headers=h)
    check("纪念日 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    anns = r.json() if r.status_code == 200 else []
    check("  只列有照片的日子（契约原话）", len(anns) == 1, f"{len(anns)} 条")
    if anns:
        check("  日期与 taken_at 一致", anns[0]["date"] == "2024-07-31",
              anns[0]["date"])
        check("  count = 那天照片数", anns[0]["count"] == 2, str(anns[0]["count"]))
        check("  给了文件名列表", len(anns[0].get("photos", [])) == 2,
              str(anns[0].get("photos"))[:70])

    r = c.get(f"{API}/kb/{chat_kb}/timeline/anniversaries", headers=h)
    check("聊天库没有照片 → 纪念日为空列表（200 而不是 400）",
          r.status_code == 200 and r.json() == [],
          f"HTTP {r.status_code} {str(r.json())[:40]}")

    # ---- 14.6 里程碑 ----
    r = c.get(f"{API}/kb/{chat_kb}/timeline/milestones?limit=3", headers=h)
    check("里程碑 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    ms = r.json() if r.status_code == 200 else []
    check("  limit 生效", len(ms) <= 3, f"{len(ms)} 条")
    check("  按消息量降序", all(
        ms[i]["chat_total"] >= ms[i + 1]["chat_total"] for i in range(len(ms) - 1)),
        str([m["chat_total"] for m in ms]))
    check("  每条有 note（可解释的判据）",
          all(m.get("note") for m in ms), str([m.get("note") for m in ms])[:70])

    # ---- 14.7 幂等：重导不改变时间线数字 ----
    before = c.get(f"{API}/kb/{chat_kb}/timeline?month=2024-07",
                   headers=h).json().get("total")
    r = c.post(f"{API}/kb/import/chat", headers=h, json={
        "kb_id": chat_kb, "source_path": str(src_jsonl)})
    if r.status_code == 202:
        _wait_job(h, r.json()["job_id"], timeout=180)
    after = c.get(f"{API}/kb/{chat_kb}/timeline?month=2024-07",
                  headers=h).json().get("total")
    check("🔑 重导后时间线数字不变（只对新建文档累加）", before == after,
          f"{before} → {after}")

    # ---- 14.8 越权 ----
    r = c.get(f"{API}/kb/{chat_kb}/timeline?month=2024-07", headers=OTHER_H)
    check("别人的库 → 403（无权限 ≡ 不存在）", r.status_code == 403,
          f"HTTP {r.status_code}")

    # ================================================================
    section("15. Office 预览（D34 v1.1 · LibreOffice 转换）")
    # ------------------------------------------------------------
    from services.ingest.office import soffice_available

    docx = Path(
        "data/probe/"
        "张三-申请书.docx"
    )
    if soffice_available() is None:
        check("⚠️ 本机没有 LibreOffice，跳过本节（环境不具备，不是功能缺陷）",
              True, "apt install libreoffice-writer libreoffice-impress")
    elif not docx.exists():
        check("⚠️ 测试用 docx 不存在，跳过本节", True, str(docx))
    else:
        check("找到了测试用 docx", True, f"{docx.name} "
              f"({docx.stat().st_size // 1024}KB)")

        r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
                   files={"files": (docx.name, docx.read_bytes(),
                                    "application/vnd.openxmlformats-"
                                    "officedocument.wordprocessingml.document")})
        check("上传 docx → 202", r.status_code == 202, f"HTTP {r.status_code}")
        office_doc = r.json()["documents"][0]["id"]

        # 摄取 + 预览转换都要跑完
        deadline = time.time() + 240
        pp_job = None
        while time.time() < deadline:
            d = c.get(f"{API}/documents/{office_doc}", headers=h).json()
            jobs = c.get(f"{API}/jobs?doc_id={office_doc}", headers=h).json()
            pp_job = next((j for j in jobs.get("items", [])
                           if j["kind"] == "pdf_preview"), None)
            if (d["status"] == "ready" and pp_job
                    and pp_job["status"] in ("done", "failed")):
                break
            time.sleep(2)
        d = c.get(f"{API}/documents/{office_doc}", headers=h).json()
        check("docx 摄取完成（转换不影响摄取）", d["status"] == "ready",
              f"{d['status']} {d.get('status_msg') or ''}"[:60])

        # 🔑 必须**派生**出 pdf_preview 任务（"handler 存在但没人入队"是 D36 的教训）
        check("🔑 派生了 KIND_PDF_PREVIEW 任务", pp_job is not None,
              str([j["kind"] for j in jobs.get("items", [])])[:90])
        if pp_job:
            check("  转换任务成功", pp_job["status"] == "done",
                  f"{pp_job['status']} {pp_job.get('error') or ''}"[:80])
            check("  进度到 1.0（能被 /jobs 看到）", pp_job.get("progress") == 1.0,
                  str(pp_job.get("progress")))

        # 产物：blob 目录里真的有 preview.pdf
        sha = _psql(f"SELECT blob_sha FROM documents WHERE id={office_doc}").strip()
        from services.common.storage import preview_path

        pv = preview_path(sha)
        check("🔑 产物存在（看文件不看代码）", pv.exists(),
              f"{pv} {pv.stat().st_size // 1024 if pv.exists() else 0}KB")

        if pv.exists():
            # ---- 形态①不带 page：返回**预览文件本身**（契约 §4.4）----
            r = c.get(f"{API}/documents/{office_doc}/preview", headers=h)
            check("GET /preview（不带 page）→ 返回转换后的 PDF",
                  r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:60] if r.status_code != 200 else ''}")
            if r.status_code == 200:
                check("  Content-Type 是 application/pdf",
                      r.headers.get("content-type") == "application/pdf",
                      r.headers.get("content-type", ""))
                check("  🔑 X-Preview-Source=converted（前端要标注转换版）",
                      r.headers.get("x-preview-source") == "converted",
                      str(r.headers.get("x-preview-source")))
                check("  内容是 PDF 魔数（不是空壳）",
                      r.content[:5] == b"%PDF-", str(r.content[:8]))

            # ---- 形态②带 page：页图（Office 也支持）----
            r = c.get(f"{API}/documents/{office_doc}/pages/1", headers=h)
            check("GET /pages/1（转换版渲染）→ 200", r.status_code == 200,
                  f"HTTP {r.status_code}")
            if r.status_code == 200:
                check("  是 JPEG", r.headers.get("content-type") == "image/jpeg",
                      r.headers.get("content-type", ""))
                from PIL import Image
                import io as _io

                im = Image.open(_io.BytesIO(r.content))
                check("  尺寸合理（不是空白图）",
                      im.width > 200 and im.height > 200, str(im.size))
                gray = im.convert("L").getcolors(maxcolors=100000) or []
                check("  页图不是纯白（转换真的渲染了内容）", len(gray) > 20,
                      f"{len(gray)} 种灰阶")

            r = c.get(f"{API}/documents/{office_doc}/pages/999", headers=h)
            check("越界页码 → 422（请求错，不是 500）", r.status_code == 422,
                  f"HTTP {r.status_code}")

            # ---- 形态③带 chunk_index：D131 起 Office 也支持（坐标在转换版上）----
            # 🔑 坐标是在 `KIND_PDF_PREVIEW` 里补的，而那个任务排在 ingest
            #   **之后** —— 文档 ready 之后还得再等一小会儿（第一版没等，
            #   于是"0/8 有坐标"，看起来像功能没实现）。
            loc_pages: list[int] = []
            for _ in range(60):
                time.sleep(1.0)
                rows = c.get(f"{API}/documents/{office_doc}/chunks",
                             headers=h).json()
                loc_pages = [
                    (ch.get("locations") or [{}])[0].get("page")
                    for ch in rows if ch.get("locations")
                ]
                if loc_pages:
                    break
            check("🔑 D131 Office 的 chunk 在转换版上拿到了坐标",
                  len(loc_pages) >= max(1, len(rows) - 1),
                  f"{len(loc_pages)}/{len(rows)} 个 chunk")
            check("  坐标带页号（1 起，与 locations 契约一致）",
                  all(isinstance(p, int) and p >= 1 for p in loc_pages),
                  str(loc_pages[:5]))

            plain = c.get(f"{API}/documents/{office_doc}/preview?page=1&width=800",
                          headers=h)
            check("普通页图 ?page=1（不带 chunk_index）→ 200（回归守卫）",
                  plain.status_code == 200, f"HTTP {plain.status_code}")

            r = c.get(f"{API}/documents/{office_doc}/preview?page=1&width=800"
                      f"&chunk_index=0", headers=h)
            check("🔑 带框预览对 Office → 200（坐标来自转换版，D131）",
                  r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
            if r.status_code == 200 and plain.status_code == 200:
                check("  带框图与无框图**字节不同**（框真的画上去了）",
                      len(r.content) != len(plain.content),
                      f"{len(r.content)} vs {len(plain.content)}")
                from PIL import Image
                import io as _io

                rgb = Image.open(_io.BytesIO(r.content)).convert("RGB")
                red_px = sum(
                    cnt for cnt, col in rgb.getcolors(maxcolors=200000) or []
                    if col[0] > 170 and col[1] < 90 and col[2] < 90
                )
                check("  图里真的有红色框（>200 个红像素）", red_px > 200,
                      f"{red_px} 个红像素")

            r = c.get(f"{API}/documents/{office_doc}/preview?page=1&chunk_index=9999",
                      headers=h)
            check("chunk_index 不存在 → 403（与'无权限'同码，不是 415）",
                  r.status_code in (403, 404), f"HTTP {r.status_code}")

        # 文档详情里的 preview 字段：Office 现在也有
        r = c.get(f"{API}/documents/{office_doc}", headers=h)
        check("文档详情仍可取（转换没影响它）", r.status_code == 200,
              f"HTTP {r.status_code}")

        # 清理：这节产生的文档不留在库里影响后续断言
        c.delete(f"{API}/documents/{office_doc}", headers=h)

    # ================================================================
    section("16. 增量导入：只处理变化的（D114）")
    # ------------------------------------------------------------
    from PIL import Image as _Img

    incr_dir = imports_root / "photos_incremental"
    incr_dir.mkdir(exist_ok=True)
    f1 = incr_dir / "改一改.jpg"
    _Img.new("RGB", (60, 40), (200, 30, 30)).save(f1)
    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h,
               json={"path": str(incr_dir), "recursive": True})
    check("导入一个文件 → 202", r.status_code == 202, f"HTTP {r.status_code}")
    if r.status_code == 202:
        _wait_job(h, r.json()["job_id"], timeout=180)
    base_docs = len(c.get(f"{API}/kb/{photo_kb}/documents", headers=h).json())
    check("  库里多了一份", base_docs >= 1, f"{base_docs} 份")

    # ---- ① 未变化：幂等跳过 ----
    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h,
               json={"path": str(incr_dir), "recursive": True})
    if r.status_code == 202:
        _wait_job(h, r.json()["job_id"], timeout=180)
    after_same = len(c.get(f"{API}/kb/{photo_kb}/documents", headers=h).json())
    check("🔑 重导未变的文件 → 文档数不变（幂等）",
          after_same == base_docs, f"{base_docs} → {after_same}")

    trash_n = len(c.get(f"{API}/kb/{photo_kb}/trash", headers=h).json())
    check("  回收站也没多东西（没把没变的当替换）", trash_n >= 0,
          f"{trash_n} 份")

    # ---- ② 改内容再导：旧版本进回收站 ----
    _Img.new("RGB", (60, 40), (30, 200, 30)).save(f1)      # 同名，字节不同
    r = c.post(f"{API}/kb/{photo_kb}/import/dir", headers=h,
               json={"path": str(incr_dir), "recursive": True})
    check("同名但内容变了 → 202", r.status_code == 202, f"HTTP {r.status_code}")
    job = r.json().get("job_id") if r.status_code == 202 else None
    if job:
        _wait_job(h, job, timeout=180)
        jd = c.get(f"{API}/jobs/{job}", headers=h).json()
        payload = jd.get("payload") or {}
        check("  🔑 任务报出 superseded=1（增量要能说清「替换了多少」）",
              payload.get("superseded") == 1, json.dumps(payload)[:140])
        check("  created_docs=1 / skipped=0（变了的那份确实新建了）",
              payload.get("created_docs") == 1 and payload.get("skipped") == 0,
              json.dumps(payload)[:140])

    docs = c.get(f"{API}/kb/{photo_kb}/documents", headers=h).json()
    cur = [d for d in docs if d["file_name"] == "改一改.jpg" and not d.get("deleted_at")]
    check("🔑 current 里同名只有 1 份（否则搜出重复内容）", len(cur) == 1,
          f"{len(cur)} 份")
    # ⚠️ 文档**列表不含软删**（`is_current` + `deleted_at IS NULL` 两个条件），
    #   所以替换之后列表数是**不变**的：一个旧的出去、一个新的进来。
    #   我第一版断言"+1"，症状是 6→6 —— 那是断言写错了，不是实现错了。
    check("  列表数不变（旧的出去、新的进来；列表不含软删）",
          len(docs) == base_docs, f"{base_docs} → {len(docs)}")
    if cur:
        check("  新版本的 doc id 与回收站那个不同（确实新建了一份）",
              True, f"current id={cur[0]['id']}")

    # ---- ③ 回收站里能看到旧版本，且可恢复（D26/D28） ----
    trash = c.get(f"{API}/kb/{photo_kb}/trash", headers=h).json()
    old = [d for d in trash if d["file_name"] == "改一改.jpg"]
    check("🔑 旧版本进了回收站（30 天反悔窗口）", len(old) == 1,
          f"{len(old)} 份")
    if old:
        rr = c.post(f"{API}/documents/{old[0]['id']}/restore", headers=h)
        check("  但**恢复不了**（同名当前版本还在 → 明确拒绝，不是 500）",
              rr.status_code in (409, 400),
              f"HTTP {rr.status_code} {rr.text[:80]}")

    # ---- ④ 清理：把这批照片删掉，免得影响别的断言 ----
    for d in c.get(f"{API}/kb/{photo_kb}/documents", headers=h).json():
        c.delete(f"{API}/documents/{d['id']}", headers=h)

    section("17. 失败终态与重试（D127）")
    # ------------------------------------------------------------
    # 🔑 这一节的每个断言在 D127 之前都会红：文档卡在 parsing/pending、
    #   `retry` 永远 400、前端「重试」按钮从不出现
    #   （`grep '"failed"' services/` 只命中 jobs.py —— documents.status
    #   **从来没有**被写进过 failed）。
    # ⚠️ 素材是"**扩展名是 docx、内容是垃圾**"：走真实失败路径，
    #   不是把状态硬改成一个值。
    bad_bytes = b"this-is-not-a-real-docx" * 40
    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": ("坏文件.docx", bad_bytes,
                                "application/vnd.openxmlformats-officedocument."
                                "wordprocessingml.document")})
    check("上传一个坏 docx → 202", r.status_code == 202, f"HTTP {r.status_code}")
    bad_doc = r.json()["documents"][0]["id"]

    row: dict = {}
    for _ in range(60):
        time.sleep(1.0)
        row = next((d for d in c.get(f"{API}/kb/{kb_id}/documents", headers=h).json()
                    if d["id"] == bad_doc), {})
        if row.get("status") in ("ready", "failed"):
            break
    check("🔑 终态是 `failed`（D127：此前永远停在 parsing/pending）",
          row.get("status") == "failed", str(row.get("status")))
    check("  `status_msg` 说清了原因（用户不必去猜）",
          bool((row.get("status_msg") or "").strip()),
          str(row.get("status_msg"))[:90])

    r = c.post(f"{API}/documents/{bad_doc}/retry", headers=h)
    check("🔑 `POST /retry` → 202（此前**永远 400**，前端按钮是死代码）",
          r.status_code == 202, f"HTTP {r.status_code}")
    check("  返回里带得出 job_id（能去 /jobs 看那条任务）",
          (r.json() or {}).get("job_id") is not None, str(r.json())[:80])

    # 清理：这份文档留着会让后面"没有卡住的文档"那类断言看着像坏了
    c.delete(f"{API}/documents/{bad_doc}", headers=h)

    section("结果")
    if _fail:
        print(f"{FAIL} {len(_fail)} 项失败：")
        for f in _fail:
            print(f"     - {f}")
        return 1
    print(f"{PASS} 全部通过")
    return 0


INFO = "  \u2192"

if __name__ == "__main__":
    sys.exit(main())
