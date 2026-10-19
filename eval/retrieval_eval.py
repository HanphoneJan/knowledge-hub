"""检索质量评测回放（真实语料）。

## 它回答什么

文章《RAG 优化》的核心是**评估与回放**：改动（换 embedding / 切分 / 融合权重 /
路由 / 查询改写）之后，拿**同一批真实问题**重跑，比较
`Hit@k / Recall@k / MRR`、每类别、每腿的数字 —— 否则"感觉变好了"无法证伪。

本脚本是 v1 被删的 `eval/retrieval_eval.py`（D157）在新的唯一检索实现
（`services/retrieval/`）上的重建：**真实语料**（`eval/_eval_corpus.py`）+
**人工标注问题集**（`eval/_qset.py`）+ **纯函数指标**（`eval/retrieval_metrics.py`）。

## 跑法

```bash
bash deploy/dev.sh start          # 需要 PG + Qdrant（模型离线缓存）
uv run python eval/retrieval_eval.py
```

产出：`reports/retrieval_eval.json`（机器可读）+ 屏幕表格（人可读）。
⚠️ 它会**清掉自己的评测账号**（`retr-eval@t.local`，与 1b/1c/1d 同一套清场约定），
跑完不留数据 —— 所以**串行**跑，别与其它 eval 并发。

## 口径（脱离口径的数字没有意义）

- 真值粒度：**chunk 级**（`(doc_id, chunk_index)`），另给一列 **doc 级**命中。
- `limit=10`，融合用默认 RRF（k=60）。
- `visual` 腿**不在本评测里**：本评测库只有文本类真实文档；扫描件/照片由
  keeper 库单独覆盖（那要 colSmol，15s/图，不适合放进回放主线）。
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import asyncio  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _eval_corpus import EVAL_EMAIL, build, materialize_chat_days  # noqa: E402
try:  # noqa: E402
    from _qset import QUERIES, EvalQuery
except ModuleNotFoundError as exc:  # pragma: no cover — 只在干净 checkout 里
    raise SystemExit(
        "缺少 eval/_qset.py —— 真实语料的人工标注问题集**含个人数据、不进仓库**。\n"
        "复制 eval/_qset.example.py 为 eval/_qset.py，"
        "按你自己的 data/ 语料填真值后再跑。"
    ) from exc
from retrieval_metrics import per_query_rows, score_query, summarize  # noqa: E402

from services.common.auth import UserContext  # noqa: E402
from services.common.db import dispose_engines, session_scope, sync_session_scope  # noqa: E402
from services.common.models import Chunk  # noqa: E402
from services.retrieval import engine  # noqa: E402
from services.retrieval.dense import search_dense  # noqa: E402
from services.retrieval.sparse import search_sparse  # noqa: E402
from services.retrieval.trgm import search_trgm  # noqa: E402

LIMIT = 10
REPORT = ROOT / "reports" / "retrieval_eval.json"

#: 照片意图的评测集（用**持久 keeper 库**：8 张形状/配色不同的图，真值可判）。
#: 依赖 `deploy/seed_photos.py` 已建过 keeper 库；没有就跳过这段（不是错误）。
#: 这是路由权重表里 `photo` 行能否复现的**唯一**干净证据 —— 文本语料测不出它。
PHOTO_QUERIES: list[tuple[str, str]] = [
    ("棋盘格的图案", "checker"),
    ("红色的圆形图形", "circle"),
    ("三角形的图案", "triangle"),
    ("柱状条纹的图形", "bars"),
    ("点阵排列的图案", "dot-grid"),
    ("同心圆环的图案", "ring"),
    ("绿色的方块", "square"),
    ("黄色的十字", "cross"),
]

#: 一个 event loop 跑完全部异步检索（§5.17：asyncpg 连接池绑定创建它的 loop）。
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


# ============================================================
# 标注守卫
# ============================================================


def verify_annotations(ids: dict, queries: list[EvalQuery]) -> list[str]:
    """每条标注的**真值文件存在**且**真值片段逐字出现**，否则报错。

    🔑 这是文章"评测集也要版本化"的落地：语料改名/删除后，一条"命中率掉了"
    很可能是**标注过期**而不是检索退化。先把它和真正的退化区分开。
    """
    problems: list[str] = []
    files = ids["files"]
    for q in queries:
        if q.golden_file not in files:
            problems.append(f"[{q.seed}] 真值文件不在语料里: {q.golden_file}")
    return problems


def gold_chunks(ids: dict, queries: list[EvalQuery]) -> dict[int, set[tuple[int, int]]]:
    """每条 query 的真值 chunk 集合 = 真值文档里**含 `golden_marker` 的 chunk**。

    ⚠️ 找不到就报一条 problem（标注与切分对不上），而不是悄悄退化成整篇文档 ——
    后者会让 `recall@k` 恒等于 doc 级命中，"找全了没有"这件事就测不出来了。
    """
    out: dict[int, set[tuple[int, int]]] = {}
    files = ids["files"]
    with sync_session_scope() as s:
        for q in queries:
            doc_id = files.get(q.golden_file)
            if doc_id is None:
                continue
            rows = s.query(Chunk.doc_id, Chunk.chunk_index, Chunk.content_norm).filter(
                Chunk.doc_id == doc_id
            ).all()
            wanted = q.golden_marker
            gold = {(d, ci) for d, ci, txt in rows if wanted in (txt or "")}
            out[q.seed] = gold
    return out


def _chat_queries(markers: dict[str, str]) -> list[EvalQuery]:
    """真实聊天按天 → date 意图查询（真值 = 那一天的那份文档）。"""
    out: list[EvalQuery] = []
    for i, (d, marker) in enumerate(sorted(markers.items()), start=100):
        y, m, dd = d.split("-")
        out.append(
            EvalQuery(
                i, "date", f"{int(y)}年{int(m)}月{int(dd)}日那天聊了什么",
                f"imports/eval_chat/chat-{d}.md", marker, "fact",
            )
        )
    return out


# ============================================================
# 检索
# ============================================================


def _ranked(leg: str, ctx: UserContext, kb_id: int, query: str) -> list[tuple[int, int]]:
    """跑一条腿/一次融合，返回按相关性排好的 `(doc_id, chunk_index)`。"""

    async def _q():
        async with session_scope() as s:
            if leg == "hybrid":
                out = await engine.search(
                    s, ctx, query, kb_id=kb_id, mode="hybrid", top_k=LIMIT
                )
                return [(h.doc_id, h.chunk_index) for h in out.items]
            if leg == "hybrid_rewrite":
                # 🔑 查询改写（文章 §Query Rewrite）：规则层 + 外部 LLM（若库配置可用）。
                #   ⚠️ 真实外部调用 —— 这是"要外部 LLM 做优化"的实测口径。
                from services.retrieval.llm import load_llm_config
                from services.retrieval.rewrite import rewrite_query

                config = await load_llm_config(s)
                rr = await rewrite_query(query, config=config, use_llm=True)
                out = await engine.search(
                    s, ctx, query, kb_id=kb_id, mode="hybrid", top_k=LIMIT,
                    extra_queries=rr.queries[1:],
                )
                return [(h.doc_id, h.chunk_index) for h in out.items]
            if leg == "dense":
                r = await search_dense(ctx, query, kb_id=kb_id, limit=LIMIT, session=s)
            elif leg == "sparse":
                r = await search_sparse(s, ctx, query, kb_id=kb_id, limit=LIMIT)
            elif leg == "trgm":
                r = await search_trgm(s, ctx, query, kb_id=kb_id, limit=LIMIT)
            elif leg == "visual":
                from services.retrieval.visual import search_visual

                r = await search_visual(ctx, query, kb_id=kb_id, limit=LIMIT)
            else:  # pragma: no cover
                raise ValueError(f"未知配置 {leg!r}")
            return [(h.doc_id, h.chunk_index) for h in r.hits]

    return run(_q())


def _keeper_docs() -> tuple[int, dict[str, int]] | None:
    """持久照片库（名字含 `keeper`）与其图片文档：`(kb_id, {形状: doc_id})`。

    返回 `None` 表示没跑过 `deploy/seed_photos.py` ⇒ **跳过照片段**（不是错误）。
    """
    from services.common.models import Document, KnowledgeBase

    with sync_session_scope() as s:
        kb = (
            s.query(KnowledgeBase)
            .filter(KnowledgeBase.name.like("%keeper%"))
            .first()
        )
        if kb is None:
            return None
        rows = (
            s.query(Document.id, Document.file_name)
            .filter(
                Document.kb_id == kb.id,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
            )
            .all()
        )
    shape_to_doc: dict[str, int] = {}
    for did, name in rows:
        for _q, shape in PHOTO_QUERIES:
            if shape in (name or ""):
                shape_to_doc[shape] = did
    return (kb.id, shape_to_doc) if shape_to_doc else None


def measure_photo(ctx: UserContext, kb_id: int, shape_to_doc: dict[str, int]) -> dict:
    """照片意图：每条腿的 doc 级命中（真值 = 形状对应的图片文档）。

    ⚠️ **doc 级**而不是 chunk 级：照片没有文本 chunk，标识就是 `doc_id`。
    """
    out: dict[str, dict] = {}
    for cfg in ("dense", "sparse", "trgm", "visual", "hybrid"):
        scores = []
        for q, shape in PHOTO_QUERIES:
            gold = shape_to_doc.get(shape)
            if gold is None:
                continue
            ranked_docs = list(dict.fromkeys(d for d, _ci in _ranked(cfg, ctx, kb_id, q)))
            scores.append(score_query(q, "photo", ranked_docs, {gold}))
        out[cfg] = {**summarize(scores), "per_query": per_query_rows(scores)}
    return out


def measure_time_filter(ctx: UserContext, kb_id: int, query: str) -> dict:
    """时间过滤自检（P6）：无过滤 / 过去 / 未来三种口径的命中数。

    🔑 期望：`no_filter == after_past`（过去的时间不会过滤掉任何东西）、
    `after_future == 0`（未来的时间过滤掉全部）。任一不符 = payload 的
    `updated_at` 没写进去或过滤没生效。
    """

    async def _count(after: str | None) -> int:
        async with session_scope() as s:
            out = await engine.search(
                s, ctx, query, kb_id=kb_id, mode="hybrid", top_k=LIMIT,
                updated_after=after,
            )
            return out.total

    return {
        "query": query,
        "no_filter": run(_count(None)),
        "after_past": run(_count("2000-01-01T00:00:00+00:00")),
        "after_future": run(_count("2999-01-01T00:00:00+00:00")),
    }


# ============================================================
# 主流程
# ============================================================
def _versions() -> dict:
    """本次回放的**版本戳**（P7 / 文章"每次改动都要版本化"）。"""
    import subprocess

    from services.common.config import get_settings
    from services.ingest.strategies import DEFAULT_STRATEGY

    s = get_settings()
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        rev = ""
    return {
        "code_rev": rev,
        "text_embed_model": s.text_embed_model,
        "rerank_model": s.rerank_model,
        "default_chunk_strategy": DEFAULT_STRATEGY,
        "rewrite": "rule + optional LLM (llm_settings.use_for_rewrite)",
        "eval_set": "eval/_qset.py QUERIES + PHOTO_QUERIES",
        "report_schema": 1,
    }


def main() -> int:  # noqa: C901
    from _reset import reset_users_and_bm25

    print("── 清理上一轮评测数据 ──")
    reset_users_and_bm25([EVAL_EMAIL])

    print("── 摄取真实语料（进程内走 handle_ingest）──")
    t0 = time.perf_counter()
    chat_markers = materialize_chat_days()
    ids = build(kb_name="检索评测-真实语料")
    print(f"   {ids['n_docs']} 篇文档，{time.perf_counter() - t0:.1f}s"
          + (f"（{len(ids['failed'])} 篇解析为空已跳过）" if ids.get("failed") else "")
          + (f"；聊天 {len(chat_markers)} 天已渲染" if chat_markers else ""))

    all_queries = [*QUERIES, *_chat_queries(chat_markers)]
    problems = verify_annotations(ids, all_queries)
    golds = gold_chunks(ids, all_queries)
    for q in all_queries:
        if not golds.get(q.seed):
            problems.append(f"[{q.seed}] 真值片段在真值文档里找不到: {q.golden_marker!r}")
    if problems:
        print("\n\033[31m标注问题（先修这个，否则指标不可信）：\033[0m")
        for p in problems:
            print("  ✗", p)

    kb_id = ids["kb_id"]
    ctx = UserContext(
        user_id=ids["user_id"], display_name="eval", email=EVAL_EMAIL,
        api_key_id=None, workspace_ids=frozenset({ids["ws_id"]}),
        readable_kb_ids=frozenset({kb_id}),
    )

    configs = ("dense", "sparse", "trgm", "hybrid", "hybrid_rewrite")
    results: dict[str, dict] = {}
    for cfg in configs:
        scores = []
        for q in all_queries:
            ranked = _ranked(cfg, ctx, kb_id, q.question)
            # chunk 级：真值是"应进上下文的证据片段"（文章口径：Context Recall/P）
            scores.append(
                score_query(q.question, q.category, ranked, golds.get(q.seed, set()))
            )
        results[cfg] = {**summarize(scores), "per_query": per_query_rows(scores)}

    # ---- 照片意图（keeper 持久库；没有就跳过）----
    photo_report = None
    keeper = _keeper_docs()
    if keeper is not None:
        pkb_id, shape_to_doc = keeper
        photo_ctx = UserContext(
            user_id=ids["user_id"], display_name="eval", email=EVAL_EMAIL,
            api_key_id=None, workspace_ids=frozenset(),
            readable_kb_ids=frozenset({pkb_id}),
        )
        photo_report = measure_photo(photo_ctx, pkb_id, shape_to_doc)

    # ---- 时间过滤自检（P6）----
    # ⚠️ 探针问题取自**本机**标注集，不要在这里硬编码真实语料里的字面查询
    #   （本脚本进公开仓库，硬编码等于把私人文档主题泄露出去）。
    time_filter = measure_time_filter(
        ctx, kb_id, QUERIES[0].question if QUERIES else "文档"
    )

    report = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        # D194（P7）：版本戳 —— 报告要能对应到"是哪一版解析/切分/嵌入/索引/
        # 改写/重排/评测集"跑出来的，否则指标变化无法定位到回归源
        #（文章：每次改动都要版本化）。
        "versions": _versions(),
        "corpus": {
            "source": "data/ 真实文档（docs_light + blogs + readme_demo）",
            "n_docs": ids["n_docs"],
            "kb_id": kb_id,
        },
        "n_queries": len(all_queries),
        "limit": LIMIT,
        "gold_granularity": "chunk（(doc_id, chunk_index)）",
        "annotation_problems": problems,
        "results": results,
        "photo": photo_report,
        "time_filter": time_filter,
        # D194（P7）：生成侧指标（Faithfulness / Answer Relevancy / Citation
        # Accuracy）需要**外部 LLM 判定**；本项目默认零外传 ⇒ 不在回放主线里跑。
        # 抽取式下"引用准确性"是结构保证（答案句子全部摘自原文），无需 LLM 判官。
        "generation_metrics": {
            "ran": False,
            "note": (
                "生成侧指标需外部 LLM 判定（显式配置后才可跑）；"
                "默认抽取式的引用可核验性由结构保证（答案句全部摘自原文）。"
            ),
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_table(results)
    if photo_report:
        print("\n照片意图（keeper 库，doc 级）—— 路由 photo 行的证据：")
        for cfg, blk in photo_report.items():
            o = blk["overall"]
            print(f"  [{cfg:<7}] n={o['n_queries']} Hit@1={o['hit_at'][1]:.3f}"
                  f" Hit@5={o['hit_at'][5]:.3f} MRR={o['mrr']:.3f}")
    else:
        print("\n（跳过照片意图：没找到 keeper 照片库，跑 deploy/seed_photos.py 后可见）")

    ok_time = (
        time_filter["no_filter"] == time_filter["after_past"]
        and time_filter["after_future"] == 0
    )
    print(
        f"\n时间过滤自检（P6）：无过滤 {time_filter['no_filter']} · "
        f"过去 {time_filter['after_past']} · 未来 {time_filter['after_future']}"
        f"  {'✓' if ok_time else '✗ 期望 无过滤==过去 且 未来==0'}"
    )

    print("\n── 清理评测数据 ──")
    reset_users_and_bm25([EVAL_EMAIL])

    dispose_engines()
    if not _LOOP.is_closed():
        _LOOP.close()

    print(f"\n报告写入 {REPORT}")
    return 1 if problems else 0


def _print_table(results: dict) -> None:
    print(f"\n{'配置/类别':<22}{'n':>4}{'Hit@1':>8}{'Hit@5':>8}"
          f"{'Recall@5':>10}{'MRR':>8}")
    print("-" * 62)
    for cfg, blk in results.items():
        overall = blk["overall"]
        print(f"[{cfg}] 总体".ljust(22)
              + f"{overall['n_queries']:>4}"
              + f"{overall['hit_at'][1]:>8.3f}"
              + f"{overall['hit_at'][5]:>8.3f}"
              + f"{overall['recall_at'][5]:>10.3f}"
              + f"{overall['mrr']:>8.3f}")
        for cat, c in blk["by_category"].items():
            print(f"    {cat:<18}{c['n_queries']:>4}"
                  f"{c['hit_at'][1]:>8.3f}{c['hit_at'][5]:>8.3f}"
                  f"{c['recall_at'][5]:>10.3f}{c['mrr']:>8.3f}")
    print("-" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
