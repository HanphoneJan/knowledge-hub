"""生成侧质量评测（P7）—— 用**外部 LLM** 做生成与判定（RAGAS 式的 Faithfulness /
Answer Relevancy / Citation Accuracy 的轻量版）。

## 为什么单独一个脚本、且要真实外部 LLM

文章《RAG 优化》§评估要求把**检索指标**与**生成指标**分开看。检索侧由
`eval/retrieval_eval.py` 覆盖；生成侧（答案是否忠于证据、是否回应问题、引用是否
支撑结论）**没有 LLM 判官就测不出来** —— 而本项目默认零外传（抽取式），所以：

- 只有 `llm_settings` 配了可用外部 LLM 时才跑；没配就打印说明并跳过（不是失败）。
- 用它当**生成器**（`openai_compat`）与**判官**（同一个网关）。⚠️ 这会把检索出的
  原文片段发给外部服务 —— 这是"要外部 LLM 做优化"的明确前提。

## 三个指标（LLM 判官 + 一条结构判据）

| 指标 | 判据 | 来源 |
|---|---|---|
| `faithfulness` | 答案的每个事实是否被证据支撑（0/1）| LLM 判官 |
| `answer_relevancy` | 答案是否回应了问题（1~5）| LLM 判官 |
| `citation_accuracy` | 引用的证据是否支撑对应结论（0/1）| LLM 判官 |
| `dangling_rate` | 悬空角标比例（引用了不存在的证据）| **结构判据，无需 LLM** |

跑法：`uv run python eval/generation_eval.py`（需 PG + Qdrant + 可用外部 LLM）。
产出 `reports/generation_eval.json`。⚠️ 与 `retrieval_eval.py` 一样会清自己的评测账号，**串行**跑。
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import asyncio  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _eval_corpus import EVAL_EMAIL, build  # noqa: E402
try:  # noqa: E402
    from _qset import QUERIES
except ModuleNotFoundError as exc:  # pragma: no cover — 只在干净 checkout 里
    raise SystemExit(
        "缺少 eval/_qset.py —— 真实语料的人工标注问题集**含个人数据、不进仓库**。\n"
        "复制 eval/_qset.example.py 为 eval/_qset.py，"
        "按你自己的 data/ 语料填真值后再跑。"
    ) from exc

from services.common.auth import UserContext  # noqa: E402
from services.common.db import dispose_engines, session_scope, sync_session_scope  # noqa: E402
from services.retrieval import engine  # noqa: E402
from services.retrieval.ask import to_evidences  # noqa: E402
from services.retrieval.llm import get_generator, load_llm_config, parse_citations  # noqa: E402

REPORT = ROOT / "reports" / "generation_eval.json"

#: 评测的 question 数（每个要 1 次生成 + 1 次判官调用 ⇒ 控制成本）。
MAX_QUERIES = 12

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


_JUDGE_SYSTEM = (
    "你是 RAG 答案质量评估器。给你一个问题、一个候选答案、以及检索到的证据列表，"
    "请只输出 JSON 对象，字段："
    '{"faithfulness": 0或1（答案的每个事实是否都能在证据里找到支撑）, '
    '"answer_relevancy": 1到5 的整数（答案多大程度回应了问题）, '
    '"citation_accuracy": 0或1（答案标注的引用是否支撑了对应结论）, '
    '"reason": "一句话理由"}。不要输出 JSON 以外的任何内容。'
)


def _llm_json(config, system: str, user: str) -> dict | None:
    """同步调外部 LLM 要一段 JSON。失败返回 None（判官是增强，崩了不算失败）。"""
    import httpx

    base = (config.base_url or "").rstrip("/")
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    for k, v in (getattr(config, "extra_headers", None) or {}).items():
        if k and v is not None:
            headers[str(k)] = str(v)
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=60, trust_env=False) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _judge(config, question: str, answer: str, evidence: list[str]) -> dict | None:
    ev_txt = "\n".join(f"[{i}] {t[:500]}" for i, t in enumerate(evidence, start=1))
    user = f"问题：{question}\n\n候选答案：{answer}\n\n证据：\n{ev_txt}"
    return _llm_json(config, _JUDGE_SYSTEM, user)


def _ask_once(ctx, kb_id, question, generator):
    """跑一次问答（检索 → 组证据 → 生成 → 解析引用）。**不落库**。"""

    async def _go():
        async with session_scope() as session:
            outcome = await engine.search(
                session, ctx, question, kb_id=kb_id, mode="hybrid", top_k=8
            )
            evs = to_evidences(outcome.items)
            try:
                answer = "".join([p async for p in generator.stream(question, evs, [])])
            except Exception as exc:  # noqa: BLE001
                answer = f"[生成失败: {type(exc).__name__}: {exc}]"
            citations, dangling = parse_citations(answer, len(evs))
            return answer, [e.text for e in evs], citations, dangling

    return run(_go())


def main() -> int:
    from _reset import reset_users_and_bm25

    reset_users_and_bm25([EVAL_EMAIL])

    with sync_session_scope() as s:
        config = _sync_config(s)
    if not (
        config.usable() and config.usable_for("ask")
    ):
        print("外部 LLM 不可用（llm_settings 未配或 ask 未开）—— 生成侧评测**跳过**。")
        return 0

    print(f"外部 LLM 就绪：{config.model} @ {config.base_url}")
    print("── 摄取真实语料（进程内）──")
    ids = build(kb_name="生成评测-真实语料")
    kb_id = ids["kb_id"]
    ctx = UserContext(
        user_id=ids["user_id"], display_name="gen", email=EVAL_EMAIL,
        api_key_id=None, workspace_ids=frozenset({ids["ws_id"]}),
        readable_kb_ids=frozenset({kb_id}),
    )
    generator = get_generator(config=config)  # 外部 LLM 生成器（usable_for('ask') 已校验）

    rows: list[dict] = []
    for q in QUERIES[:MAX_QUERIES]:
        answer, evidence, citations, dangling = _ask_once(
            ctx, kb_id, q.question, generator
        )
        judged = _judge(config, q.question, answer, evidence)
        rows.append(
            {
                "question": q.question,
                "category": q.category,
                "answer": answer[:400],
                "citations": citations,
                "dangling": dangling,
                "n_evidence": len(evidence),
                "judge": judged,
            }
        )
        status = "?" if judged is None else (
            f"F={judged.get('faithfulness')} R={judged.get('answer_relevancy')} "
            f"C={judged.get('citation_accuracy')}"
        )
        print(f"  [{q.category:<6}] {q.question[:26]:<28} {status}")

    scored = [r for r in rows if r["judge"]]
    n = max(1, len(scored))
    report = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": config.model,
        "provider": config.provider,
        "n_queries": len(rows),
        "n_judged": len(scored),
        "metrics": {
            "faithfulness": round(
                sum(r["judge"].get("faithfulness", 0) for r in scored) / n, 4
            ),
            "answer_relevancy": round(
                sum(float(r["judge"].get("answer_relevancy", 0)) for r in scored) / n, 4
            ),
            "citation_accuracy": round(
                sum(r["judge"].get("citation_accuracy", 0) for r in scored) / n, 4
            ),
            # 结构判据：悬空角标（引用了不存在的证据）——**不需要 LLM**
            "dangling_rate": round(
                sum(1 for r in rows if r["dangling"]) / max(1, len(rows)), 4
            ),
        },
        "rows": rows,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    m = report["metrics"]
    print("\n生成侧指标：")
    print(f"  faithfulness   {m['faithfulness']:.3f}")
    print(f"  answer_relevancy {m['answer_relevancy']:.3f}")
    print(f"  citation_accuracy {m['citation_accuracy']:.3f}")
    print(f"  dangling_rate  {m['dangling_rate']:.3f}（结构判据，无需 LLM）")

    reset_users_and_bm25([EVAL_EMAIL])
    dispose_engines()
    if not _LOOP.is_closed():
        _LOOP.close()
    print(f"\n报告写入 {REPORT}")
    return 0


def _sync_config(session):
    """同步读 llm_settings（本脚本在 sync/async 混合下用）。"""
    from services.retrieval.llm import load_llm_config_sync

    return load_llm_config_sync(session)


if __name__ == "__main__":
    raise SystemExit(main())
