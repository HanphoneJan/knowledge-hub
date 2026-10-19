"""trgm 腿：Postgres 子串搜索（D17/D40）。

## 为什么这腿必须存在

BM25 搜不到**非词**字符串。实测场景：手机号 `1381234`、工号 `20230001001`、
错误码、英文变量名、文件名片段 —— jieba 切出来是碎片，BM25 的 idf 权重也救不回来。
`pg_trgm` 是 Postgres 自带 contrib，做字符级三 gram 子串/模糊匹配，
正好补这个洞（`DESIGN-DATA-MODEL.md` §2.6 实测 `similarity()` 可用）。

## 为什么**不能**用 Postgres 内置分词

`to_tsvector('chinese', '向量检索与BM25混合召回的架构设计')` 实测把整行切成
**单个 token** `'向量检索与bm25混合召回的架构设计'`，搜「混合召回」匹配不到；
且 `pgvector/pgvector:pg16` 镜像内没有 zhparser/pg_jieba。
→ 这就是 D40「BM25 留 Qdrant，trgm 留 Postgres」的由来，**不要试图改**。

## ⚠️ 召回条件**只能**用 `word_similarity()` —— 试过两种"更快"的写法，都漏召回

第一版用 `ILIKE '%<整句提问>%'` 当召回条件（想借 GIN 索引），
结果**整条腿返回 0 条**，而真值 chunk 明明有 0.400。222 个 chunk 的库上实测：

| 召回条件 | 命中数 | 含真值？ |
|---|---|---|
| `content_norm ILIKE '%<整句提问>%'` | 0 | ❌ |
| `similarity(chunk, q) >= 0.1`（整段） | 0 | ❌ |
| `word_similarity(q, chunk) >= 0.25`（窗口） | **3** | ✅ 0.400 |

两个失败版本的共同根因：**它们都要求 query 与整段文本可比**。

- `ILIKE` 要求 query 是原文的**连续子串** —— 自然语言提问从来不是子串。
- `similarity()` 按整段长度归一化 —— 500 字 chunk 对 8 字 query 恒在 0.05~0.11。

第三种写法（`ILIKE OR word_similarity >= 0.25`，本想"两者取并集总不会漏"）
**也漏**，因为 `ILIKE` 在长 query 上恒假、并集≈交集 ≈ 只有一个分支起作用。
教训：OR 放宽的是**同一个错误判据**，不是换一个判据。

## ⚠️ 索引：这一腿走 Seq Scan，这是有意识的取舍

`word_similarity` **不能用 `gin_trgm_ops` 索引**（只有 `similarity` 和 `LIKE` 能）。
所以这一腿是全表扫。

本项目规模（个人知识库，几万 chunk，PG 全表扫 ~10ms 量级）扛得住。
真扛不住时正解是物化一列 token 集合或换外部搜索引擎，
**不是**把召回条件换回 `ILIKE`/`similarity` —— 那会静默漏召回，
而漏召回比慢更难发现（用户只会觉得"搜不到"）。

## ⚠️ 排序必须用 `word_similarity()` 而不是 `similarity()`

本机实测（查一个 8 字自然语言提问，chunk 平均 500 字；数字来自真实语料一次测量，
受隐私约束不引用原文）：

| 函数 | top1 | 分数 |
|---|---|---|
| `similarity(query, chunk)` | 一段**偏离答案**的表格标题 | 0.111 |
| `word_similarity(query, chunk)` | **含答案的那一段** | 0.400 |

根因：`similarity()` 对**整段**做三 gram 集合比较并按长度归一化，
500 字 chunk 对 8 字 query 天生只有 0.05~0.11 —— 噪声大于信号。
`word_similarity()` 取正文里相似度最高的**连续窗口**，于是"命中了"能压过"很长"。

用错了不会报错，只会**稳定地排错序**。所以这里写死 `word_similarity`，
并在下面留了一条单测锁住。

## `mode=exact` 的语义：这一腿**不只**服务子串搜索

契约 §5.2 说 `exact` 走 Postgres trgm。但实测表明 `word_similarity` 对自然语言
提问同样有效（上面那个 0.400 就是），所以这一腿同时承担：
- 搜非词字符串：`1381234`、`20230001001`、`WIDGET-XYZ-8899`
- 给自然语言提问提供一路**字符级**的独立召回（与 BM25 的词元级互补）

## 租户过滤

⚠️ 这一腿在 Postgres 里，拿不到 `build_qdrant_filter` 的字典。
必须调 `auth.build_kb_scope_sql` —— 它与 Qdrant 版**同源**
（都只读 `UserContext.readable_kb_ids`），fail-closed 语义一致。
**不要在这里自己 join + 复刻可见性**，那是本项目唯一一次真实越权的原因
（CONVENTIONS §0.6）。

## 软删除二次过滤

Qdrant 允许 PG 与索引短暂不一致（D7 一致性原则：`Qdrant 是可重建派生索引`），
所以向量腿可能召回已进回收站的文档。这一腿直接查 PG，顺带把
`is_current=TRUE AND deleted_at IS NULL` 过滤掉（CONVENTIONS §1.2）。
`engine` 在融合后还会再做一次统一的二次过滤。
"""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.auth import UserContext, build_kb_scope_sql
from ..common.config import get_settings
from ..common.models import Chunk, Document, KnowledgeBase
from ..common.qdrant import point_id
from .types import LEG_TRGM, NS_TEXT, LegHit, LegResult, rrf_key

#: `word_similarity()` 的下限。低于它的命中没有子串意义，直接丢 ——
#: 否则这一腿会给 RRF 灌进一大批"只是恰好共现几个字符"的噪声。
#: 实测真值是 0.400，纯噪声在 0.05~0.15，0.25 落在中间偏保守一侧。
DEFAULT_MIN_SIMILARITY = 0.25

#: 低于 3 个字符的 query 不做 trgm。三 gram 至少要 3 个字符才有 gram 可比，
#: 2 个字的 query 走这一腿会拿到一堆随机共现。
MIN_QUERY_LEN = 3


def build_stmt(
    q: str,
    ctx: UserContext,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    updated_after: dt.datetime | None = None,
    updated_before: dt.datetime | None = None,
):
    """构造 trgm 查询语句。**纯函数，不碰 session** —— 所以单测能编译出 SQL 断言。

    单独抽出来的理由：`similarity` 与 `word_similarity` 只差一个词，
    但排序结果完全不同（见模块 docstring 的实测表）。
    用错了**不会报错**，只会稳定地排错序。所以必须有一条测试
    把编译出的 SQL 钉住。
    """
    wsim = func.word_similarity(q, Chunk.content_norm)
    # ⚠️ 刻意**不 select** `knowledge_bases.visibility`。
    # 检索层一旦把可见性字段拿到手里，早晚会被写成一句
    # `if visibility == "workspace": ...` —— 那就是 CONVENTIONS §0.6
    # 那个真实越权的成因（两处复刻可见性逻辑）。
    # 可见性判定只在 `auth.load_user_context` 与 `kb.py::_visible_kb_filter`。
    # `workspace_id` 留着是因为它进 payload 供审计与前端展示，不参与判定。
    return (
        select(
            Chunk.doc_id,
            Chunk.chunk_index,
            Chunk.kind,
            Chunk.locations,
            Chunk.content_norm,
            Document.file_name,
            Document.kb_id,
            Document.mime_type,
            KnowledgeBase.source_kind,
            KnowledgeBase.workspace_id,
            wsim.label("wsim"),
        )
        .join(Document, Document.id == Chunk.doc_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            # ① 租户过滤 —— 唯一入口，与 Qdrant 版同源（CONVENTIONS §0.2）
            build_kb_scope_sql(ctx, kb_id, KnowledgeBase.id, kb_ids=kb_ids),
            # ② 软删除二次过滤（CONVENTIONS §1.2）
            Document.is_current.is_(True),
            Document.deleted_at.is_(None),
            KnowledgeBase.deleted_at.is_(None),
            # D199：时间过滤（非租户条件，见 qdrant.with_updated_range 的说明）。
            #   ⚠️ 与租户条件是**并列的 and**，不是 or（§0.6）。
            *([Document.updated_at >= updated_after] if updated_after else []),
            *([Document.updated_at <= updated_before] if updated_before else []),
            # ③ **唯一的召回条件**。实测（222 chunk 的库，8 字自然语言提问）：
            #
            #    条件                                        命中
            #    ILIKE '%整句%'                              0 条 ← 自然语言不是子串
            #    similarity(chunk, q) >= 0.1                 0 条 ← 整段长度归一化后恒低
            #    word_similarity(q, chunk) >= 0.25           3 条，含真值 0.400 ✅
            #
            #    前两行都漏掉真值，所以它们**不能**作为过滤条件。
            #    （这两条各配一个 OR 分支的写法也试过，仍然漏 ——
            #     因为 OR 只是放宽，而 word_similarity 那条阈值才是对的。
            #     详见模块 docstring "为什么召回条件不能只用 ILIKE"。）
            #
            # ⚠️ 代价：`word_similarity` **不能用 GIN 索引**（实测走 Seq Scan）。
            #   本项目规模（个人知识库，几万 chunk）扛得住；
            #   真到扛不住时正解是给 `chunks.content_norm` 加
            #   `gin_trgm_ops` 之外的物化列或改用外部搜索引擎，
            #   **不是**把召回条件换回 ILIKE（那会静默漏召回）。
            wsim >= min_similarity,
        )
        .order_by(wsim.desc())
    )


async def search_trgm(
    session: AsyncSession,
    ctx: UserContext,
    query: str,
    *,
    kb_id: int | None = None,
    kb_ids: list[int] | None = None,
    limit: int = 30,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    updated_after: str | None = None,
    updated_before: str | None = None,
) -> LegResult:
    """trgm 子串召回。

    @param limit 取多少候选交给 RRF。trgm 的分数不是概率，
        取深一点让 RRF 有更多机会把它认可的结果捞上来。
    @param updated_after / updated_before RFC3339 字符串（与 Qdrant 腿同一口径）。
        解析失败就**忽略时间过滤**（这是增强，不该因为一个坏参数让整条腿失败）。
    """
    t0 = time.perf_counter()
    q = (query or "").strip()

    def _parse(s: str | None) -> dt.datetime | None:
        if not s:
            return None
        try:
            return dt.datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    if len(q) < MIN_QUERY_LEN:
        return LegResult(
            leg=LEG_TRGM,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            skipped=f"query 短于 {MIN_QUERY_LEN} 字，三 gram 无可比",
        )

    try:
        # 🔑 D180：库一大，`word_similarity` 的 **seq scan** 就是秒级 ——
        #   实测（本机，~55 字/行的合成语料）：1 万 ≈ 0.18s、10 万 ≈ 1.4s、
        #   100 万 ≈ **13.9s**，远超 D46 的 500ms 预算。GiST 索引也救不了
        #   （实测规划器仍走 Seq Scan：函数在 WHERE 里，行数命中率低时索引不划算）。
        #   ⇒ 给它一个**语句超时**，超了就把这一腿降级成 `skipped`（如实说明），
        #   文本召回仍由 dense / sparse 承担。不是静默丢 —— skipped 会带出去。
        budget_ms = get_settings().trgm_statement_timeout_ms
        if budget_ms and budget_ms > 0:
            await session.execute(
                text(f"SET LOCAL statement_timeout = {int(budget_ms)}")
            )
        rows = (
            await session.execute(build_stmt(
                q, ctx, kb_id=kb_id, kb_ids=kb_ids,
                min_similarity=min_similarity,
                updated_after=_parse(updated_after),
                updated_before=_parse(updated_before),
            ).limit(limit))
        ).all()
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - t0) * 1000)
        blob = f"{type(exc).__name__}: {exc}".lower()
        if "timeout" in blob or "querycanceled" in blob:
            return LegResult(
                leg=LEG_TRGM, elapsed_ms=elapsed,
                skipped=(
                    f"trgm 腿超时（>{budget_ms}ms，D180）：库大时 word_similarity "
                    "是 seq scan，已降级；文本召回仍由 dense/sparse 承担。"
                    "（调 PKH_TRGM_STATEMENT_TIMEOUT_MS，或按 D70 换物化/外部引擎）"
                ),
            )
        return LegResult(
            leg=LEG_TRGM, elapsed_ms=elapsed, skipped=f"trgm 腿失败: {exc}"
        )

    hits: list[LegHit] = []
    for rank, r in enumerate(rows, start=1):
        (
            doc_id, chunk_index, ck_kind, locations, content_norm,
            file_name, doc_kb_id, mime,
            source_kind, ws_id, wsim,
        ) = r
        # ⚠️ 复算点 ID（模块 docstring）：trgm 没有 Qdrant 点，
        # 但 `point_id(kb_id, doc_id, chunk_index)` 就是同一个值，
        # 于是这一腿的命中能直接和 dense/sparse 在 RRF 里合并。
        pid = point_id(doc_kb_id, doc_id, chunk_index)
        hits.append(
            LegHit(
                key=rrf_key(NS_TEXT, pid),
                point_id=pid,
                doc_id=doc_id,
                chunk_index=chunk_index,
                rank=rank,
                score=float(wsim),
                leg=LEG_TRGM,
                # 补齐成与 Qdrant payload 同构的 dict，
                # 这样 `engine` / `ask` 不需要关心命中来自哪条腿
                payload={
                    "kb_id": doc_kb_id,
                    "doc_id": doc_id,
                    "chunk_index": chunk_index,
                    "workspace_id": ws_id,
                    "source_kind": source_kind,
                    "file_name": file_name,
                    "mime": mime,
                    "kind": ck_kind,
                    # ⚠️ 刻意**不**填 `page`：这一腿查到的是 chunk，
                    # 页码要看 `locations[0]["page"]`（D33 的坐标里才有）。
                    # 填一个假的页码会让前端的"跳到第 N 页"跳错地方。
                    "text_norm": content_norm,
                    "text_raw": content_norm,
                    **({"locations": locations} if locations else {}),
                },
            )
        )

    return LegResult(
        leg=LEG_TRGM, hits=hits, elapsed_ms=int((time.perf_counter() - t0) * 1000)
    )


def _escape_like(s: str) -> str:
    """转义 LIKE 通配符。

    ⚠️ 当前**没有**任何 SQL 依赖它（召回条件是 `word_similarity`，不是 ILIKE），
    保留是因为 `highlight_spans` 与将来的精确子串端点都要处理这类字符，
    而且**它是一个纯函数、可以单测** —— 与它是否被 SQL 调用无关。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def highlight_spans(
    text: str, query: str, *, max_spans: int = 3, context: int = 30
) -> list[dict[str, object]]:
    """子串命中位置，供 `SearchHit.snippets` 用（契约 §5.2）。

    返回 `[{"text": 上下文, "highlight": 命中串, "positions": [[start, end]]}]`，
    偏移是**相对 `text`（上下文片段）**的，前端直接拿它渲染高亮。
    """
    import re

    q = (query or "").strip()
    if not q or not text:
        return []
    spans: list[dict[str, object]] = []
    for m in re.finditer(re.escape(q), text):
        s = max(0, m.start() - context)
        e = min(len(text), m.end() + context)
        spans.append(
            {
                "text": text[s:e],
                "highlight": q,
                # 相对片段的偏移 —— 不是相对原文
                "positions": [[m.start() - s, m.end() - s]],
            }
        )
        if len(spans) >= max_spans:
            break
    return spans