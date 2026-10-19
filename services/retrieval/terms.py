"""BM25 词表的增量维护。

`retrieval/bm25.py` 是纯计算，本模块负责**状态**：`terms` 表与 `corpus_stats`。

## 为什么需要它

旧的 BM25 是"跑一遍全语料生成词表，落盘 json"。产品化后文档是**增量**入库的，
每传一个文件重建全库词表不可行（4600 chunk 要 2 秒，且多租户下互相影响）。

## idf 漂移是已知折中

新词入库 → N 变大 → 旧词的 idf 偏高。修法是定期跑 `KIND_REINDEX` 重算。
实测漂移的影响远小于漏召回，所以默认不自动重算。

## 并发

多 worker 可能同时入库。用 `INSERT ... ON CONFLICT DO UPDATE` 保证
`terms` 行的唯一性，`corpus_stats` 用 `SELECT ... FOR UPDATE` 原子自增。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..common.models import CorpusStat, Term

logger = logging.getLogger(__name__)

#: 单次 upsert 的词数上限。太多会让 SQL 变得巨大且慢。
_BATCH = 2000


class TermRegistry:
    """一次调用内的词表缓存。

    一次入库会用到几百个词，逐个查 PG 太慢。这里先查一次全量，
    内存里补齐缺失的，结束时一次性落库。
    """

    def __init__(self, session: Session):
        self.session = session
        #: 词 → term_id（已有 + 本次新建）
        self.ids: dict[str, int] = {}
        #: term_id → df
        self.df: dict[int, int] = {}
        self._new_terms: list[str] = []
        self._loaded = False

    def ensure_loaded(self, terms: set[str]) -> None:
        """加载词表，并把 `terms` 里没有的登记为待新建。"""
        if not terms:
            self._loaded = True
            return

        unknown = terms - self.ids.keys()
        # 分批 IN 查询（PG 参数上限 65535，2000 一批很安全）
        todo = sorted(unknown)
        found: dict[str, int] = {}
        for i in range(0, len(todo), _BATCH):
            batch = todo[i : i + _BATCH]
            rows = self.session.execute(
                select(Term.term, Term.id, Term.df).where(Term.term.in_(batch))
            ).all()
            for term, tid, df in rows:
                found[term] = tid
                self.df[tid] = df

        self.ids.update(found)
        for term in todo:
            if term not in found:
                self._new_terms.append(term)
        self._loaded = True

    def flush(self) -> dict[str, int]:
        """把新词落库，返回 `词 → term_id`。

        `ON CONFLICT DO UPDATE` 而不是 `DO NOTHING` —— 并发时另一个 worker
        可能刚插了同一个词，用 DO NOTHING 会导致 `RETURNING` 少一行，
        那个词拿不到 id（`None` 会污染稀疏向量）。
        """
        if not self._new_terms:
            return {}

        batch = sorted(set(self._new_terms))
        ids: dict[str, int] = {}
        for i in range(0, len(batch), _BATCH):
            chunk = batch[i : i + _BATCH]
            res = self.session.execute(
                pg_insert(Term)
                .values([{"term": t, "df": 0} for t in chunk])
                .on_conflict_do_update(
                    index_elements=[Term.term], set_={"term": Term.term}
                )
                .returning(Term.term, Term.id)
            )
            for term, tid in res:
                ids[term] = tid
                self.df.setdefault(tid, 0)

        self._new_terms = []
        self.ids.update(ids)
        return ids

    def dfs_by_id(self, terms: set[str]) -> dict[int, int]:
        """取指定词的 `term_id → df`。"""
        return {self.ids[t]: self.df.get(self.ids[t], 0) for t in terms if t in self.ids}


# ============================================================
# 异步版（API 进程的查询路径）
# ============================================================
#
# ⚠️ 为什么必须分成两个名字而不是只写一个（CONVENTIONS §0.9）：
#
# **写**路径在 worker（同步 Session），**读**路径在 API 的检索腿（AsyncSession）。
# 早先只有同步版，于是在 AsyncSession 上调 `session.execute(...)` ——
# **不报错**，只是返回一个从未被 await 的 coroutine，症状是
# `AttributeError: 'coroutine' object has no attribute 'all'`。
#
# 这与 PROGRESS §5.28 记的三处是同一个根因，症状也一样隐蔽。
# 已有 AST 扫描测试（tests/test_unawaited_coroutines.py）兜底。


class TermRegistryAsync:
    """`TermRegistry` 的异步版。**只读**，不改 df —— 查询路径不该动统计量。

    与同步版的区别：
    - 不有 `flush()`：查询时遇到 OOV 词就是 OOV，不该往 `terms` 表插新词
      （那会让一个随机的用户查询污染全局词表与 idf）
    - `df` 直接跟着 `ids` 一次查出来
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.ids: dict[str, int] = {}
        self.df: dict[int, int] = {}

    async def ensure_loaded(self, terms: set[str]) -> None:
        if not terms:
            return
        todo = sorted(terms - self.ids.keys())
        for i in range(0, len(todo), _BATCH):
            batch = todo[i : i + _BATCH]
            rows = (
                await self.session.execute(
                    select(Term.term, Term.id, Term.df).where(Term.term.in_(batch))
                )
            ).all()
            for term, tid, df in rows:
                self.ids[term] = tid
                self.df[tid] = df

    def dfs_by_id(self, terms: set[str]) -> dict[int, int]:
        return {self.ids[t]: self.df.get(self.ids[t], 0) for t in terms if t in self.ids}

    def oov(self, terms: set[str]) -> set[str]:
        """查不到词表的词。调方可据此提示"这个查询词全都没被索引过"。"""
        return {t for t in terms if t not in self.ids}


def register_chunks(
    session: Session,
    chunk_tokens: list[list[str]],
    *,
    increment_df: bool = True,
) -> TermRegistry:
    """把一批 chunk 的 token 登记进词表。

    @param increment_df False 用于删除/重算场景（先减后加，或只查不增）
    """
    reg = TermRegistry(session)
    reg.ensure_loaded({t for toks in chunk_tokens for t in toks})
    reg.flush()

    if not increment_df:
        return reg

    # df 增量 —— 与 `unregister_chunks` 共用 `_df_delta`：
    # 加与减必须算出同一个 delta，否则一边多算一边少算，df 就永久偏掉（§5.29）
    delta, total_len = _df_delta(chunk_tokens)

    if delta:
        # 按 term 批量更新
        terms = sorted(delta)
        for i in range(0, len(terms), _BATCH):
            batch = terms[i : i + _BATCH]
            rows = session.execute(
                select(Term).where(Term.term.in_(batch)).with_for_update()
            ).scalars().all()
            for row in rows:
                row.df += delta[row.term]

    bump_corpus_stats(session, n_docs=len(chunk_tokens), total_len=total_len)
    return reg


def _df_delta(chunk_tokens: list[list[str]]) -> tuple[Counter, int]:
    """一批 chunk 的 df 增量与总 token 数。

    抽成纯函数是因为 `unregister_chunks` 与它的 async 孪生**必须算出同一个
    delta** —— 两份实现漂移的后果是 df 减错，而 df 减错**不报错**，
    只让检索质量悄悄变差（PROGRESS §5.29）。
    """
    delta: Counter = Counter()
    total_len = 0
    for toks in chunk_tokens:
        delta.update(set(toks))
        total_len += len(toks)
    return delta, total_len


def unregister_chunks(session: Session, chunk_tokens: list[list[str]]) -> None:
    """删除一批 chunk 时把 df 和统计量减回去。

    ⚠️ df 不会减到负数以下的实现要靠 `GREATEST(0, df - n)`。
    漏减的后果是 idf 越来越小 → 罕见词也拿高分 → 检索结果被噪声淹没。
    """
    delta, total_len = _df_delta(chunk_tokens)

    if delta:
        terms = sorted(delta)
        for i in range(0, len(terms), _BATCH):
            batch = terms[i : i + _BATCH]
            rows = session.execute(
                select(Term).where(Term.term.in_(batch)).with_for_update()
            ).scalars().all()
            for row in rows:
                row.df = max(0, row.df - delta[row.term])

    bump_corpus_stats(session, n_docs=-len(chunk_tokens), total_len=-total_len)


async def unregister_chunks_async(
    session: AsyncSession, chunk_tokens: list[list[str]]
) -> None:
    """异步版（API 用）。见 CONVENTIONS §0.9。

    ⚠️ 别在 `AsyncSession` 上调同步版：不报错，只是返回一个从未 await 的
    coroutine，df 就悄悄不减了（§5.28 第 3 例的同源问题）。
    """
    delta, total_len = _df_delta(chunk_tokens)

    if delta:
        terms = sorted(delta)
        for i in range(0, len(terms), _BATCH):
            batch = terms[i : i + _BATCH]
            rows = (
                (
                    await session.execute(
                        select(Term).where(Term.term.in_(batch)).with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.df = max(0, row.df - delta[row.term])

    await bump_corpus_stats_async(
        session, n_docs=-len(chunk_tokens), total_len=-total_len
    )


def get_corpus_stats(session: Session) -> tuple[int, float]:
    """返回 `(n_docs, avgdl)`。没有记录时返回 `(0, 1.0)`。

    avgdl 至少为 1 —— 语料为空时 `dl / avgdl` 会除零。
    """
    row = session.get(CorpusStat, 1)
    if row is None or row.n_docs <= 0:
        return 0, 1.0
    return row.n_docs, max(1.0, row.total_len / row.n_docs)


async def get_corpus_stats_async(session: AsyncSession) -> tuple[int, float]:
    """异步版。见 §0.9 —— 与同步版必须是两个名字。

    ⚠️ 在 AsyncSession 上调同步版 `session.get(...)` **不报错**：
    它返回一个未 await 的 coroutine，然后 `.n_docs` 报
    `AttributeError: 'coroutine' object has no attribute 'n_docs'`。
    """
    row = (
        await session.execute(select(CorpusStat).where(CorpusStat.id == 1))
    ).scalar_one_or_none()
    if row is None or row.n_docs <= 0:
        return 0, 1.0
    return row.n_docs, max(1.0, row.total_len / row.n_docs)


def bump_corpus_stats(session: Session, *, n_docs: int, total_len: int) -> None:
    """原子更新全局统计量。

    用 `SELECT ... FOR UPDATE` 而不是 `UPDATE ... SET n = n + 1`：
    后者在并发下会丢更新（两个 worker 都读到旧值再写回）。
    实际上 `UPDATE ... SET n_docs = n_docs + :d` 本身是原子的，
    但**要同时更新 updated_at 且保证行存在**，所以先 upsert 再自增。
    """
    session.execute(
        pg_insert(CorpusStat)
        .values(id=1, n_docs=0, total_len=0, updated_at=dt.datetime.now(dt.UTC))
        .on_conflict_do_update(
            index_elements=[CorpusStat.id], set_={"id": CorpusStat.id}
        )
    )
    # GREATEST(0, ...) 兜底：删文档比加文档多时不会出负数
    session.execute(
        CorpusStat.__table__.update()
        .where(CorpusStat.id == 1)
        .values(
            n_docs=func.greatest(0, CorpusStat.n_docs + n_docs),
            total_len=func.greatest(0, CorpusStat.total_len + total_len),
            updated_at=dt.datetime.now(dt.UTC),
        )
    )


async def bump_corpus_stats_async(
    session: AsyncSession, *, n_docs: int, total_len: int
) -> None:
    """异步版 `bump_corpus_stats`。语义必须与同步版一致。

    `GREATEST(0, ...)` 的兜底同样重要：删文档比加文档多时不会出负数
    （`DELETE /trash` 一次能删掉上百份文档）。
    """
    await session.execute(
        pg_insert(CorpusStat)
        .values(id=1, n_docs=0, total_len=0, updated_at=dt.datetime.now(dt.UTC))
        .on_conflict_do_update(
            index_elements=[CorpusStat.id], set_={"id": CorpusStat.id}
        )
    )
    await session.execute(
        CorpusStat.__table__.update()
        .where(CorpusStat.id == 1)
        .values(
            n_docs=func.greatest(0, CorpusStat.n_docs + n_docs),
            total_len=func.greatest(0, CorpusStat.total_len + total_len),
            updated_at=dt.datetime.now(dt.UTC),
        )
    )


def reset_terms(session: Session) -> None:
    """清空词表（`KIND_REINDEX` 全量重建的第一步）。"""
    session.execute(Term.__table__.delete())
    session.execute(CorpusStat.__table__.delete())
    logger.info("BM25 词表已清空")
