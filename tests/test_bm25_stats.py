"""🔑 防回归：`corpus_stats` 与 `terms.df` 会**静默**漂移（PROGRESS §5.29）。

这是 1d 实现时实测踩到的最隐蔽的一个坑：BM25 的统计量只在两个地方被维护
—— 入库（`register_chunks`）与 worker cleanup（`unregister_chunks`，30 天后）。
于是**任何绕开这两处的删除**（级联删用户、测试脚本清库、删工作区）
都会让统计量虚高，而后果**不是报错**，是检索质量悄悄变差。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from services.common.db import sync_session_scope
from services.common.models import Chunk, CorpusStat, Document, Job, Term
from services.retrieval import bm25
from services.retrieval.terms import get_corpus_stats


def _live_chunk_count(s) -> int:
    """库里**全部** chunk 行（含软删未 purge 的）。

    🔑 口径 = **purge 生命周期**（D115），必须与 `handle_reindex` 一致：

    | 路径 | 登记范围 |
    |---|---|
    | 入库 `register_chunks` | 当时的活文档（软删**不清 df**，D26 的 30 天窗口内还能恢复） |
    | 重建 `handle_reindex` | 全部 chunk（软删的也登记） |

    ⚠️ 这个函数**曾经**按 `is_current` 过滤（"活 chunk 数"），
    而 `handle_reindex` 也曾经只登记活文档 —— 两者自洽，但与**入库路径**
    不自洽：回收站里有一份文档时，`n_docs`（552）与活 chunk（544）差 8。
    两处口径必须**一起**改，否则任何不变量都不成立
    （`deploy/monitor.py` 每小时报一次假警）。

    ⚠️ 别拿"活 chunk 数"当 df 判据：那是**向量侧**口径
    （`admin.py::_pg_point_ids` 与 reindex 的向量重建都按活文档算）。
    """
    return int(s.execute(select(func.count()).select_from(Chunk)).scalar_one())


def _all_chunk_count(s) -> int:
    """表里**全部** chunk 行（含软删/历史版本）。

    用于 `df ≤ N` 那类**上界**断言：df 只统计活文档，所以上界要用
    全表数 —— 用活文档数会让"软删文档的残留 df"看起来合法。
    """
    return int(s.execute(select(func.count()).select_from(Chunk)).scalar_one())


def _wait_queue_idle(s, timeout: float = 90.0) -> bool:
    """等任务队列排空再断言 df（🔑 df 一致性是**静止状态**性质）。

    `register_chunks` / `unregister_chunks` 都在 worker 的事务里做，
    所以刚跑完 `eval/api_1c_check.py`（它导入几百个文档）立刻跑 pytest，
    会量到"文档已入库、df 还没登记"的**中间态** → 这条断言假红。

    ⚠️ 实测过这个假红：`n_docs=544` vs `活 chunk=552`（差 8 = 1c 刚导入的
    8 个 chunk），而队列排空后两者正好相等。

    → 等，但**有上限**：真的漂移时要红，不能永远等下去（那会让测试挂住）。
    返回值 = **是否真的排空**了（`True`/`False`），调用方**必须**据此决定
    是断言还是跳过。

    ⚠️ 第一版这里返回"等了多少轮"，而调用方**根本没用它** ——
    于是超时之后照样拿中间态去比，报出一个看起来像"df 漂移"的假红。
    （与 §5.102 同一个形状：一个为"让人判断"而设计的返回值，没人用。
    本轮实测踩到：后台脚本还在跑时 pytest 红了 `n_docs=612 vs 609`。）
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = int(
            s.execute(
                select(func.count()).select_from(Job).where(
                    Job.status.in_(("pending", "running"))
                )
            ).scalar_one()
        )
        if busy == 0:
            return True
        time.sleep(1.0)
    return False


def _true_avgdl(s) -> float:
    """真实平均 chunk 长度（与 n_docs 同口径：全部 chunk 行）。"""
    rows = s.execute(select(Chunk.content_norm)).scalars().all()
    if not rows:
        return 1.0
    return max(1.0, sum(len(bm25.tokenize(r)) for r in rows) / len(rows))


def _pg_alive() -> bool:
    try:
        with sync_session_scope() as s:
            s.execute(select(func.count()).select_from(Chunk)).scalar_one()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_corpus_stats_matches_live_chunks():
    """🔑 `n_docs` 必须等于库里 chunk 行数。

    虚高的后果**不是报错**而是检索质量悄悄变差：
    `idf = log(1 + (N - df + .5)/(df + .5))` 里 N 虚高 → 所有词的 idf 被压平
    → 罕见词不再罕见 → 短 chunk 里单个高频字盖过长 chunk 的多词共现。
    实测第 4 轮跑 1c 时 n_docs=1332 vs 真实 222（6 倍），
    sparse 腿 top1 从答案段落变成一个表格标题。

    ⚠️ 这条断言会**主动失败**提醒重跑 `handle_reindex`。
    日常跑 1c/1d 时脚本自己会重建（`eval/_reset.py`），所以不该失败；
    如果失败了，说明有删除路径绕开了 `unregister_chunks`。
    """
    with sync_session_scope() as s:
        # 🔑 队列没排空就**不比**：`n_docs` 是静止态不变量，
        #   中间态比出来的差值会被误读成"df 漂移"（假红，且会让人去
        #   跑一次 reindex —— 而真正的原因是"还有任务在跑"）。
        if not _wait_queue_idle(s):
            pending = int(
                s.execute(
                    select(func.count()).select_from(Job).where(
                        Job.status.in_(("pending", "running"))
                    )
                ).scalar_one()
            )
            pytest.skip(
                f"任务队列还有 {pending} 个未完成任务，df 处于中间态，"
                "本轮不验证静止态不变量（等 worker 排空后重跑）"
            )
        real = _live_chunk_count(s)
        if real == 0:
            pytest.skip("库里没有 chunk")
        n_docs, avgdl = get_corpus_stats(s)
        assert n_docs == real, (
            f"corpus_stats.n_docs={n_docs} 但库里只有 {real} 个 chunk 行。"
            "统计量漂移了 —— 跑 `handle_reindex`（KIND_REINDEX）重建，"
            "并检查是不是有删除路径绕开了 terms.unregister_chunks"
        )
        assert avgdl == pytest.approx(_true_avgdl(s), rel=0.05), (
            f"avgdl={avgdl:.1f} 与真实值 {_true_avgdl(s):.1f} 差太多"
        )


def test_df_invariant_is_skipped_not_compared_while_queue_is_busy(monkeypatch):
    """🔑 队列繁忙时必须**跳过**，不能拿中间态去比。

    `n_docs` 是**静止态**不变量。还在摄取时比出来的差值会被误读成
    "df 漂移"，而真正的原因只是"有任务在跑" —— 那种假红会让人
    去跑一次 reindex（几分钟），而问题根本不存在。

    ⚠️ 这条测试的存在理由：第一版 `_wait_queue_idle` 返回"等了多少轮"，
    调用方**没用那个返回值**，于是超时后照样断言 ——
    本轮实测踩到（后台脚本还在跑时红成 `n_docs=612 vs 609`）。
    """
    from tests import test_bm25_stats as mod

    monkeypatch.setattr(mod, "_wait_queue_idle", lambda *a, **k: False)
    with pytest.raises(pytest.skip.Exception):
        mod.test_corpus_stats_matches_live_chunks()


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_no_term_df_exceeds_total_chunks():
    """🔑 任何词的 `df` 都不能超过 chunk 总数。

    `df > N` 意味着 `idf` 变负 —— BM25 会**惩罚**这个词，
    于是越常用的词越搜不到。这是"重复登记"的直接指纹。
    """
    with sync_session_scope() as s:
        total = _all_chunk_count(s)
        if total == 0:
            pytest.skip("库里没有 chunk")
        over = (
            s.execute(select(func.count()).select_from(Term).where(Term.df > total))
            .scalar_one()
        )
        assert over == 0, (
            f"有 {over} 个词的 df 超过 chunk 总数 {total} —— "
            "词表被重复登记了（多半是删除时漏了 unregister_chunks）"
        )


@pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")
def test_idf_stays_positive_for_all_terms():
    """🔑 `bm25.idf` 的 `+1` 保证 idf 恒正（bm25.py 的注释）。

    一旦某个词的 idf 变负，BM25 会反向惩罚它 —— 罕见词反而搜不到。
    """
    with sync_session_scope() as s:
        total = _all_chunk_count(s)
        if total == 0:
            pytest.skip("库里没有 chunk")
        rows = s.execute(select(Term.term, Term.df).where(Term.df > 0)).all()
        negatives = [term for term, df in rows if bm25.idf(df, total) <= 0]
        assert not negatives, (
            f"这些词的 idf ≤ 0（df > N={total}）：{negatives[:5]}"
        )

# ============================================================
# 守卫：测试文件不许**绕开** df 的维护路径删数据（D146）
# ============================================================


def test_tests_never_hard_delete_without_rebuilding_bm25():
    """🔑 `tests/` 里不许出现裸的 `DELETE FROM users|chunks|documents|kb`。

    🔴 **本文件自己被这个坑咬过**：D145 新写的 `test_a2a_skill_schemas.py`
    清场时用 `DELETE FROM users WHERE email LIKE …`，级联干掉 chunks 而
    **不动 `terms.df`** ⇒ 全量基线里 pytest 段**假红**在
    `test_corpus_stats_matches_live_chunks`。

    ⚠️ 之所以**难发现**，是因为它**顺序依赖**：紧跟着的 eval 脚本自己会
    `reset_users_and_bm25()` 重建，于是"跑一遍全量基线、下一次 pytest 就绿了"，
    看起来像偶发（我真被骗了一轮，先以为是自己新加的断言有问题）。

    唯一合规的清场入口是 `conftest.purge_users` fixture
    （内部走 `eval/_reset.reset_users_and_bm25`：删用户 **+ 重建 BM25/向量**）。

    ⚠️ 判据用 **AST 找字符串字面量**而不是全文搜：第一版全文搜把**本函数
    docstring 里提到的那句 `DELETE FROM users`** 也当成真代码，红得很有道理
    却毫无意义（§5.137 那种"自己打自己"）。真硬删一定藏在
    `text("DELETE …")` / `f"DELETE …"` 这种 SQL 字面量里。

    ⚠️ **本守卫的盲区**（写下来是为了下一个人知道边界在哪）：
    - 只扫 `tests/*.py`：`eval/` 下用 `drop_users` 是**允许**的
      （它后面紧跟着 `rebuild_bm25()`）。
    - 抓不到**不经字符串**的硬删（`s.execute(Chunk.__table__.delete())`
      这类 SQLAlchemy 表达式）—— 那种要靠上面那条**行为**断言兜底。
    - 它也抓不到 `purge_users` 内部被改成"只删不重建"（那半边的缺失），
      因为重建不在字符串里。
    """
    import ast
    import re
    from pathlib import Path

    pattern = re.compile(
        r"delete\s+from\s+[\"`\']?(users|chunks|documents|knowledge_bases)\b",
        re.I,
    )

    def _docstring_ids(tree: ast.AST) -> set[int]:
        """所有 docstring 节点的 `id()`（它们是散文，不是代码）。"""
        out: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
        return out

    def _literal_text(node: ast.Constant | ast.JoinedStr) -> str:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else ""
        return "".join(p.value for p in node.values
                       if isinstance(p, ast.Constant) and isinstance(p.value, str))

    tests_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for f in sorted(tests_dir.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        # ⚠️ f-string 的**内层** Constant 要一起跳过：`f"DELETE FROM users …{x}"`
        #   在树上既是 JoinedStr 又含一个 Constant 字面量，两个都匹配 ⇒ 同一次
        #   硬删被数成两条（第一版就这么报 4 条，实际只有 2 处）。
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                skip.update(id(v) for v in node.values)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            if id(node) in skip:
                continue
            m = pattern.search(_literal_text(node))
            if m:
                offenders.append(f"{f.name}: {m.group(0)!r}")
    assert not offenders, (
        "测试里出现了绕开 df 维护的硬删（会让 corpus_stats.n_docs 虚高，"
        "下一次 pytest 假红）—— 改用 `conftest.purge_users` fixture：\n"
        + "\n".join(offenders)
    )
