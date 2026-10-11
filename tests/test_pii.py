"""🔑 PII 检测与掩码（D121 · M8）。

这一层的测试重点是**掩码之后还剩多少**，而不是"能不能检出"：

| 关注点 | 测试 |
|---|---|
| 掩码后**再检测**必须为空（掩码真的有效） | `test_masked_text_has_no_pii` |
| 掩码**不比原文长**（长度安全，全长度覆盖） | `test_mask_value_never_longer` |
| **重叠命中**不会被数成两处、也不会把文本复制一份 | `test_overlap_not_duplicated` |
| 邮箱不能只留 2 个字符（等于没掩） | `test_email_local_part_mostly_hidden` |
| PII 掩码**全仓库只有一个定义点**（D157：原为「pipeline 与 services 是同一批对象」）| `test_pii_masks_have_exactly_one_definition_site` |
| 报告口径 == 真正掩掉的数 | `test_summarize_matches_mask_text` |

⚠️ 两个**已修**的真实缺陷有专门的回归测试
（`test_overlap_not_duplicated` / `test_short_value_mask_length`）——
它们曾经让 `621111202001011234` 被掩成 36 个字符（原文 18 个）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from services.ingest import pii

ROOT = Path(__file__).resolve().parents[1]
from services.ingest.pii import (
    TYPE_PRIORITY,
    counts_by_type,
    detect_pii,
    mask_text,
    mask_value,
    summarize,
)

SAMPLE = (
    "我的身份证是110101199001011234，手机13812345678，"
    "邮箱user@example.com，学号20230001003。"
)


# ============================================================
# 1. 检出
# ============================================================


def test_detects_each_type():
    by_type = counts_by_type(detect_pii(SAMPLE))
    assert by_type == {"id_card": 1, "phone": 1, "email": 1, "student_id": 1}


def test_hit_has_position():
    """🔑 位置是掩码拼接的依据：没有 start/end 就没法只替换那一段。"""
    (hit,) = [h for h in detect_pii(SAMPLE) if h["type"] == "phone"]
    assert SAMPLE[hit["start"]:hit["end"]] == hit["raw"] == "13812345678"


def test_valid_id_card_matches():
    assert "id_card" in counts_by_type(detect_pii("110101199001011234"))


def test_invalid_date_id_card_does_not_match():
    """⚠️ 12 月 34 日**不是**身份证。

    这条不是学术问题：原模块 `__main__` 的演示样例用的就是
    `441302199812345678`（12 月 34 日），于是"身份证"那一项从来没命中过 ——
    演示输出看着正常，没人发现规则其实是死的。
    """
    assert detect_pii("441302199812345678") == []


def test_empty_text():
    assert detect_pii("") == []
    assert mask_text("") == ("", [])


# ============================================================
# 2. 🔑 掩码后**再检测**必须为空
# ============================================================


def test_masked_text_has_no_pii():
    """🔑 掩码有效性的判据不是"看起来打了码"，而是**再检测一次为空**。

    只看输出里有没有 `*` 会漏掉一类失败：掩码串本身又被规则命中
    （例如把邮箱掩成 `u***@example.com` 而 `*` 在邮箱正则的字符类里，
    于是掩完还是"邮箱"—— 报告说 1 处、实际一直有 1 处）。
    """
    masked, hits = mask_text(SAMPLE)
    assert hits, "样例里必须有命中，否则这个测试是恒真的"
    assert detect_pii(masked) == [], f"掩完还能检出：{detect_pii(masked)}"


def test_masking_is_idempotent():
    once, _ = mask_text(SAMPLE)
    twice, hits2 = mask_text(once)
    assert hits2 == []
    assert twice == once


# ============================================================
# 3. 🔑 长度安全
# ============================================================


@pytest.mark.parametrize(
    "ptype", ["phone", "id_card", "email", "student_id", "bank_card", "unknown"]
)
@pytest.mark.parametrize(
    "raw",
    ["1", "12", "123", "1234", "12345", "123456", "1234567", "12345678",
     "123456789", "123456789012345678", "621111202001011234",
     "11010119900101123X", "user@example.com", "a@b.co"],
)
def test_mask_value_never_longer(ptype, raw):
    """🔑 掩码后**更长**等于"掩了个寂寞还改了内容"。

    已修的真实缺陷：原实现对 6 位数字算出 `raw[:3] + raw[-4:]`（3+4=7）；
    第一版修版对**没有 @ 的** email 入参算出 `"1***@"`（5 位，原文 1 位）。
    """
    out = mask_value(ptype, raw)
    assert len(out) <= len(raw), f"{ptype}({raw!r}) → {out!r} 比原文长"


def test_short_value_mask_length():
    """6~7 位这一档专门钉住（原来就在这里翻车）。"""
    assert mask_value("student_id", "202312") == "2****2"
    assert mask_value("student_id", "1234567") == "1*****7"
    assert len(mask_value("student_id", "202312")) == 6


def test_keeps_head_and_tail():
    """留首尾是为了还能认出"是哪张卡/哪个人"，全打码等于失去可用性。"""
    assert mask_value("phone", "13812345678") == "138****5678"
    assert mask_value("id_card", "110101199001011234").startswith("110")
    assert mask_value("id_card", "110101199001011234").endswith("1234")


def test_id_card_with_x_keeps_length():
    """⚠️ 身份证末位可能是 `X`。若按"非纯数字"另走分支会缩成 5 位，
    徒增 `Chunk.char_start/char_end` 的偏移（D121 的已知取舍）。"""
    out = mask_value("id_card", "11010119900101123X")
    assert len(out) == 18
    assert out.endswith("X")


def test_unknown_type_still_masked():
    assert "*" in mask_value("unknown", "abcdefghij")


# ============================================================
# 4. 🔑 重叠命中
# ============================================================


def test_overlap_not_duplicated():
    """🔑 一串 18 位数字能同时满足身份证与银行卡。

    `621111202001011234`：区划 621111 + 年 2020 + 月 01 + 日 01 + 567 + 校验 4
    → 身份证成立；`62` 开头 18 位 → 银行卡也成立。
    原实现按 start 排序后依次拼接，第二段把**同一段文字再写一遍**：
    实测掩码输出 36 个字符（原文 18 个）。
    """
    src = "号 621111202001011234 尾"
    masked, hits = mask_text(src)
    assert len(hits) == 1, f"同跨度只该算一处，实得 {[h['type'] for h in hits]}"
    assert len(masked) == len(src), f"文本长度变了：{len(src)} → {len(masked)}"
    assert "621111202001011234" not in masked


def test_overlap_keeps_more_specific_type():
    hits = [h for h in detect_pii("621111202001011234")]
    assert {h["type"] for h in hits} == {"id_card", "bank_card"}, "两条规则都该命中（去重前）"
    masked, kept = mask_text("621111202001011234")
    assert [h["type"] for h in kept] == ["id_card"]
    assert masked == "621***********1234"


def test_type_priority_order():
    """`TYPE_PRIORITY` 决定同跨度时留谁 —— 身份证比银行卡更具体。"""
    assert TYPE_PRIORITY.index("id_card") < TYPE_PRIORITY.index("bank_card")
    assert "unknown" not in TYPE_PRIORITY, "未知类型没有优先级，_rank() 会给兜底值"


def test_non_overlapping_hits_all_kept():
    src = "13812345678 和 13900001111"
    _, hits = mask_text(src)
    assert len(hits) == 2
    assert [h["raw"] for h in hits] == ["13812345678", "13900001111"]


def test_mask_preserves_surrounding_text():
    masked, _ = mask_text("前缀 13812345678 后缀")
    assert masked.startswith("前缀 ")
    assert masked.endswith(" 后缀")


# ============================================================
# 5. 邮箱
# ============================================================


def test_email_local_part_mostly_hidden():
    """🔑 只留 1 个字符：留 2 个时 `us***@example.com` 基本等于没掩
    （英文名常常就是前两个字母 + 后面全露）。"""
    out = mask_value("email", "user@example.com")
    assert out == "u***@example.com"
    assert "user" not in out


def test_email_keeps_domain():
    """域名留着：多数场景下"哪个公司的邮箱"不是隐私，
    全打掉会让"工作邮箱 vs 个人邮箱"这类判断彻底失效。"""
    assert mask_value("email", "user@example.com").endswith("@example.com")


# ============================================================
# 6. 报告口径
# ============================================================


def test_summarize_matches_mask_text():
    """🔑 报告的数字必须与真正掩掉的数一致。

    不一致的后果：报告说"掩了 2 处"而实际只掩了 1 处 →
    用户以为掩码有 bug，而真相是计数口径不同。
    """
    by_type, total = summarize(SAMPLE)
    _, hits = mask_text(SAMPLE)
    assert total == len(hits)
    assert by_type == counts_by_type(hits)
    assert sum(by_type.values()) == total


def test_counts_by_type_omits_zero_types():
    """⚠️ 只列真的有命中的类型：报告里 `"phone": 0` 会让用户
    以为"手机号查过了、没问题"，而真相是"这条规则没命中过"。"""
    assert counts_by_type([]) == {}
    assert counts_by_type(detect_pii("今天天气不错")) == {}


def test_summarize_of_clean_text():
    assert summarize("今天天气不错，去公园散步。") == ({}, 0)


# ============================================================
# 7. §0.1：`pipeline/` 必须是 re-export（同一批对象）
# ============================================================


def test_pii_masks_have_exactly_one_definition_site():
    """🔑 整个仓库里 PII 掩码**只有一个定义点**。

    变成"两份实现"的后果不是难看，而是**掩码漂移**：
    离线库按旧规则掩、运行时按新规则掩，
    两边漏掩的种类还不一样，而谁也没法发现。

    🔑 D157 之前这条断言是"`pipeline/privacy/pii_detector.py` 必须 re-export
    同一个对象" —— 那是**两个具体文件**之间的比较。删掉 v1 遗留目录后改成
    扫全仓库的 AST，能抓住"有人在别处又写了一份"。
    ⚠️ 扫 `services/` 时**排除本文件**（测试里写替身是合理的）。
    """
    import ast

    targets = ("detect_pii", "mask_text", "mask_value", "PATTERNS")
    hits: dict[str, list[str]] = {k: [] for k in targets}
    for f in (ROOT / "services").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rel = str(f.relative_to(ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in hits:
                hits[node.name].append(rel)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in hits:
                        hits[tgt.id].append(rel)
    bad = {k: v for k, v in hits.items() if len(v) > 1 or (v and v[0] != "services/ingest/pii.py")}
    assert not bad, (
        f"PII 掩码的实现不止一处：{bad}\n"
        "⚠️ 掩码漂移的后果是**漏掩**（高敏内容进索引），而漏掩不报错"
    )


def test_module_exports():
    for name in pii.__all__:
        assert hasattr(pii, name), name


# ============================================================
# 8. 摄取侧的接入（纯函数部分）
# ============================================================


def test_chunk_helpers_are_importable_and_total():
    """`_detect_pii` / `_mask_chunks` 走的是同一套 `pii` 纯逻辑，
    不该另写一份正则。"""
    from services.ingest.pipeline import _detect_pii, _mask_chunks
    from services.ingest.chunker import Chunk

    chunks = [Chunk(text=SAMPLE)]
    counts, total = _detect_pii(chunks)
    assert total == 4
    assert counts["phone"] == 1
    assert _mask_chunks(chunks) == 1
    assert "13812345678" not in chunks[0].text
    assert _mask_chunks(chunks) == 0, "掩过的 chunk 再掩一次不该算改动"


def test_detect_counts_are_per_type_sum():
    from services.ingest.pipeline import _detect_pii
    from services.ingest.chunker import Chunk

    counts, total = _detect_pii([Chunk(text=SAMPLE), Chunk(text="13800002222")])
    assert sum(counts.values()) == total == 5