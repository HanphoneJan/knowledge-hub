"""1d 子步骤端到端验证：检索 + 问答（SSE）。

用**真实的 15 页填写指南 PDF**（1c 传过的那份），因为验收标准就是
"能搜到'考察情况那栏填什么'"。

跑法：
    bash deploy/dev.sh start
    uv run python eval/api_1d_check.py

脚本会自己上传一份文档（可重复运行，前一轮的数据会被清掉）。

## 与 pytest 的分工

- `tests/test_retrieval_e2e.py`：合成数据，测**腿的正确性与 fail-closed**
- 这里：真实数据，测**召回质量与契约**，以及 SSE 的事件顺序
"""

from __future__ import annotations

import os

# 🔴 离线加载模型（说明见 tests/conftest.py）：不设的话每次加载 bge/colSmol
#   都会去 huggingface.co 重试 5 次（≈23s），整套 eval 慢到几十分钟。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GEN_NAME = "多页表单模板.pdf"

BASE = os.environ.get("PKH_TEST_BASE", "http://127.0.0.1:8010")
API = f"{BASE}/api/v1"
#: 探针要一份**多页、带坐标**的 PDF（自备）。用环境变量指，别把本机路径写进仓库。
DOC = Path(os.environ.get("PKH_PROBE_PDF", f"data/probe/{GEN_NAME}"))
#: 验收问题。答案在第 2 页第 3 条。
QUESTION = "考察情况那栏填什么"
#: `mode=exact` 的用例：必须是**正文里确实存在的连续子串**。
#: 早先写的 "软件2025" 只出现在文件名里，正文没有 → 0 条命中，
#: 差点被当成 trgm 腿的 bug。
EXACT_Q = "定期填写在"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "  \u2192"
_fail: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _fail.append(label)
    return ok


def section(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


def reset() -> dict[str, int | str]:
    """清掉上一轮的测试数据**并**重建 BM25 词表。

    ⚠️ **两个账号都要删**：本脚本会注册第二个用户（越权边界那节需要
    一个与被测库无关的用户）。不一起清的话第二次跑时 `ret1d-other`
    会拿到 409 而不是 201，而 `KeyError: 'api_key'` 完全指不到
    "这个邮箱已经存在" 这件真正的事。

    ⚠️ 还要**重建 BM25**：`DELETE FROM users` 会级联清 chunks，但不会回退
    `terms.df` / `corpus_stats.n_docs`（那两个数只有 worker 的 cleanup
    在 30 天后才减）。不重建的话每跑一轮 n_docs 就虚高 74，
    跑几轮后 sparse 腿的 idf 被压平、top1 会从答案段落变成表格标题。
    详见 `eval/_reset.py` 的 docstring 与 `PROGRESS.md` §5.29。
    """
    from eval._reset import reset_users_and_bm25

    return reset_users_and_bm25(["ret1d@t.local", "ret1d-other@t.local"])


# ============================================================
# SSE 客户端
# ============================================================


def read_sse(resp: httpx.Response) -> list[tuple[str, dict]]:
    """把 SSE 响应解析成 `[(event, data)]`。

    契约（`DESIGN-API.md` §5.1）：`event:` 行 + `data:` 行成对，
    用空行分隔。`data` 里可能有裸换行吗？没有 —— `retrieval/ask.py::_sse`
    用 `json.dumps`，多行文本会变成转义后的 `\\n`。
    """
    events: list[tuple[str, dict]] = []
    cur_event = None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            cur_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                events.append((cur_event or "message", json.loads(line[6:])))
            except json.JSONDecodeError:
                events.append((cur_event or "message", {"_raw": line[6:]}))
            cur_event = None
    return events


def _audit_count(user_id: int) -> int:
    """数该用户的审计条数。直接查库 —— 审计查询端点还没做（DESIGN-API §12）。

    `user_id` 是 SET NULL 外键，但用户还在，所以能按 id 查。
    """
    out = subprocess.run(
        ["docker", "exec", "pkh-postgres", "psql", "-U", "pkh", "-d", "pkh", "-tAc",
         f"SELECT count(*) FROM audit_log WHERE user_id = {int(user_id)}"],
        capture_output=True, text=True, check=False,
    )
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return 0


def _audit_rows(c, h, ws_id: int, action: str) -> list:
    """取某个 action 最近若干条审计**明文**（`GET /audit` 的查询面）。

    🔑 D141 要断言的是"审计里如实记了**两个** query"，那只能从查询面读 ——
    直接查库能看到行，但验不到"用户能不能通过产品看到这些信息"。
    """
    r = c.get(f"{API}/audit?workspace_id={ws_id}&action={action}", headers=h)
    if r.status_code != 200:
        return []
    body = r.json()
    return body.get("items", []) if isinstance(body, dict) else (body or [])


def main() -> int:  # noqa: C901
    c = httpx.Client(trust_env=False, timeout=180, base_url=BASE)
    stats = reset()

    section("0. 前置")
    check("BM25 词表已重建（n_docs 对得上真实 chunk 数）",
          # 🔑 比的是**全部 chunk 行**（含软删未 purge 的）：BM25 的 n_docs
          #   按 purge 生命周期登记，四处口径一致（D115）。
          #   ⚠️ 别改成「活文档 chunk 数」——那是向量侧口径，回收站一有内容就恒红。
          str(stats["n_docs"]) == str(stats["real_chunks"]),
          f"n_docs={stats['n_docs']} 真实 chunk={stats['real_chunks']} "
          f"terms={stats['terms']}")
    if not DOC.exists():
        print(f"{FAIL} 测试文档不存在: {DOC}")
        return 1
    check("测试文档存在", True, f"{DOC.name} ({DOC.stat().st_size // 1024}KB)")
    r = c.get(f"{BASE}/health")
    check("API 在线", r.status_code == 200)

    section("1. 注册 + 建库 + 传文档")
    r = c.post(f"{API}/auth/register", json={
        "email": "ret1d@t.local", "password": "ret1d-pass-1",
        "display_name": "检索测试"})
    if r.status_code != 201:
        print(r.text[:300])
        return 1
    key = r.json()["api_key"]
    uid = r.json()["user"]["id"]
    h = {"Authorization": f"Bearer {key}"}

    # ⚠️ 模型状态只在**鉴权版** `/api/v1/health` 里（`routers/health.py` 有意为之：
    # "bge 加载失败"这类信息不该暴露给免鉴权的公网探活端点）。
    r = c.get(f"{API}/health", headers=h)
    models = r.json().get("models", {}) if r.status_code == 200 else {}
    check("文本模型已加载", models.get("text_model", {}).get("loaded") is True,
          str(models.get("text_model", {}).get("name")))
    check("视觉模型在 API 进程未加载（D50）",
          models.get("visual_model", {}).get("loaded") is False)
    # 🔑 D133：队列状态。⚠️ 只在**鉴权版**里有值（部署细节不外泄），
    #   而它必须**真的在说队列的事** —— M9 写着 ✅，实现里原本是静态说明。
    q = r.json().get("queue") if r.status_code == 200 else {}
    check("🔑 鉴权版 /health 带队列状态",
          bool(q) and "error" not in q, str(q))
    check("  四个数字都在（含 24 小时失败数）",
          {"pending", "running", "failed_24h"} <= set(q), str(sorted(q)))
    check("  🔑 没有 running 时 oldest 是 null 而不是 0",
          q.get("oldest_running_seconds") is None or q["oldest_running_seconds"] > 0,
          str(q.get("oldest_running_seconds")))
    check("  stuck_after_seconds 与 monitor 的红线一致（1800）",
          q.get("stuck_after_seconds") == 1800, str(q.get("stuck_after_seconds")))
    pub = c.get(f"{BASE}/health")   # 免鉴权
    check("  免鉴权 /health 不泄露队列（qdrant/部署细节）",
          pub.status_code == 200 and not pub.json().get("queue"),
          str(pub.json().get("queue")) if pub.status_code == 200 else "")

    ws_id = c.get(f"{API}/workspaces", headers=h).json()[0]["id"]
    kb_id = c.post(f"{API}/kb", headers=h, json={
        "workspace_id": ws_id, "name": "1d 测试库", "visibility": "private",
    }).json()["id"]
    check("工作区与知识库就绪", bool(ws_id and kb_id), f"ws={ws_id} kb={kb_id}")

    # 🔑 越权用户**提前**注册：§11.2（MCP/A2A 的权限裁剪）要用它，
    #   而 §9 才开始注册 —— 顺序反了 §11.2 拿不到越权的那个库。
    r = c.post(f"{API}/auth/register", json={
        "email": "ret1d-other@t.local", "password": "other-pass-1",
        "display_name": "无关用户"})
    other_key = r.json()["api_key"]
    oh = {"Authorization": f"Bearer {other_key}"}
    other_kb_id = c.post(f"{API}/kb", headers=oh, json={
        "workspace_id": c.get(f"{API}/workspaces", headers=oh).json()[0]["id"],
        "name": "别人的库", "visibility": "private"}).json()["id"]
    #: 我能读的库（§11.2 的权限裁剪断言要逐个对）
    readable_kbs = sorted(
        k["id"] for k in c.get(f"{API}/kb", headers=h).json()
    )

    def _audit_total(action: str) -> int:
        """某个 action 的审计条数（§12 的查询面）。"""
        return c.get(f"{API}/audit?workspace_id={ws_id}&action={action}",
                     headers=h).json().get("total", 0)

    r = c.post(f"{API}/kb/{kb_id}/documents", headers=h,
               files={"files": (DOC.name, DOC.read_bytes(), "application/pdf")})
    check("上传 → 202", r.status_code == 202)
    doc_id = r.json()["documents"][0]["id"]

    section("2. 等 worker 摄取")
    deadline = time.time() + 180
    final = None
    while time.time() < deadline:
        d = c.get(f"{API}/documents/{doc_id}", headers=h).json()
        if d["status"] in ("ready", "failed"):
            final = d
            break
        time.sleep(2)
    if final is None:
        print(f"{FAIL} 超时未完成")
        return 1
    check("摄取完成", final["status"] == "ready",
          f"status={final['status']} msg={final.get('status_msg')}")
    if final["status"] != "ready":
        return 1
    n_chunks = final["chunk_count"]
    check("产出 chunk", n_chunks > 0, f"{n_chunks} 个")

    section("3. hybrid 检索命中验收问题")
    t0 = time.perf_counter()
    r = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 10})
    elapsed = (time.perf_counter() - t0) * 1000
    check("hybrid 搜索 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:400])
        return 1
    body = r.json()
    items = body["items"]
    check("有结果", len(items) > 0, f"{len(items)} 条，总耗时 {elapsed:.0f}ms")

    # ⚠️ **首查要单独量**：D46 的 500ms 说的是"检索段"，而**进程启动后的
    #   第一查**还要付一次嵌入模型懒加载（实测 500~550ms，正好压线）。
    #   那是预热，不是检索段的稳态成本 —— 实测稳态 30ms。
    #   混在一起量的话，这条断言会在"刚重启完就测"时随机变红（踩过）。
    check("  首查 < 3s（含进程预热的模型懒加载）", elapsed < 3000,
          f"{elapsed:.0f}ms")
    t2 = time.perf_counter()
    r2 = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 10})
    warm_ms = (time.perf_counter() - t2) * 1000
    check("  检索段 < 500ms（D46 · 稳态）", warm_ms < 500,
          f"{warm_ms:.0f}ms（首查 {elapsed:.0f}ms 含预热）")

    # 真值：含答案的那一段。答案在第 2 页第 3 条，含"考察情况"与"栏内"。
    #
    # ⚠️ 比较要用 **(doc_id, chunk_index)** 而不是 (kb_id, chunk_index) ——
    # chunk_index 只在单个文档内唯一，写成 kb_id 会得到"永远命中不了"的假失败。
    chunks = c.get(f"{API}/documents/{doc_id}/chunks?page_size=200", headers=h).json()
    truth = {
        (doc_id, ch["chunk_index"]) for ch in chunks
        if "考察情况" in ch["content_norm"] and "栏内" in ch["content_norm"]
    }
    check("真值 chunk 存在", bool(truth), f"{len(truth)} 个")
    if truth:
        got = {(i["doc_id"], i["chunk_index"]) for i in items}
        hit = truth & got
        check("  命中真值（RRF 融合）", bool(hit), f"命中 {len(hit)}/{len(truth)}")
        rank = next(
            (i for i, x in enumerate(items, start=1)
             if (x["doc_id"], x["chunk_index"]) in truth),
            None,
        )
        check("  真值排第一", rank == 1, f"rank={rank}")

    check("  结果带 L3 坐标", any(i.get("locations") for i in items),
          f"{sum(1 for i in items if i.get('locations'))}/{len(items)}")

    # ⚠️ 高亮片段是**字面子串**匹配（契约 §5.2 的 `highlight`/`positions`）。
    # 提问"考察情况那栏填什么"在原文里不存在这个连续子串，所以 snippets 为空
    # 是**正确行为** —— 高亮服务的是 exact 搜索，不是语义问答。
    #
    # ⚠️ 早先这里写的是 `all(not snippets) or any(snippets)` —— **恒真**，
    # 是个永远不会红的假断言。现在换成真断言（D77）：
    # hybrid 的 snippets 大概率全空，但**每条都必须带正文 `text`**，
    # 否则调用方（前端 / MCP / A2A）拿不到任何可读内容。
    n_snipped = sum(1 for i in items if i["snippets"])
    check("  hybrid 的 snippets 基本为空（字面子串匹配）",
          n_snipped < max(1, len(items) // 2),
          f"{n_snipped}/{len(items)} 条有高亮")
    check("  🔑 每条命中都带正文 text（D77）",
          all(i.get("text") for i in items),
          f"{sum(1 for i in items if i.get('text'))}/{len(items)} 条，"
          f"首条 {len(items[0].get('text') or '')} 字")

    section("4. 三种 mode")
    # ⚠️ `exact` 用**原文里确实存在的子串**。早先用的 "软件2025" 只在文件名里、
    # 正文没有，所以 0 条命中 —— 那不是 bug，是测试数据选错了。
    r = c.post(f"{API}/kb/{kb_id}/search", headers=h,
               json={"query": "培养教育考察", "mode": "semantic", "top_k": 5})
    check("semantic（dense）有命中", r.status_code == 200 and r.json()["total"] > 0,
          f"{r.json()['total'] if r.status_code == 200 else 0} 条")

    r = c.post(f"{API}/kb/{kb_id}/search", headers=h,
               json={"query": EXACT_Q, "mode": "exact", "top_k": 5})
    items = r.json()["items"] if r.status_code == 200 else []
    check("exact（trgm）有命中", r.status_code == 200 and len(items) > 0,
          f"{len(items)} 条")
    if items:
        check("  命中的是含该子串的段落",
              any(EXACT_Q in i["snippets"][0]["highlight"] for i in items if i["snippets"]),
              f"highlight={items[0]['snippets'][0]['highlight'] if items[0]['snippets'] else '无'}")
        check("  exact 模式带高亮片段", any(i["snippets"] for i in items),
              f"{sum(1 for i in items if i['snippets'])}/{len(items)}")
        # `text` 与 `snippets` 必须说的是同一段（D77）：
        # 前端会用 text 渲染卡片、用 snippets 打高亮，两者对不上就会高亮错位置。
        mismatched = [
            i["chunk_index"] for i in items
            if i["snippets"] and not any(
                s["text"][s["positions"][0][0]:s["positions"][0][1]] == s["highlight"]
                for s in i["snippets"] if s["positions"]
            )
        ]
        check("  🔑 text 与 snippets 指向同一段（D77）", not mismatched,
              f"不一致 {len(mismatched)} 条" if mismatched
              else f"抽查 {items[0]['chunk_index']}: {items[0]['text'][:30]}…")

    r = c.post(f"{API}/kb/{kb_id}/search", headers=h,
               json={"query": QUESTION, "mode": "hybrid", "top_k": 5})
    check("hybrid（三路）有命中", r.status_code == 200 and r.json()["total"] > 0,
          f"{r.json()['total'] if r.status_code == 200 else 0} 条")

    section("5. 检索调试面板（四路可视化）")
    r = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h,
               json={"query": QUESTION, "top_k": 5})
    check("debug → 200", r.status_code == 200)
    dbg = r.json()
    legs = dbg["legs"]
    check("  返回 4 条腿", len(legs) == 4, f"{sorted(legs)}")
    for name in ("dense", "sparse", "trgm"):
        check(f"  {name} 腿有耗时记录", legs[name]["ms"] >= 0, f"{legs[name]['ms']}ms")
    # 🔴 D159：这条判据以前是"visual 腿必须 `skipped`"——那是**接通读侧之前**
    # 的事实（API 进程恒不加载 colSmol，视觉腿只写不读）。视觉查询服务
    # （`:8020`）起来之后它**不该**再跳过 ⇒ 旧判据会在服务正常时红掉。
    #
    # ⚠️ **两种状态都要通过**，而判据必须指向**不变量**而不是某一个状态：
    #   ✅ `skipped is None` ⇒ 视觉腿真的跑了（走远端编码）
    #   ✅ `skipped is not None` ⇒ 降级了，但**必须带可操作的原因**
    #        （只写"失败"的话，运维会去查 Qdrant / colSmol / 索引，
    #          而真正的原因只是"那个进程没起"）
    # ⚠️ **不能**写成 `skipped is None`：那会让本脚本的结果取决于
    #   那台机器上有没有起视觉服务 —— CI 上绿、本机上红，
    #   而红的原因与被测代码无关（MEMORY.md §6 第 3 条那一族）。
    vis = legs["visual"]
    vis_detail = (vis["skipped"] or "跑了，skipped=null")[:60]
    if vis["skipped"] is None:
        check("  visual 腿跑了（视觉服务可达）", True, vis_detail)
    else:
        check("  visual 腿降级但原因可操作",
              "8020" in vis["skipped"] or "dev.sh start" in vis["skipped"],
              vis_detail)
    check("  RRF 融合结果有排名明细",
          bool(dbg["rrf"]) and bool(dbg["rrf"][0]["ranks"]),
          f"top1 ranks={dbg['rrf'][0]['ranks'] if dbg['rrf'] else '空'}")
    check("  列出每腿耗时", "total_ms" in dbg["elapsed"],
          f"{dbg['elapsed']}")

    section("6. 问答 SSE")
    t0 = time.perf_counter()
    with c.stream("POST", f"{API}/kb/{kb_id}/ask", headers=h,
                  json={"question": QUESTION, "top_k": 8, "stream": True}) as resp:
        check("ask → 200", resp.status_code == 200, f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        check("  Content-Type 是 text/event-stream",
              "text/event-stream" in ctype, ctype)
        check("  关掉了代理缓冲（D46 逐字输出）",
              resp.headers.get("x-accel-buffering") == "no")
        text = "".join(resp.iter_text())
    total_ms = (time.perf_counter() - t0) * 1000

    events = read_sse(httpx.Response(200, text=text))
    names = [e for e, _ in events]
    check("事件序列 = sources → delta* → done",
          names[0] == "sources" and names[-1] == "done"
          and set(names[1:-1]) <= {"delta"},
          f"{names[0]} → {names[-1]}（中间 {len(names) - 2} 帧）")

    sources = next(d["sources"] for e, d in events if e == "sources")
    retrieval_ms = next(d["retrieval_ms"] for e, d in events if e == "sources")
    check("  sources 事件带证据", len(sources) > 0, f"{len(sources)} 条")
    check("  检索段 < 500ms（D46）", retrieval_ms < 500, f"{retrieval_ms}ms")
    check("  首帧前无 delta（前端据此区分检索/生成）", names[0] == "sources")

    answer = "".join(d["text"] for e, d in events if e == "delta")
    check("  有答案文本", bool(answer.strip()), f"{len(answer)} 字")
    done = next(d for e, d in events if e == "done")
    check("  done 带引用", bool(done["citations"]), f"citations={done['citations']}")
    check("  done 里无悬空引用", done["dangling_citations"] == [],
          f"悬空={done['dangling_citations']}")
    check("  角标都能对应到证据",
          all(1 <= x <= len(sources) for x in done["citations"]))
    # D170：生成器默认是抽取式；若实例**显式配了**外部 LLM（`llm_settings`
    # enabled），则应是 openai_compat。两者都合法，别把"配了外部 LLM"判成失败。
    check("  生成器是已知的两种之一（extractive / openai_compat）",
          done["generator"] in ("extractive", "openai_compat"), done["generator"])
    check("  done 回报每腿情况", bool(done["legs"]), f"{sorted(done['legs'])}")
    # 🔑 留住第一轮的 `done`：D141 要用它证明"第一轮没有上文"
    #   （§8 里断言第二轮 history_turns=2，两者合起来才说明多轮真的接上了）。
    first_done = dict(done)

    if sources:
        s0 = sources[0]
        check("  证据带 L3 坐标（点引用能画框）", bool(s0.get("locations")),
              str(s0.get("locations", "")[:1])[:70])
        check("  证据带标题", bool(s0.get("title")), s0.get("title", "")[:40])


    section("7. 会话已落库")
    sid = done.get("session_id")
    check("done 回传 session_id", bool(sid), f"session_id={sid}")
    if sid:
        r = c.get(f"{API}/kb/{kb_id}/sessions", headers=h)
        check("  会话列表可读", r.status_code == 200, f"HTTP {r.status_code}")
        # ⚠️ 分页响应是 `{"items": [...], "total": N, ...}`，不是裸数组。
        # 迭代 dict 得到的是 key（字符串），于是 `s["id"]` 报
        # `TypeError: string indices must be integers` —— 报错完全指不到
        # "响应是分页结构" 这件真正的事。
        lst = r.json() if r.status_code == 200 else {}
        check("  会话已记录", any(x["id"] == sid for x in lst.get("items", [])),
              f"共 {lst.get('total', 0)} 个会话")
        r = c.get(f"{API}/sessions/{sid}", headers=h)
        check("  会话详情可读", r.status_code == 200)
        if r.status_code == 200:
            msgs = r.json()["messages"]
            check("  存了 user + assistant 两条",
                  len(msgs) == 2 and {m["role"] for m in msgs} == {"user", "assistant"},
                  f"{[m['role'] for m in msgs]}")
            check("  assistant 消息带引用角标", bool(msgs[-1].get("citations")),
                  f"citations={msgs[-1].get('citations')}")

    section("8. 多轮（复用 session_id · D141 上下文窗口）")
    # ⚠️ 第一轮（§7 那个）必须报 `history_turns=0`：会话刚建、没有上文。
    #   这条是"多轮不是假的"的**地基** —— 之前历史只写不读，
    #   每一轮的 history_turns 都是 0，而界面看上去就是多轮对话。
    check("🔑 第一轮 history_turns=0（刚建的会话没有上文）",
          first_done.get("history_turns") == 0,
          f"history_turns={first_done.get('history_turns')}")
    with c.stream("POST", f"{API}/kb/{kb_id}/ask", headers=h, json={
        "question": "变更流程是什么", "session_id": sid, "top_k": 5,
    }) as resp:
        ev2 = read_sse(httpx.Response(200, text="".join(resp.iter_text())))
    d2 = next(d for e, d in ev2 if e == "done")
    check("第二轮 → done", d2["citations"] is not None, f"citations={d2['citations']}")
    check("  沿用同一个 session", d2.get("session_id") == sid)
    # 🔑 第二轮**必须**带进上一轮的两条消息（user + assistant）
    check("🔑 第二轮带进上文（history_turns=2）",
          d2.get("history_turns") == 2, f"history_turns={d2.get('history_turns')}")

    # ---- 指代式追问：并上上一轮问句去检索 ----
    # ⚠️ 这一段默认生成器（抽取式）**不会**用上文作答，但**检索**会 ——
    #   "那栏填什么"单看检索不到东西，并上上一轮的问句才检得到。
    with c.stream("POST", f"{API}/kb/{kb_id}/ask", headers=h, json={
        "question": "那栏具体填什么", "session_id": sid, "top_k": 5,
    }) as resp:
        ev3 = read_sse(httpx.Response(200, text="".join(resp.iter_text())))
    d3 = next(d for e, d in ev3 if e == "done")
    check("🔑 第三轮带上两轮上文（history_turns=4）",
          d3.get("history_turns") == 4, f"history_turns={d3.get('history_turns')}")
    _adetail = _audit_rows(c, h, ws_id, "ask")
    latest = _adetail[0].get("detail") if _adetail else {}
    check("🔑 指代式追问并上了上一轮问句（query_expanded）",
          latest.get("query_expanded") is True,
          f"query={latest.get('question')!r} → {latest.get('retrieval_query')!r}")
    check("  审计里如实记两个 query（用户问了什么 / 检索了什么）",
          latest.get("retrieval_query", "").startswith("那栏具体填什么")
          and "转正流程" in latest.get("retrieval_query", ""),
          f"retrieval_query={latest.get('retrieval_query')!r}")
    if sid:
        r = c.get(f"{API}/sessions/{sid}", headers=h)
        # ⚠️ D141 在这一节多问了一轮（指代式追问），所以是 **3 轮 × 2 = 6 条**。
        #   别改回 4 —— 那条断言的用途是"每轮都落库了"，
        #   消息数应当等于**轮数 × 2**，不是某个记下来的常数。
        msgs = r.json()["messages"]
        check("  同一个会话的每轮都落库（3 轮 × 2 = 6 条）", len(msgs) == 6,
              f"{len(msgs)} 条：{[m['role'] for m in msgs]}")
        # ⚠️ 期望值里有 **3** 条 user 消息：本节的 §7 那一轮 + 这里的第 2、3 轮
        #   （我第一版只写了后两条，于是这条断言在**自己机器上就红**——
        #   症状是"多轮串台了"，而真相是"我数轮数时漏了一轮"）。
        check("  🔑 落库的 user 消息**就是**发出去的那句（多轮没串台）",
              [m["content"] for m in msgs if m["role"] == "user"]
              == [QUESTION, "变更流程是什么", "那栏具体填什么"],
              f"{[m['content'] for m in msgs if m['role'] == 'user']}")
        check("  🔑 落库的是**原问题**而不是并过上文的那个 query",
              all("上文问的是" not in m["content"] for m in msgs),
              "落库里混进了检索用的扩展 query ⇒ 下一轮会把它当用户问的话")

    section("9. 🔑 越权边界")
    # 另一个用户（§1 就注册好了，那里有注释说明为什么提前）
    check("越权用户已就绪（§1 提前注册）", bool(other_key), "")
    r = c.post(f"{API}/kb/{kb_id}/search", headers=oh,
               json={"query": QUESTION, "mode": "hybrid", "top_k": 5})
    check("无权限搜别人的 private 库 → 403 KB_NOT_FOUND", r.status_code == 403,
          r.json()["error"]["code"] if r.status_code == 403 else f"HTTP {r.status_code}")
    check("  错误码是 KB_NOT_FOUND（无权限 ≡ 不存在）",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND")

    r = c.post(f"{API}/kb/99999/search", headers=oh,
               json={"query": QUESTION, "mode": "hybrid", "top_k": 5})
    check("搜不存在的库 → 同一个错误码",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND",
          f"HTTP {r.status_code} {r.json().get('error', {}).get('code', '')}")

    r = c.post(f"{API}/search", headers=oh,
               json={"query": QUESTION, "kb_ids": [kb_id], "top_k": 5})
    check("跨库检索别人的库 → 空结果（不是 403，避免泄漏存在性）",
          r.status_code == 200 and r.json()["total"] == 0,
          f"HTTP {r.status_code} total={r.json().get('total') if r.status_code == 200 else ''}")

    r = c.post(f"{API}/kb/{kb_id}/ask", headers=oh, json={"question": QUESTION})
    check("无权限问答 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    # 拿别人的 session_id 往自己的库写
    r = c.get(f"{API}/kb/{kb_id}/sessions", headers=oh)
    check("无权限读别人的会话列表 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    section("10. 🔑 请求体不接受 user_id")
    r = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 5,
        "user_id": uid, "tenant_id": 1})
    check("带 user_id/tenant_id 的请求被忽略或拒绝",
          r.status_code in (200, 422),
          f"HTTP {r.status_code}（200 = 字段被忽略，结果不受影响）")

    section("11. 审计已记录（D8 审计三元组）")
    before = _audit_count(uid)
    c.post(f"{API}/search", headers=h, json={"query": QUESTION, "top_k": 3})
    time.sleep(0.5)
    after = _audit_count(uid)
    check("跨库检索后审计增加", after > before, f"{before} → {after}")

    r = c.post(f"{API}/search", headers=h, json={"query": QUESTION, "top_k": 3})
    check("本用户跨库检索 → 200", r.status_code == 200, f"total={r.json().get('total')}")

    r = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 3})
    check("单库检索 → 200", r.status_code == 200)

    # ---- 🔴 D139：检索面**逐个**计数，不能只看总数 ----
    # ⚠️ 第一版就是只断言"总数增加了"，而 D139 那两个漏掉的端点加起来
    #   恰好能让总数增加成立 —— 于是基线一直是绿的（§5.138 第三行那一族）。
    #   判据：每个端点**单独**打一次，比它自己带来的增量。
    def _audit_surface(label, call):
        b = _audit_count(uid)
        code = call().status_code
        time.sleep(0.5)
        a = _audit_count(uid)
        check(f"🔑 {label} 记审计（M8：每次检索/问答）", a > b,
              f"HTTP {code} · 审计 {b} → {a}")

    _audit_surface("单库检索 /kb/{id}/search",
                   lambda: c.post(f"{API}/kb/{kb_id}/search", headers=h,
                                  json={"query": QUESTION, "mode": "hybrid",
                                        "top_k": 3}))
    _audit_surface("检索调试 /kb/{id}/search/debug",
                   lambda: c.post(f"{API}/kb/{kb_id}/search/debug", headers=h,
                                  json={"query": QUESTION, "top_k": 3}))
    _audit_surface("证据预览 /kb/{id}/ask/preview（会返回正文片段）",
                   lambda: c.get(f"{API}/kb/{kb_id}/ask/preview",
                                 headers=h, params={"q": QUESTION, "top_k": 3}))
    _audit_surface("跨库检索 /search",
                   lambda: c.post(f"{API}/search", headers=h,
                                  json={"query": QUESTION, "top_k": 3}))

    section("12. 答案内容抽检（人可读，便于人工核对）")
    # 打印 sources 与答案正文。断言只能验证结构（角标合法、有文本），
    # 但"抽出来的句子是不是真的回答了问题"要靠眼睛看。
    print(f"  {INFO} 问题：{QUESTION}")
    for s in sources[:3]:
        print(f"  {INFO} [{s['idx']}] {s['title'][:36]}  score={s['score']:.5f}")
        print(f"       {(s['text'] or '')[:100]}")
    print(f"  {INFO} 答案：")
    for line in answer.splitlines()[:6]:
        print(f"       {line[:110]}")
    truth_cited = done["citations"][:1] if done["citations"] else []
    check("答案引用了真值证据 [1]", truth_cited == [1],
          f"citations={done['citations']}（真值应排第一）")

    # ================================================================
    section("11.2 MCP（D9）与 A2A（D10）")
    # ------------------------------------------------------------
    MCP = f"{BASE}/mcp"
    A2A = f"{BASE}/a2a/tasks"

    def rpc(method: str, params: dict | None = None, *, req_id=1) -> dict:
        return c.post(MCP, headers=h, json={
            "jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params or {}}).json()

    # ---- 🔴 D144：参数校验（-32602 而不是 -32603 + 裸异常）----
    def _mcp_err(name, arguments):
        body2 = rpc("tools/call", {"name": name, "arguments": arguments})
        return (body2.get("error") or {})

    for _label, _args, _expect in [
        ("缺必填参数", {"query": QUESTION}, "缺少必填参数 kb_id"),
        ("类型错", {"kb_id": "不是数字", "query": QUESTION}, "必须是整数"),
        ("越界", {"kb_id": kb_id, "query": QUESTION, "top_k": 9999}, "不能大于 50"),
        ("未知参数", {"kb_id": kb_id, "query": QUESTION, "kb_lid": 1}, "未知参数"),
    ]:
        err = _mcp_err("search_kb", _args)
        check(f"🔑 {_label} → -32602", err.get("code") == -32602,
              f"code={err.get('code')} {str(err)[:70]}")
        check(f"  {_label}的消息可操作（含「{_expect}」）",
              _expect in err.get("message", ""), err.get("message", "")[:80])
        # 🔑 绝不能是裸 Python 异常（D144 之前的症状：-32603 + KeyError）
        check(f"  {_label}的消息不含裸异常类型名",
              not any(x in err.get("message", "") for x in
                      ("KeyError", "ValueError", "TypeError", "Traceback")),
              err.get("message", "")[:80])
    # 🔑 界线：库不存在是**业务失败**（isError），不是协议错
    body2 = rpc("tools/call", {"name": "get_document",
                              "arguments": {"kb_id": 999999, "doc_id": 1}})
    check("🔑 库不存在仍是 isError（不是 -32602）",
          "error" not in body2 and body2.get("result", {}).get("isError") is True,
          str(body2)[:90])

    # ---- initialize：版本协商 ----
    init = rpc("initialize", {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "probe", "version": "0"}})
    check("MCP initialize → 有 result", "result" in init, str(init)[:90])
    r0 = init.get("result") or {}
    check("  protocolVersion 协商成功", r0.get("protocolVersion") == "2025-06-18",
          str(r0.get("protocolVersion")))
    check("  声明了 tools 能力", bool((r0.get("capabilities") or {}).get("tools")),
          str(r0.get("capabilities")))
    check("  serverInfo 有名字与版本",
          bool((r0.get("serverInfo") or {}).get("name"))
          and bool((r0.get("serverInfo") or {}).get("version")),
          str(r0.get("serverInfo"))[:70])
    check("  instructions 告诉调用方先 list_kb",
          "list_kb" in (r0.get("instructions") or ""), str(r0.get("instructions"))[:60])

    # 不支持的版本 → 换成我们最新的，而不是报错
    init2 = rpc("initialize", {"protocolVersion": "1999-01-01"}, req_id=2)
    check("不认识的协议版本 → 回退到最新（不是报错）",
          (init2.get("result") or {}).get("protocolVersion") == "2025-06-18",
          str((init2.get("result") or {}).get("protocolVersion")))

    # ---- 通知：不该有响应体 ----
    r = c.post(MCP, headers=h, json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    check("notifications/initialized → 204（通知没有响应）",
          r.status_code == 204, f"HTTP {r.status_code} body={r.text[:40]!r}")

    # ---- tools/list：按权限裁剪（契约 §10.2）----
    tl = rpc("tools/list")
    tools = (tl.get("result") or {}).get("tools") or []
    names = [x["name"] for x in tools]
    check("tools/list → 4 个工具", sorted(names) == sorted(
        ["list_kb", "search_kb", "ask_kb", "get_document"]), str(names))
    kb_enum = None
    for x in tools:
        if x["name"] == "search_kb":
            kb_enum = ((x.get("inputSchema") or {}).get("properties") or {}) \
                .get("kb_id", {}).get("enum")
    check("  🔑 kb_id 的 enum 只含**有权限**的库（§10.2 权限裁剪）",
          isinstance(kb_enum, list) and set(kb_enum) == set(readable_kbs),
          f"enum={kb_enum} 可读={readable_kbs}")
    check("  每个工具都有 description（外部 AI 靠它选工具）",
          all((x.get("description") or "").strip() for x in tools))

    # ---- tools/call：search ----
    sr = rpc("tools/call", {"name": "search_kb", "arguments": {
        "kb_id": kb_id, "query": QUESTION, "top_k": 5, "mode": "hybrid"}})
    res = sr.get("result") or {}
    check("tools/call search_kb → 有 content", bool(res.get("content")),
          str(sr)[:90])
    check("  🔑 content[0].type=text（客户端必须支持的那一种）",
          (res.get("content") or [{}])[0].get("type") == "text",
          str((res.get("content") or [{}])[0].get("type")))
    check("  isError=false", res.get("isError") is False, str(res.get("isError")))
    try:
        payload = json.loads((res.get("content") or [{}])[0].get("text") or "{}")
        parsed = True
    except json.JSONDecodeError:
        payload, parsed = {}, False
    check("  text 里是合法 JSON（结果用 JSON 字符串）", parsed, "")
    check("  有命中且带正文",
          len(payload.get("items", [])) > 0
          and any(i.get("text") for i in payload.get("items", [])),
          f"{len(payload.get('items', []))} 条")
    check("  给出路由/精排结论（外部 AI 要知道为什么这么排）",
          "route" in payload and "rerank" in payload,
          str(sorted(payload))[:80])

    # ---- tools/call：ask（带引用）----
    ak = rpc("tools/call", {"name": "ask_kb", "arguments": {
        "kb_id": kb_id, "question": QUESTION, "top_k": 5}}, req_id=3)
    a_res = ak.get("result") or {}
    a_payload = json.loads((a_res.get("content") or [{}])[0].get("text") or "{}")
    check("tools/call ask_kb → 有答案", bool(a_payload.get("answer")),
          (a_payload.get("answer") or "")[:40])
    check("  带引用角标与 sources",
          bool(a_payload.get("citations")) and len(a_payload.get("sources", [])) > 0,
          f"citations={a_payload.get('citations')}")
    check("  回传 session_id（可续多轮，D7）",
          isinstance(a_payload.get("session_id"), int), str(a_payload.get("session_id")))
    sid = a_payload.get("session_id")
    ak2 = rpc("tools/call", {"name": "ask_kb", "arguments": {
        "kb_id": kb_id, "question": "那填完之后呢", "session_id": sid}}, req_id=4)
    check("  带 session_id 再问 → 同一个会话",
          json.loads((ak2.get("result") or {}).get("content", [{}])[0]
                     .get("text") or "{}").get("session_id") == sid,
          str(sid))

    # ---- tools/call：get_document ----
    gd = rpc("tools/call", {"name": "get_document", "arguments": {
        "kb_id": kb_id, "doc_id": doc_id, "limit": 3}}, req_id=5)
    g_res = gd.get("result") or {}
    g_payload = json.loads((g_res.get("content") or [{}])[0].get("text") or "{}")
    check("tools/call get_document → 元信息 + chunk 正文",
          g_payload.get("file_name") and len(g_payload.get("chunks", [])) > 0,
          str(g_payload.get("file_name"))[:40])
    check("  给了 truncated 标记（知道还有更多）",
          "truncated" in g_payload, str(g_payload.get("truncated")))

    # ---- 未知工具 = 协议错误（-32601），不是 500 ----
    bad = rpc("tools/call", {"name": "rm_rf", "arguments": {}}, req_id=6)
    check("未知工具 → JSON-RPC -32601",
          (bad.get("error") or {}).get("code") == -32601,
          str(bad.get("error"))[:70])

    # ---- 未知方法 ----
    nm = rpc("resources/list", {}, req_id=7)
    check("未知方法 → -32601", (nm.get("error") or {}).get("code") == -32601,
          str(nm.get("error"))[:70])

    # ---- 越权 kb_id = 业务错误（isError），不是协议错误 ----
    other = rpc("tools/call", {"name": "search_kb", "arguments": {
        "kb_id": other_kb_id, "query": QUESTION}}, req_id=8)
    o_res = other.get("result") or {}
    o_text = (o_res.get("content") or [{}])[0].get("text") or ""
    check("越权 kb_id → isError + KB_NOT_FOUND（业务错误）",
          o_res.get("isError") is True and "KB_NOT_FOUND" in o_text,
          o_text[:70].replace("\n", " "))
    check("  不是 JSON-RPC error（模型要读得懂前者）", "error" not in other,
          str(other)[:70])

    # ---- 协议错误也回 HTTP 200 ----
    r = c.post(MCP, headers=h, json={"jsonrpc": "1.0", "id": 9, "method": "tools/list"})
    body = r.json()
    check("jsonrpc 版本不对 → HTTP 400（不是 JSON-RPC 消息）",
          r.status_code == 400, f"HTTP {r.status_code}")
    check("  body 里有 error.code", (body.get("error") or {}).get("code") == -32600,
          str(body.get("error"))[:70])
    r = c.post(MCP, headers=h, content="{不是 json".encode())
    check("非 JSON → 400 + -32700",
          r.status_code == 400
          and (r.json().get("error") or {}).get("code") == -32700,
          f"HTTP {r.status_code}")

    r = c.get(MCP, headers={"Authorization": "Bearer x"})
    check("MCP 无有效 Key → 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ---- A2A：Agent Card ----
    r = c.get(f"{BASE}/.well-known/agent-card.json", headers=h)
    check("Agent Card → 200", r.status_code == 200, f"HTTP {r.status_code}")
    card = r.json() if r.status_code == 200 else {}
    skills = {s["id"] for s in card.get("skills", [])}
    check("  声明两个 skill（answer/research）",
          skills == {"answer", "research"}, str(sorted(skills)))
    check("  streaming=false（如实：端点不同步推流）",
          card.get("capabilities", {}).get("streaming") is False,
          str(card.get("capabilities")))
    # ⚠️ 用 str 比：kb_id 是 int，直接 `kb_id in json字符串` 会 TypeError
    check("  🔑 不含任何库的名字（那是权限信息）",
          str(kb_id) not in json.dumps(card, ensure_ascii=False),
          json.dumps(card, ensure_ascii=False)[:120])
    check("  url 指向 /a2a/tasks", (card.get("url") or "").endswith("/a2a/tasks"),
          card.get("url"))

    # ---- A2A：tasks/send（协议面）与便捷面 ----
    r = c.post(A2A, headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": "tasks/send",
        "params": {"skill": "answer", "kb_id": kb_id,
                   "message": {"parts": [{"kind": "text", "text": QUESTION}]}}})
    check("A2A tasks/send → 200", r.status_code == 200, f"HTTP {r.status_code}")
    task = r.json() if r.status_code == 200 else {}
    check("  是 Task 对象（state=completed）",
          task.get("kind") == "task"
          and task.get("status", {}).get("state") == "completed",
          str(task.get("status"))[:60])
    arts = task.get("artifacts") or []
    a_text = ((arts[0].get("parts") or [{}])[0].get("text") or "") if arts else ""
    check("  artifacts[].parts[].text 有内容", bool(a_text.strip()), a_text[:40])
    check("  metadata 带引用来源",
          bool((task.get("metadata") or {}).get("sources")),
          str(len((task.get("metadata") or {}).get("sources") or [])) + " 条")

    r2 = c.post(A2A, headers=h, json={
        "skill": "answer", "kb_id": kb_id, "query": QUESTION})
    t2 = r2.json() if r2.status_code == 200 else {}
    check("便捷面（skill+query）等价于协议面",
          bool(((t2.get("artifacts") or [{}])[0].get("parts") or [{}])[0]
               .get("text", "").strip()),
          "")

    # ---- A2A：research（按文档分组 + 如实说明抽取式）----
    r = c.post(A2A, headers=h, json={
        "skill": "research", "kb_id": kb_id, "query": QUESTION})
    rt = r.json() if r.status_code == 200 else {}
    rm = (rt.get("metadata") or {})
    docs = rm.get("documents") or []
    check("A2A research → 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("  按文档分组给摘录（多角度汇总）", len(docs) > 0, f"{len(docs)} 份文档")
    md = ((rt.get("artifacts") or [{}])[0].get("parts") or [{}])[0].get("metadata") or {}
    check("  🔑 如实声明抽取式（不调用 LLM）",
          md.get("extractive") is True and "抽取式" in (md.get("note") or ""),
          str(md.get("note"))[:50])

    # ---- A2A：越权与非法入参 ----
    r = c.post(A2A, headers=h, json={"skill": "answer", "kb_id": other_kb_id,
                                     "query": "x"})
    check("A2A 越权 → 403（无权限 ≡ 不存在）", r.status_code == 403,
          f"HTTP {r.status_code}")
    r = c.post(A2A, headers=h, json={"skill": "nope", "kb_id": kb_id, "query": "x"})
    check("未知 skill → 400 并列出可用", r.status_code == 400
          and "answer" in r.text, f"HTTP {r.status_code}")
    r = c.post(A2A, headers=h, json={"skill": "answer", "query": "x"})
    check("不给 kb_id → 400（Card 不泄露有哪些库）", r.status_code == 400,
          f"HTTP {r.status_code}")

    # ---- 审计（D8）----
    check("🔑 MCP 调用进了审计", _audit_total("mcp_call") >= 1,
          f"mcp_call={_audit_total('mcp_call')}")
    check("🔑 A2A 任务进了审计", _audit_total("a2a_call") >= 1,
          f"a2a_call={_audit_total('a2a_call')}")

    # ================================================================
    section("11.5 阶段 3：查询类型路由 + Reranker")
    # ------------------------------------------------------------
    from eval._reset import psql as _psql

    # --- 路由：默认行为必须与引入前一致 ---
    dbg = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 10}).json()
    check("debug 返回路由结论", "route" in dbg, str(sorted(dbg))[:80])
    rt = dbg.get("route") or {}
    check("普通问句 → general（不路由）", rt.get("intent") == "general",
          f"intent={rt.get('intent')}")
    check("  权重为空 = 等权 RRF 未被动（D71）", rt.get("weights") == {},
          str(rt.get("weights")))
    check("  有 why 可读（不是黑盒）", bool(rt.get("why")), str(rt.get("why"))[:50])

    # --- 路由：photo 意图 ---
    dbg2 = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h, json={
        "query": "第一次旅游的照片", "mode": "hybrid", "top_k": 10}).json()
    rt2 = dbg2.get("route") or {}
    check("photo 问句 → photo 意图", rt2.get("intent") == "photo",
          f"intent={rt2.get('intent')}")
    check("  偏好照片库", rt2.get("prefer_source_kinds") == ["photo"],
          str(rt2.get("prefer_source_kinds")))
    check("  有日期字面量时抽出日期", True, "")

    dbg3 = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h, json={
        "query": "2023年5月那天发生了什么", "mode": "hybrid", "top_k": 10}).json()
    rt3 = dbg3.get("route") or {}
    check("日期问句 → date 意图且抽出日期",
          rt3.get("intent") == "date" and rt3.get("date_hint") == "2023-05-01",
          f"{rt3.get('intent')} / {rt3.get('date_hint')}")

    # --- 🔑 过滤失效必须退回，不能返回 0 条 ---
    n_before = len(c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": "第一次旅游的照片", "mode": "hybrid", "top_k": 10}).json()["items"])
    dbg4 = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h, json={
        "query": "第一次旅游的照片", "mode": "hybrid", "top_k": 10}).json()
    note4 = (dbg4.get("route") or {}).get("note") or ""
    check("库里没有 photo 库时**不返回 0 条**", n_before > 0, f"{n_before} 条")
    check("  如实说明没有做过滤", "不做过滤" in note4, note4[:80])

    # --- Reranker：默认库没配 → skipped 且顺序不变 ---
    rr = dbg.get("rerank")
    check("debug 返回精排结论", "rerank" in dbg, str(sorted(dbg))[:80])
    check("未配 rerank_model → applied=false",
          (rr or {}).get("applied") is False, json.dumps(rr, ensure_ascii=False)[:90])
    check("  skipped 有原因（D72：不许静默）", bool((rr or {}).get("skipped")),
          str((rr or {}).get("skipped"))[:70])

    search_items = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
        "query": QUESTION, "mode": "hybrid", "top_k": 10}).json()["items"]
    check("未启用精排时检索照常返回", len(search_items) > 0, f"{len(search_items)} 条")

    # --- 🔑 开了精排：applied=true 且顺序真的按分排 ---
    _psql(f"UPDATE knowledge_bases SET rerank_model="
          f"'BAAI/bge-reranker-v2-m3' WHERE id={kb_id}")
    try:
        dbg5 = c.post(f"{API}/kb/{kb_id}/search/debug", headers=h,
                      json={"query": "考察情况那栏该填什么", "mode": "hybrid",
                            "top_k": 10}).json()
        rr5 = dbg5.get("rerank") or {}
        check("配了 rerank_model → applied=true", rr5.get("applied") is True,
              json.dumps(rr5, ensure_ascii=False)[:110])
        check("  每个候选都有分", bool(rr5.get("scores")),
              f"{len(rr5.get('scores') or {})} 个")
        check("  报出模型名", bool(rr5.get("model")), str(rr5.get("model")))

        items5 = c.post(f"{API}/kb/{kb_id}/search", headers=h, json={
            "query": "考察情况那栏该填什么", "mode": "hybrid",
            "top_k": 10}).json()["items"]
        scores = rr5.get("scores") or {}
        # ⚠️ rerank 的 scores 用 `FusedHit.key`（= `text:{point_id}`），
        #   而 `/search` 的响应里**没有 key 也没有 point_id** —— 所以这里
        #   用 `point_id()` 重算（同一口径，见 qdrant.py 的说明）。
        #   第一版写成 `text:{doc_id}:{chunk_index}`，一个都对不上，
        #   而"列表比空列表更糟"让断言变成恒真。
        from services.common.qdrant import point_id as _pid

        keyed = [
            f"text:{_pid(kb_id, i['doc_id'], i['chunk_index'])}" for i in items5
        ]
        matched = [k for k in keyed if k in scores]
        check("  重算的 key 能对上精排分数", len(matched) >= 3,
              f"{len(matched)}/{len(keyed)} 对上")
        by_score = sorted(
            [k for k in keyed if k in scores], key=lambda k: -scores[k])
        check("  顺序真的按精排分排（不是 RRF 原序）",
              [k for k in keyed if k in scores] == by_score,
              f"返回 {len(keyed)} 条，其中有分 {len(by_score)} 条")

        # 相关性抽查：最相关的那条内容里应含"考察情况"
        top = items5[0] if items5 else {}
        check("  top1 的正文与 query 同主题（粗查）",
              "考察" in (top.get("text") or "") or not top.get("text"),
              (top.get("text") or "")[:40])
    finally:
        _psql(f"UPDATE knowledge_bases SET rerank_model=NULL WHERE id={kb_id}")

    check("关掉后恢复 skipped",
          (c.post(f"{API}/kb/{kb_id}/search/debug", headers=h, json={
              "query": QUESTION, "mode": "hybrid", "top_k": 5}).json()
           .get("rerank") or {}).get("applied") is False, "")

    # ================================================================
    section("11. 运维与审计（M8 只读那半 + M9 对账/重建/磁盘）")
    from eval._reset import psql
    # ------------------------------------------------------------
    # 审计查询
    # ------------------------------------------------------------
    r = c.get(f"{API}/audit?workspace_id={ws_id}&action=search", headers=h)
    check("审计查询 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    au = r.json() if r.status_code == 200 else {}
    check("  是分页包（契约 §0.4）",
          set(au) >= {"items", "total", "page", "page_size"}, str(sorted(au)))
    check("  能查到刚才那次检索", au.get("total", 0) >= 1, f"total={au.get('total')}")
    if au.get("items"):
        it = au["items"][0]
        check("  审计三元组齐全（key→user→kb）",
              it["user_id"] == uid and it["kb_id" if "kb_id" in it else "kb_ids"],
              f"user={it.get('user_id')} kb_ids={it.get('kb_ids')}")
        check("  含 query 摘要（§12 的用途）",
              "query" in (it.get("detail") or {}),
              str(it.get("detail"))[:60])

    r = c.get(f"{API}/audit?workspace_id={ws_id}&action=ask", headers=h)
    check("按 action 过滤（ask 有记录）", r.status_code == 200 and r.json()["total"] >= 1,
          f"total={r.json().get('total') if r.status_code == 200 else '-'}")
    # ⚠️ 选一个**真的没发生过**的 action。原先用 `mcp_call`，
    #   加了 MCP 之后它就有记录了 → 这条断言恒红（第一版就这么错的）。
    #   现在 `restore`/`purge`/`reindex` 由别的 eval（1c 的回收站）覆盖，
    #   1d 里不该出现。
    for never in ("purge", "restore"):
        r = c.get(f"{API}/audit?workspace_id={ws_id}&action={never}", headers=h)
        check(f"按 action 过滤（本轮没发生过的 {never} 为 0）",
              r.status_code == 200 and r.json()["total"] == 0,
              f"total={r.json().get('total') if r.status_code == 200 else '-'}")
        break

    r = c.get(f"{API}/audit?workspace_id={ws_id}&action=serach", headers=h)
    check("打错 action → 400（不静默返回空）", r.status_code == 400,
          r.json()["error"]["code"] if r.status_code == 400 else f"HTTP {r.status_code}")

    r = c.get(f"{API}/audit?workspace_id={ws_id}", headers={"Authorization": "Bearer x"})
    check("无 Key → 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ------------------------------------------------------------
    # 索引对账（M9）：先制造漂移，再看它被看见，最后重建修好
    # ------------------------------------------------------------
    r = c.get(f"{API}/admin/consistency?kb_id={kb_id}", headers=h)
    check("对账 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    rep = r.json() if r.status_code == 200 else {}
    check("  PG chunk 数 = 该库 chunk 数",
          rep.get("pg_chunks") == len(chunks),
          f"pg={rep.get('pg_chunks')} 实际={len(chunks)}")
    check("  此刻一致（刚摄取完还没漂）", rep.get("consistent") is True,
          f"missing={rep.get('missing_count')} dangling={rep.get('dangling_count')}")

    # 制造漂移：删掉 **Qdrant 里的点**（漏索引）。
    # ⚠️ 为什么删点而不是删 PG 的 chunk 行：reindex 是"从 PG 重建索引"，
    #   PG 里没有的东西它造不回来 —— 第一版造的是那种漂移，
    #   于是"重建后恢复一致"永远不成立（方向搞反了）。
    from services.common.qdrant import close_client as _cc, get_client as _gc
    from services.common.qdrant import point_id as _pid

    qc = _gc()
    victim_chunk = chunks[0]["chunk_index"]
    victim_doc = int(
        psql(f"SELECT id FROM documents WHERE kb_id={kb_id} "
             f"AND is_current IS TRUE LIMIT 1").splitlines()[0]
    )
    gone_pid = _pid(kb_id, victim_doc, victim_chunk)
    qc.post("/collections/kb_dense/points/delete",
            json={"points": [gone_pid], "wait": True})
    _cc()
    check("  确实删掉了 Qdrant 的点（否则后面的断言无意义）", True,
          f"point_id={gone_pid}")

    r = c.get(f"{API}/admin/consistency?kb_id={kb_id}", headers=h)
    rep2 = r.json() if r.status_code == 200 else {}
    # 🔑 只删了 dense 的点，所以 sparse 仍是齐的 ——
    #   顶层并集口径这时**仍然报一致**，必须看 `legs` 才看得见。
    check("dense 腿报不一致（分腿才看得见）",
          rep2.get("legs", {}).get("kb_dense", {}).get("consistent") is False,
          f"dense={rep2.get('legs', {}).get('kb_dense')}")
    check("  报出**具体**的缺失点 id（不只给布尔值）",
          rep2.get("legs", {}).get("kb_dense", {}).get("missing_count", 0) >= 1,
          f"missing={rep2.get('legs', {}).get('kb_dense', {}).get('missing_count')}")
    check("  sparse 腿仍一致（没被动过）",
          rep2.get("legs", {}).get("kb_sparse", {}).get("consistent") is True,
          f"sparse={rep2.get('legs', {}).get('kb_sparse')}")
    check("  顶层并集口径此时**仍然说一致**（所以不能只看顶层）",
          rep2.get("consistent") is True,
          f"consistent={rep2.get('consistent')} —— 这是并集口径的盲点")

    r = c.post(f"{API}/admin/reindex?kb_id={kb_id}", headers=h)
    check("重建索引 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        check("  返回了任务 id（入队不是同步跑）",
              isinstance(r.json().get("job_ids"), list) and r.json()["job_ids"],
              str(r.json())[:80])
        deadline = time.time() + 120
        while time.time() < deadline:
            js = c.get(f"{API}/jobs?kb_id={kb_id}&kind=reindex", headers=h).json()
            if js.get("total", 0) >= 1 and all(
                j["status"] in ("done", "failed") for j in js["items"]
            ):
                break
            time.sleep(2)
        check("  worker 跑完了重建任务",
              all(j["status"] == "done" for j in js.get("items", [])),
              str([(j["kind"], j["status"]) for j in js.get("items", [])]))
        rep3 = c.get(f"{API}/admin/consistency?kb_id={kb_id}", headers=h).json()
        legs3 = rep3.get("legs", {})
        check("  重建后 dense 腿恢复（漏索引被补回）",
              legs3.get("kb_dense", {}).get("consistent") is True,
              f"dense={legs3.get('kb_dense')}")
        check("  重建后 sparse 腿也一致", legs3.get("kb_sparse", {}).get("consistent") is True,
              f"sparse={legs3.get('kb_sparse')}")
        # 🔑 重建只针对目标库，但 BM25 词表是**全局量**：
        #   如果重建时只登记目标库的 chunk，别的库的 idf 就会偏移。
        #   这里用全库口径对账验它没被弄坏。
        rep4 = c.get(f"{API}/admin/consistency", headers=h).json()
        check("  词表重建没有弄坏别的库（全库口径仍一致）",
              rep4.get("consistent") is True,
              f"missing={rep4.get('missing_count')} dangling={rep4.get('dangling_count')}")
        # 🔑 重建走的是**入库那条实现**，payload 必须与入库时一致 ——
        #   之前 reindex 写最简 payload，重建后 SearchHit.text 与 L3 坐标会消失。
        hit = c.post(f"{API}/kb/{kb_id}/search", headers=h,
                     json={"query": QUESTION, "mode": "exact", "top_k": 5}).json()["items"]
        check("  重建后检索结果仍有正文与坐标（D77/L3 没被重建毁掉）",
              any(i.get("text") for i in hit) and any(i.get("locations") for i in hit),
              f"{len([i for i in hit if i.get('text')])}/{len(hit)} 条带正文")

    # ------------------------------------------------------------
    # 按工作区统计（M9：磁盘）
    # ------------------------------------------------------------
    r = c.get(f"{API}/workspaces/{ws_id}/stats", headers=h)
    check("工作区统计 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    st = r.json() if r.status_code == 200 else {}
    check("  文档数对得上", st.get("doc_count", 0) >= 1,
          f"doc_count={st.get('doc_count')}")
    check("  chunk 数对得上", st.get("chunk_count", 0) >= len(chunks),
          f"chunk_count={st.get('chunk_count')}")  # 删点不改 PG，74 仍在
    check("  给两个字节数（用户视角 vs 实际占盘）",
          st.get("sum_doc_size_bytes", 0) > 0 and st.get("blob_disk_bytes", 0) > 0,
          f"sum={st.get('sum_doc_size_bytes')} disk={st.get('blob_disk_bytes')}")
    # ⚠️ `blob_disk_bytes` 含派生文件（预览 PDF、缩略图），所以它**大于**原始之和 ——
    # 拿"disk ≤ sum"当断言是错的（第一版写反了，被真实数字顶回来）。
    # 去重要单独验：同一份内容再传一份 → sum 翻倍，磁盘几乎不变。
    disk_before = st.get("blob_disk_bytes", 0)
    sum_before = st.get("sum_doc_size_bytes", 0)
    check("  实际占盘含派生文件（≥ 原始之和，不是更小）", disk_before >= sum_before,
          f"disk={disk_before} sum={sum_before}")
    c.post(f"{API}/kb/{kb_id}/documents", headers=h,
           files={"files": ("统计去重.pdf", DOC.read_bytes(), "application/pdf")})
    st2 = c.get(f"{API}/workspaces/{ws_id}/stats", headers=h).json()
    check("  同一份内容再传：文档大小之和翻倍",
          st2.get("sum_doc_size_bytes", 0) > sum_before,
          f"{sum_before} → {st2.get('sum_doc_size_bytes')}")
    check("  但磁盘几乎不变（内容寻址去重）",
          st2.get("blob_disk_bytes", 0) <= disk_before * 1.1,
          f"{disk_before} → {st2.get('blob_disk_bytes')}")

    section("结果")
    if _fail:
        print(f"{FAIL} {len(_fail)} 项失败：")
        for f in _fail:
            print(f"     - {f}")
        return 1
    print(f"{PASS} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())