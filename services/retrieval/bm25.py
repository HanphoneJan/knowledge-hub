"""BM25 分词与权重（稀疏腿，D17/D40）。

**逻辑来自 `index/sparse_index.py`**（已验证：sparse 腿 R@10=0.756，是四路里最强的一条），
但**存储方式改了**：

| | 旧（`index/sparse_index.py`） | 新（本模块） |
|---|---|---|
| 词表 | 全量跑一遍语料生成，落盘 `data/bm25_model.json` | PG `terms` 表，增量维护 |
| idf | 建索引时一次算好 | 随 `corpus_stats` 变化，查询时算 |
| 适用 | 离线批处理 | 多租户 + 增量入库 |

**为什么必须改**：产品化后文档是**传一份就入库一份**。
按旧做法，每传一个文件就要重建全库词表 —— 4600 个 chunk 跑一遍要 2 秒，
而且多租户下谁的文档都会影响别人的 idf。

**idf 漂移**：新词入库会让旧词的 idf 偏高（因为 N 变大）。
这是 BM25 增量化的固有折中。修法是定期跑 `reindex` 任务重算
（`jobs.py` 的 `KIND_REINDEX`）。实测漂移的影响远小于漏召回。

⚠️ **日期归一化 token 必须保留**（见 `_DATE_RE`）：
`2025-02-14` 会被 ASCII 正则切成 2025/02/14，而查询里的「2025年2月14日」
切成 2025/年/2/月/14/日 —— 两边**没有任何共同 token** 表达"这一天"，
BM25 又无法表达"多词共现"，于是按日期提问几乎必然召不回真值。
加上 `d20250214` 后，该类 query 的 R@10 从 **0.20 → 1.00**（实测）。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

logger = logging.getLogger(__name__)

#: BM25 参数。沿用旧实现的值（已验证效果）
K1 = 1.5
B = 0.75

ASCII_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+#.\-]*|\d+")
CJK_RE = re.compile(r"[一-鿿]+")
_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?(?!\d)"
)

#: 中文停用词。⚠️ 不能省 —— 单字兜底会把「的了是在」灌进语料，
#: 它们 tf 极高、权重盖过实词，BM25 精度会塌。
STOPWORDS = frozenset(
    """
    的 了 是 在 我 你 他 她 它 我们 你们 他们 她们 咱们 这 那 这个 那个 这些 那些
    和 与 或 也 就 都 而 及 着 又 但 但是 因为 所以 如果 虽然 然后 于是 而且 并且
    有 没 没有 不 吗 呢 吧 啊 呀 哦 嗯 一个 一些 什么 怎么 如何 为什么 可以
    上 下 中 里 内 外 前 后 左 右 时 个 们 地 得 着 过 很 太 最 更 还 再 又
    之 其 所 以 于 为 对 从 到 给 把 被 让 使 会 能 要 想 说 看 做 去 来 走
    一 二 三 四 五 六 七 八 九 十 百 千 万 零
    """.split()
)

_jieba_ready = False


def date_tokens(text: str) -> list[str]:
    """抽取日期并归一化为 `dYYYYMMDD`。"""
    out: list[str] = []
    for m in _DATE_RE.finditer(text or ""):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        try:
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                out.append(f"d{y}{int(mo):02d}{int(d):02d}")
        except ValueError:
            continue
    return out


def tokenize(text: str) -> list[str]:
    """分词：日期归一化 token + jieba 中文词 + 中文单字兜底 + 英文数字词。"""
    global _jieba_ready
    text = (text or "").lower()
    toks: list[str] = list(date_tokens(text))
    if CJK_RE.search(text):
        import jieba

        if not _jieba_ready:
            jieba.setLogLevel(60)
            _jieba_ready = True
        for seg in CJK_RE.findall(text):
            words = [w for w in jieba.cut(seg, cut_all=False) if w.strip() and w not in STOPWORDS]
            toks.extend(words)
            # 短片段（≤2 字）本身就是词，不再拆单字；长片段补单字兜底
            if len(seg) > 2:
                toks.extend(c for c in seg if c not in STOPWORDS)
    toks.extend(ASCII_RE.findall(text))
    return [t for t in toks if t.strip()]


# ============================================================
# BM25 权重
# ============================================================


def idf(df: int, n_docs: int) -> float:
    """BM25 的 IDF 项。

    与旧实现一致：`log(1 + (N - df + 0.5) / (df + 0.5))`。
    那个 `+1` 很重要 —— 它保证 idf 恒为正，罕见词不会被 BM25 惩罚成负分。
    """
    return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))


def doc_weight(tf: int, df: int, n_docs: int, dl: int, avgdl: float) -> float:
    """文档侧的 BM25 权重（存进 Qdrant 稀疏向量）。

    两侧都用同一个函数，Qdrant 内部算点积 = BM25 得分，无需自己遍历语料。
    """
    avgdl = avgdl or 1.0
    norm = K1 * (1 - B + B * dl / avgdl)
    return idf(df, n_docs) * (tf * (K1 + 1)) / (tf + norm)


def query_weight(tf: int, df: int, n_docs: int) -> float:
    """查询侧的 BM25 权重。

    与文档侧**公式不同**（分母是 `tf + k1` 而不是 `tf + norm`）——
    这是标准 BM25 的定义，少了它检索质量会明显下降。
    """
    return idf(df, n_docs) * (tf * (K1 + 1)) / (tf + K1)


def sparse_to_dict(
    tokens: list[str],
    term_ids: dict[str, int],
    dfs_by_id: dict[int, int],
    n_docs: int,
    avgdl: float,
    *,
    is_query: bool = False,
) -> dict[int, float]:
    """token 列表 → `{term_id: weight}`。

    @param term_ids 词 → term_id（来自 PG `terms` 表）
    @param dfs_by_id term_id → 文档频率（**已按 term_id 索引**）
    @param is_query True 用查询侧公式，False 用文档侧公式

    ⚠️ `dfs_by_id` 刻意按 term_id 而不是词索引 —— 中间做过一次
    "词 → term_id → 权重"的转换，如果这里还传"词 → df"就得反查一次。
    早先的实现用了一个**模块级反查表**，那在多租户下是错的：
    A 用户查一次、B 用户查一次，反查表被来回覆盖，权重会用到别人的 df。
    """
    tf = Counter(term_ids[t] for t in tokens if t in term_ids)
    if not tf:
        return {}
    dl = len(tokens) or 1
    if is_query:
        return {
            tid: query_weight(f, dfs_by_id.get(tid, 0), n_docs)
            for tid, f in tf.items()
        }
    return {
        tid: doc_weight(f, dfs_by_id.get(tid, 0), n_docs, dl, avgdl)
        for tid, f in tf.items()
    }


def token_df(chunk_tokens: list[list[str]]) -> dict[str, int]:
    """一批 chunk 的 token → 每个词出现在几个 chunk 里。

    @param chunk_tokens `list[list[str]]`，外层是 chunk，内层是 chunk 的 token
    """
    df: Counter = Counter()
    for tokens in chunk_tokens:
        df.update(set(tokens))
    return dict(df)
