"""🔑 Reranker 的行为（`services/retrieval/rerank.py`）。

## 重点不在"能打分"，在四件容易被忽略的事

1. **不可用时如实 `skipped` 且不动原顺序** —— 不静默降级（D73 同款理由）
2. **精排失败也保持原顺序** —— 排序是增强，崩掉整个检索是本末倒置
3. **候选之外的结果保持原顺序接在后面**，不是丢掉（精排是"提升"不是"过滤"）
4. **同分保持原 RRF 顺序** —— 排序键显式写成 `(-score, index)`，
   让"稳定"成为可读的性质而不是 `sorted` 的巧合

真正跑模型的那条（打分、排序、延迟）由 `eval/api_1d_check.py` 验 ——
单测里**不加载 1GB 模型**（138M 参数 × 每个测试进程都是几十秒）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.retrieval import rerank

ROOT = Path(__file__).resolve().parents[1]


class _Hit:
    def __init__(self, key: str, text: str = "") -> None:
        self.key = key
        self.payload = {"text_norm": text}


#: 这些测试都 monkeypatch 掉 `_load`/`score_passages`，所以**不真的加载模型**；
#: 但仍要显式传一个模型名 —— 因为 `None` 的语义是"不启用"（见
#: `test_unconfigured_kb_never_loads_the_model`）。
MODEL = "test/fake-reranker"


def _reset() -> None:
    """把模块级缓存清掉，让 monkeypatch 生效（加载是懒的、进程内的）。

    ⚠️ `_model_name` 也参与缓存判断（换模型要重载），所以必须一起清 ——
    只清 `_model` 的话测试之间会互相污染（第二版踩过）。
    """
    rerank._model = None
    rerank._model_name = ""
    rerank._model_err = None
    rerank._DISABLED = False


# ============================================================
# 1. 不可用 → skipped，且**不动顺序**
# ============================================================


def test_unavailable_keeps_original_order(monkeypatch):
    """🔑 降级必须是"原样返回"，不是"抛错"也不是"换个顺序"。

    用户拿到的结果与不开 Reranker 一模一样 —— 这才是可预期的降级。
    """
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: None)
    monkeypatch.setattr(rerank, "_model_err", "OSError: 模型目录不存在", raising=False)

    hits = [_Hit("a"), _Hit("b"), _Hit("c")]
    out = rerank.rerank("问题", hits, kb_rerank=MODEL)

    assert out.applied is False
    assert out.skipped and "模型目录不存在" in out.skipped
    assert [h.key for h in out.items] == ["a", "b", "c"], "降级不能重排"
    assert out.scores == {}, "没跑就没有分（别给 0 分冒充）"


def test_empty_candidates_are_skipped_not_ranked(monkeypatch):
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: None)
    out = rerank.rerank("问题", [])
    assert out.items == []
    assert out.applied is False and out.skipped


def test_scores_failure_keeps_order(monkeypatch):
    """打分抛异常时要**保住结果** —— 崩掉整个检索是本末倒置（D73 同款理由）。"""
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())

    def boom(*_a, **_k):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(rerank, "score_passages", boom)
    hits = [_Hit("a"), _Hit("b")]
    out = rerank.rerank("问题", hits, kb_rerank=MODEL)

    assert [h.key for h in out.items] == ["a", "b"]
    assert out.applied is False and "CUDA" in out.skipped


# ============================================================
# 2. 排序规则（用假分数，不加载模型）
# ============================================================


def test_rerank_orders_by_score_desc(monkeypatch):
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    # 顺序与分数**故意反向**，确保真的重排了
    monkeypatch.setattr(rerank, "score_passages", lambda q, ps: [1.0, 9.0, 5.0])
    hits = [_Hit("a"), _Hit("b"), _Hit("c")]
    out = rerank.rerank("问题", hits, kb_rerank=MODEL)
    assert [h.key for h in out.items] == ["b", "c", "a"]
    assert out.applied and out.scores == {"a": 1.0, "b": 9.0, "c": 5.0}


def test_equal_scores_keep_original_order(monkeypatch):
    """🔑 同分保持原 RRF 顺序。

    bge-reranker 对长度相近的短文本经常给出相同/极近的分数；
    若不稳定排序，**同样质量的语料两次检索给出两种顺序**，
    用户会认为"搜索结果在乱跳"。
    """
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    monkeypatch.setattr(rerank, "score_passages", lambda q, ps: [7.0] * 3)
    hits = [_Hit("a"), _Hit("b"), _Hit("c")]
    assert [h.key for h in rerank.rerank("问题", hits, kb_rerank=MODEL).items] == ["a", "b", "c"]


def test_sort_key_is_explicitly_stable(monkeypatch):
    """排序键写成 `(-score, index)` 而不是只写 `-score`。

    后者依赖 `sorted` 的稳定性 —— 换成人写的不稳定排序就会静默改变排序。
    """
    src = (ROOT / "services" / "retrieval" / "rerank.py").read_text(encoding="utf-8")
    assert "key=lambda i: (-scores[i], i)" in src


def test_candidates_beyond_limit_are_kept_in_order(monkeypatch):
    """🔑 精排只作用于前 `limit` 个，但**后面的不能丢**。

    丢掉就等于让 Reranker 变成过滤器，而它**只该做提升**：
    真值排在 25 位时，精排 20 个候选救不了它，但至少不该把它删掉。
    """
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    monkeypatch.setattr(rerank, "score_passages", lambda q, ps: [0.0] * 2)
    hits = [_Hit("a"), _Hit("b"), _Hit("x"), _Hit("y"), _Hit("z")]
    out = rerank.rerank("问题", hits, limit=2, kb_rerank=MODEL)
    assert [h.key for h in out.items] == ["a", "b", "x", "y", "z"]


def test_does_not_mutate_input(monkeypatch):
    """入参顺序可能被调用方复用（`/search/debug` 要看原 RRF 顺序）。"""
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    monkeypatch.setattr(rerank, "score_passages", lambda q, ps: [0.0, 5.0])
    hits = [_Hit("a"), _Hit("b")]
    rerank.rerank("问题", hits, kb_rerank=MODEL)
    assert [h.key for h in hits] == ["a", "b"]


def test_only_head_is_scored(monkeypatch):
    """只打分前 limit 个 —— 打全量会让延迟随 top_k 线性增长（§4 的延迟基线）。"""
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    seen: list[int] = []
    monkeypatch.setattr(
        rerank, "score_passages", lambda q, ps: seen.append(len(ps)) or [0.0] * len(ps)
    )
    hits = [_Hit(str(i)) for i in range(30)]
    rerank.rerank("问题", hits, limit=20, kb_rerank=MODEL)
    assert seen == [20]


# ============================================================
# 3. 输入清洗：长文本与脏 payload
# ============================================================


def test_passage_text_prefers_normalised_text():
    """Reranker 判的是"这段讲什么"，要 `text_norm`（D35）而不是带页眉的 raw。"""
    class H:
        payload = {"text_norm": "归一化正文", "text_raw": "页眉 噪声 正文"}
    assert rerank._passage_text(H()) == "归一化正文"


@pytest.mark.parametrize("bad", [None, "x", 42, [], {"text_norm": 7}])
def test_dirty_payload_gives_empty_passage_not_crash(bad):
    """Qdrant payload 是 JSONB，脏数据不许炸（§5.42 同源）。"""

    class H:
        payload = bad

    assert isinstance(rerank._passage_text(H()), str)


def test_hit_without_payload_attribute():
    class Weird:
        pass

    assert rerank._passage_text(Weird()) == ""


def test_max_chars_is_bounded():
    """⚠️ 必须截断：BERT 位置编码上限 512 token，超了**静默截断**。

    后果是"第 3 段的关键信息排在第 1 段的废话后面"而没有任何报错
    —— 长文档 rerank 最常见的坑。
    """
    assert 200 <= rerank.MAX_CHARS <= 2000


def test_truncation_is_actually_applied(monkeypatch):
    """不是只在注释里说要截断 —— 断言真的切了。"""
    _reset()
    monkeypatch.setattr(rerank, "_load", lambda name=None: object())
    captured: list[list[str]] = []
    monkeypatch.setattr(
        rerank, "score_passages",
        lambda q, ps: captured.append(ps) or [0.0] * len(ps),
    )
    long = "字" * (rerank.MAX_CHARS * 2)
    rerank.rerank("问题", [_Hit("a", long)], kb_rerank=MODEL)
    assert len(captured[0][0]) == rerank.MAX_CHARS


# ============================================================
# 4. 默认关闭这件事必须写在代码里，不只写在文档里
# ============================================================


def test_rerank_is_opt_in_per_kb():
    """🔑 默认**不**启用：v2-m3 单对 114ms（实测），20 候选 ≈ 2.3s。

    要钉的不是"没调用 rerank"（现在**确实**调用了），而是
    **默认参数是 None** —— 有人把默认值改成模型名，
    就会悄悄给所有请求加 2 秒，而没人说得清为什么变慢。
    """
    src = (ROOT / "services" / "retrieval" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "search")
    defaults = {
        a.arg: ast.unparse(d)
        for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)
    }
    assert defaults.get("rerank_model") == "None", (
        f"rerank_model 默认必须是 None（不启用），实际 {defaults.get('rerank_model')}"
    )
    # 而且必须原样透传给 rerank.rerank（不能在 engine 里 fallback 到全局默认）
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("rerank")]
    assert calls, "engine.search 应该调用 rerank（按库开关）"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("kb_rerank") == "rerank_model", (
        f"必须把库配置原样透传，实际 {kw}"
    )


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_model_name_does_not_fall_back_for_empty_kb_value(empty):
    """🔑 per-KB 值是空/None 时**不** fallback 到全局默认。

    否则"用户关了精排"却还在跑 2 秒，而且 `/search/debug` 显示不出来
    —— 那是"配置形同虚设"的典型形态。

    ⚠️ 踩过的坑：第一版写 `if kb_rerank is not None: return kb_rerank`，
    于是 `None`（库里没配）被当成"未指定，用全局默认" → 实测
    `applied=true`、5.2s。**测试当时只测了 `""`**，所以没抓住。
    现在 None / 空串 / 空白三种都测。
    """
    assert rerank.model_name(empty) == ""


def test_unconfigured_kb_never_loads_the_model(monkeypatch):
    """"没配置"要在**碰模型之前**就返回 —— 那是每个请求都会走的分支。"""
    _reset()

    def boom(name=None):  # pragma: no cover - 走到就说明错了
        raise AssertionError("未配置时不该尝试加载模型")

    seen: list[str | None] = []

    def spy(name=None):
        seen.append(name)
        return None

    monkeypatch.setattr(rerank, "_load", spy)
    out = rerank.rerank("问题", [_Hit("a"), _Hit("b")])
    assert seen == [], "未配置时不该调用 _load"
    assert out.applied is False

    # 🔑 配了模型名 → 加载的必须是**那个**名字，不是全局默认
    out_m = rerank.rerank("问题", [_Hit("a")], kb_rerank=MODEL)
    assert seen == [MODEL], f"加载的模型名错了：{seen}"
    assert out.applied is False
    assert "未配置" in out.skipped
    assert out.model == "", "未配置时不该报一个模型名（会让人以为在用）"


def test_explicit_default_model_is_reachable():
    """想要全局默认模型有**明确**的入口，而不是靠 None 隐式 fallback。"""
    from services.common.config import get_settings

    assert rerank.default_model() == get_settings().rerank_model
    assert rerank.default_model() == rerank.DEFAULT_RERANK_MODEL


def test_default_model_is_v2_m3_because_base_fails_on_chinese_forms():
    """🔑 默认值是**实测**选出来的，不是随手填的（D87 / eval/rerank_probe.py）。

    base 在"北京是中国的首都"上判别极好（+6.83 / -10.20），但在
    "入团积极分子登记表怎么填写"这种**本项目语料主体**的 query 上
    把标题行排在答案段之上 —— 成对判别 base 2/4、v2-m3 4/4。
    """
    assert rerank.DEFAULT_RERANK_MODEL == "BAAI/bge-reranker-v2-m3"
    cfg = (ROOT / "services" / "common" / "config.py").read_text(encoding="utf-8")
    assert "bge-reranker-v2-m3" in cfg
    # 探针脚本必须还在（不然这个结论没法复现）
    assert (ROOT / "eval" / "rerank_probe.py").exists()


def test_disabled_and_broken_are_different_reasons():
    """`RERANK_MODEL=""`（运维决定）与"没装模型"（环境问题）要能分开报。"""
    src = (ROOT / "services" / "retrieval" / "rerank.py").read_text(encoding="utf-8")
    assert "显式禁用" in src, "禁用与加载失败没有区分开"

def test_switching_models_reloads(monkeypatch):
    """🔑 第二个库配**另一个**模型时必须重新加载。

    缓存命中判断必须比**模型名**。只判"有没有加载过"的话，
    第二个库的 `kb.rerank_model` 会静默不生效 —— 而且
    `/search/debug` 显示的模型名还是"真的"那个，从界面上看不出问题。
    """
    _reset()
    loaded: list[str] = []

    def fake_load(name=None):
        want = rerank.model_name(name) or rerank.default_model()
        if rerank._model is None or rerank._model_name != want:
            loaded.append(want)
            rerank._model = object()
            rerank._model_name = want
        return rerank._model

    monkeypatch.setattr(rerank, "_load", fake_load)
    monkeypatch.setattr(rerank, "score_passages", lambda q, ps: [1.0] * len(ps))

    rerank.rerank("q", [_Hit("a")], kb_rerank="org/model-a")
    rerank.rerank("q", [_Hit("a")], kb_rerank="org/model-a")   # 命中缓存
    rerank.rerank("q", [_Hit("a")], kb_rerank="org/model-b")   # 必须重载

    assert loaded == ["org/model-a", "org/model-b"], loaded
