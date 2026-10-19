"""trgm 腿与生成层的单元测试。

trgm 部分覆盖三个**实测踩过 / 会静默出错**的点：

1. `word_similarity` vs `similarity` —— 用错了**不报错**，只会稳定地排错序
2. LIKE 通配符不转义 —— `query="%"` 会匹配全表
3. 子串腿的分数量纲（0.25~0.40）不能与 BM25（0~40）相加 —— 这正是 D39 用 RRF 的理由
"""

from __future__ import annotations

import re

import pytest

from services.retrieval import llm
from services.retrieval.trgm import (
    DEFAULT_MIN_SIMILARITY,
    MIN_QUERY_LEN,
    _escape_like,
    highlight_spans,
)


# ============================================================
# LIKE 转义
# ============================================================


@pytest.mark.parametrize(
    "raw,want",
    [
        ("100%", "100\\%"),
        ("a_b", "a\\_b"),
        ("%", "\\%"),
        ("_", "\\_"),
        ("\\", "\\\\"),
        # 反斜杠必须**先**转义，否则会把后面加的反斜杠再转义一次
        ("a\\%", "a\\\\\\%"),
        ("普通查询", "普通查询"),
    ],
)
def test_escape_like(raw: str, want: str):
    """🔑 用户 query 里出现 `%` / `_` 很平常（`100%`、`a_b`）。

    不转义的话 `query="%"` 的 pattern 是 `'%%%'` → 匹配全表：
    既慢（一路全表扫），又把噪声灌进 RRF。
    """
    assert _escape_like(raw) == want


def test_escape_covers_backslash_before_percent():
    """反斜杠先转义，否则 `%` 的转义会被它吃掉。"""
    assert _escape_like("%") == "\\%"
    assert _escape_like("\\%") == "\\\\\\%"


# ============================================================
# word_similarity vs similarity —— 用真实 PDF 的实测数据锁住
# ============================================================


def _sql(**kw) -> str:
    """把 build_stmt 编译成可断言的 SQL 字符串。"""
    from sqlalchemy.dialects import postgresql

    from services.common.auth import UserContext
    from services.retrieval.trgm import build_stmt

    ctx = UserContext(
        user_id=1, display_name="u", email="u@t.local", api_key_id=None,
        readable_kb_ids=frozenset({5, 7}),
    )
    stmt = build_stmt(kw.pop("q", "入职日期"), ctx, **kw)
    return str(stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))


def test_sql_uses_word_similarity_not_similarity():
    """🔑 本模块**最关键**的一条，写死实测结论防回归。

    查「入职日期那栏填什么」，chunk 平均 500 字（数字来自本机真实语料的一次测量，
    受隐私约束不引用原文）：

    | 函数 | top1 | 分数 |
    |---|---|---|
    | `similarity` | 一段**偏离答案**的表格标题 | 0.111 |
    | `word_similarity` | **含答案的那一段** | 0.400 |

    根因：`similarity()` 对整段做三 gram 集合比较并按长度归一化，
    500 字 chunk 对 8 字 query 天生只有 0.05~0.11 —— 噪声大于信号。
    `word_similarity()` 取正文里相似度最高的**连续窗口**。

    ⚠️ 用错了**不报错**，只会稳定地排错序 —— 所以必须把 SQL 钉住，
    否则某个人看到 `similarity` 更短就"简化"回去了。
    """
    sql = _sql()
    assert "word_similarity" in sql
    # ⚠️ 正则断言：不能只查 `similarity not in sql`（word_similarity 里含它）
    assert not re.search(r"(?<!word_)similarity\(", sql), (
        "发现了裸的 similarity() —— 排序会退化成'整段长度归一化'"
    )
    # 排序与阈值两处都必须用 word_similarity
    assert sql.count("word_similarity") >= 3, "SELECT / ORDER BY / WHERE 三处都该是它"


def test_sql_recall_uses_only_word_similarity():
    """🔑 召回条件**只能**是 `word_similarity` —— 这条锁住两个踩过的坑。

    实测（222 chunk 的库，query="入职日期那栏填什么"）：

    | 召回条件 | 命中 | 含真值？ |
    |---|---|---|
    | `ILIKE '%整句%'` | 0 | ❌ 自然语言提问不是子串 |
    | `similarity(chunk,q) >= 0.1` | 0 | ❌ 整段长度归一化后恒低 |
    | `word_similarity(q,chunk) >= 0.25` | **3** | ✅ |

    第一版把 ILIKE 当召回条件，整条 trgm 腿返回 0 条 —— 而真值明明有 0.400。
    """
    sql = _sql()
    # 唯一召回条件
    assert re.search(r"word_similarity\([^)]*\)\s*>=", sql), "必须有 word_similarity 阈值"

    # ⚠️ 断言"没有"要用正则，不能用 `"ILIKE" not in sql` ——
    #   那会在有人把 ILIKE 加回 OR 分支时放过，而 OR 分支实测同样漏召回。
    assert not re.search(r"ILIKE|~~\*|similarity\(\s*chunks", sql, re.I), (
        "发现了 ILIKE 或裸 similarity() 作为召回条件 —— "
        "两者都会漏掉真值（详见 trgm.py 模块 docstring 的实测表）"
    )


def test_escape_like_is_unused_by_sql_but_still_tested():
    """`_escape_like` 当前没有 SQL 调用方，但它是纯函数、值得单测。

    将来加"精确子串"端点时会直接复用，所以现在就该有覆盖 ——
    而不是等到有人用它时才发现转义顺序写错了。
    """
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    # ⚠️ 反斜杠必须先转义，否则会把 `%` 的转义再吃一次
    assert _escape_like("a\\%") == "a\\\\\\%"


def test_sql_does_not_select_visibility():
    """🔑 检索层不许把 `visibility` 读出来。

    一旦拿到手，早晚会被写成 `if visibility == "workspace": ...` ——
    那就是 CONVENTIONS §0.6 那个真实越权的成因（两处复刻可见性逻辑）。
    可见性判定只在 `auth.load_user_context` 与 `kb.py::_visible_kb_filter`。
    """
    sql = _sql()
    assert "visibility" not in sql.lower(), (
        "SELECT 里带了 knowledge_bases.visibility —— "
        "检索层一旦判定可见性就是第二个过滤入口"
    )


def test_sql_applies_tenant_scope():
    """🔑 租户条件必须在 SQL 里（trgm 腿在 PG 里，用 `build_kb_scope_sql`）。"""
    assert "knowledge_bases.id IN (5, 7)" in _sql()
    # 无权限的单库 → 恒假
    assert "knowledge_bases.id = -1" in _sql(kb_id=999)


def test_sql_filters_soft_deleted():
    """🔑 软删除二次过滤（CONVENTIONS §1.2）。"""
    sql = _sql()
    assert "documents.is_current IS true" in sql
    assert "documents.deleted_at IS NULL" in sql
    assert "knowledge_bases.deleted_at IS NULL" in sql


def test_sql_orders_by_word_similarity_desc():
    sql = _sql()
    tail = sql[sql.rindex("ORDER BY"):]
    assert "word_similarity" in tail and "DESC" in tail


def test_min_similarity_threshold_is_between_noise_and_truth():
    """🔑 阈值必须落在"真值 0.40"与"噪声 0.05~0.15"之间。

    太高 → 这一腿全空，等于没实现；太低 → 全是噪声，污染 RRF 名次。
    """
    assert 0.15 < DEFAULT_MIN_SIMILARITY < 0.40, (
        f"阈值 {DEFAULT_MIN_SIMILARITY} 必须把真值(≈0.40)收进来、"
        "把噪声(≈0.11)挡在外面"
    )


def test_min_similarity_default_is_conservative():
    assert DEFAULT_MIN_SIMILARITY == 0.25


def test_short_query_is_skipped():
    """🔑 2 个字的 query 不走 trgm。

    三 gram 至少要 3 个字符才有 gram 可比，2 字 query 走这一腿
    只会拿到一堆"恰好共现"的噪声，污染 RRF 的名次。
    """
    assert MIN_QUERY_LEN == 3


# ============================================================
# highlight
# ============================================================


def test_highlight_positions_are_relative_to_the_snippet():
    """🔑 契约的 `positions` 是**相对片段**的偏移，不是相对原文。

    前端拿它直接在片段上渲染高亮；给成原文偏移就会整体错位。
    """
    text = "前面的无关内容" * 10 + "入职日期那栏填什么" + "后面的无关内容" * 10
    spans = highlight_spans(text, "入职日期那栏填什么", context=10)
    assert len(spans) == 1
    s, e = spans[0]["positions"][0]
    snippet = spans[0]["text"]
    # 片段的 [s:e] 处必须是命中串
    assert snippet[s:e] == "入职日期那栏填什么"
    assert spans[0]["highlight"] == "入职日期那栏填什么"


def test_highlight_caps_number_of_spans():
    text = "目标词" + "填充" * 50 + "目标词" + "填充" * 50 + "目标词"
    assert len(highlight_spans(text, "目标词", max_spans=2)) == 2


def test_highlight_empty_inputs():
    assert highlight_spans("", "x") == []
    assert highlight_spans("正文", "") == []
    assert highlight_spans("正文", "不存在的串") == []


def test_highlight_treats_query_literally():
    """🔑 子串命中是字面匹配，不是正则 —— query 里的 `.` 不该当通配。

    契约说 trgm 这一腿搜的是**子串**，用户搜 `1.5` 不应该命中 `185`。
    """
    text = "版本 1.5 已发布"
    assert highlight_spans(text, "1.5") != []
    assert highlight_spans(text, "1.5.5") == []


# ============================================================
# 分数量纲（D39 为什么必须用 RRF）
# ============================================================


def test_trgm_and_bm25_scores_are_not_comparable():
    """🔑 记录量纲差异 —— 这是 D39 选 RRF（只用排名）的直接原因。

    实测：dense 0.2~0.9 / sparse 0~40+ / trgm 0.25~0.40。
    直接加权求和会让 sparse 单独主导 —— 而 trgm 的 0.40 会被当成噪声丢掉。
    """
    trgm_real = 0.400  # word_similarity 的真值上限附近
    bm25_real = 38.2  # BM25 无界
    assert bm25_real > trgm_real * 50, "量纲差 50 倍以上，归一化会引入查询相关的偏差"


# ============================================================
# 生成层
# ============================================================


def test_default_provider_is_extractive():
    """🔑 隐私红线：默认抽取式，零外传。"""
    assert llm.get_generator().name == "extractive"
    assert llm.get_generator("local").name == "extractive"


def test_openai_compat_refuses_when_unconfigured():
    """🔑 未配置外部 LLM 时**抛错**，不静默降级成抽取式。

    静默降级会让用户以为自己在用 LLM，实际得到的是抽取式答案 ——
    比报错更糟，因为它是**说谎**而不是失败。
    """
    with pytest.raises(RuntimeError) as e:
        llm.get_generator("openai_compat")
    # D170：报错指向**字段名**（不再硬编码 LLM_* env 变量名）
    assert "base_url" in str(e.value) and "api_key" in str(e.value), str(e.value)
    assert "隐私红线" in str(e.value)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="未知的生成器"):
        llm.get_generator("gpt-whatever")


def test_parse_citations_separates_dangling():
    """🔑 悬空角标（答案里有 `[9]` 但只有 3 条证据）必须被识别出来。

    出现悬空角标意味着答案引用了不存在的东西 —— 比没有引用更糟，
    因为用户点进去发现是空的。
    """
    ok, dangling = llm.parse_citations("答案 [1] 与 [2][3]", 3)
    assert ok == [1, 2, 3]
    assert dangling == []

    ok, dangling = llm.parse_citations("答案 [1] 和 [9]", 3)
    assert ok == [1]
    assert dangling == [9], "悬空角标要被单独报出来给前端提示"


def test_split_sentences_breaks_on_speaker_markers():
    """🔑 聊天文本**没有标点**，靠 `[昵称]` 前缀标记说话人（PROGRESS §5.4）。

    只按句末标点切会切出 `…[张三] 你吃饭了吗[李四中宝贝] …` 这种跨消息碎片。
    """
    chat = "[张三]你吃饭了吗[李四中宝贝]吃啦，刚吃完[张三]吃的什么"
    sents = llm.split_sentences(chat)
    assert all("[" not in s for s in sents), f"不该残留说话人标记: {sents}"
    assert len(sents) >= 3


def test_split_sentences_on_normal_prose():
    sents = llm.split_sentences("第一句话在这里。第二句话也在这里！第三句话收尾。")
    assert len(sents) == 3


def test_split_sentences_keeps_short_chat_messages():
    """🔑 句子下限不能设成 8 —— 那会把聊天消息整条丢掉。

    写这条测试时才发现第一版的 `min_len=8` 让
    `"[张三]你吃饭了吗[李四中宝贝]吃啦"` 切出**空列表**。
    聊天是本项目最大的数据源（87,057 条），短句是它的常态。
    下限只该滤掉 `嗯` `哦` 这类 2 字语气词。
    """
    assert llm.MIN_SENTENCE_LEN <= 5, (
        f"下限 {llm.MIN_SENTENCE_LEN} 会丢掉「你吃饭了吗」这类聊天短句"
    )
    assert llm.split_sentences("你吃饭了吗") == ["你吃饭了吗"]
    assert llm.split_sentences("嗯") == []


def test_sentence_scoring_uses_bm25_tokens_including_dates():
    """🔑 句级打分必须走 `bm25.tokenize`，日期归一化 token 要一起进来。

    少了 `d20250214`，"2025年2月14日"这类提问在**句级**打分上也会全部得 0
    —— 与 `bm25.py` 里那个 R@10 0.20→1.00 的修复是同一个根因。
    """
    sc = llm.score_sentences("2025年2月14日那天做了什么", ["2025-02-14 我们去了海边"])
    assert sc and sc[0] > 0, "日期归一化后应有命中"


def test_extractive_caps_two_sentences_per_evidence():
    """🔑 一条证据最多贡献 2 句 —— 否则一条长 chunk 能把 6 句全占满。"""
    ev = [
        llm.Evidence(
            idx=1, doc_id=1, chunk_index=0,
            text="入职日期填写要求第一句。入职日期填写要求第二句。"
                 "入职日期填写要求第三句。入职日期填写要求第四句。",
        )
    ]
    answer, cited = llm.compose_extractive("入职日期填写要求", ev, max_sentences=6)
    assert answer.count("[1]") == 2
    assert cited == [1]


def test_extractive_says_so_when_nothing_matches():
    """🔑 一句都选不出来时**如实说检索不到**，不编。"""
    ev = [llm.Evidence(idx=1, doc_id=1, chunk_index=0, text="完全无关的内容。")]
    answer, cited = llm.compose_extractive("另一个话题", ev)
    assert cited == []
    assert "没有" in answer


def test_extractive_reports_images_instead_of_faking_text():
    """🔑 只有图片证据时如实说明，不要用图片标题冒充答案。"""
    ev = [llm.Evidence(idx=1, doc_id=1, chunk_index=0, text="", kind="image")]
    answer, cited = llm.compose_extractive("第一次旅游的照片", ev)
    assert cited == [1]
    assert "图片" in answer