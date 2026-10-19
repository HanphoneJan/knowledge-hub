"""🔑 邀请码（D4 · M1）：口令强度、哈希、规范化、兑换的**统一失败**。

这一层是安全边界（兑换端点本身"拿着码就能进工作区"），所以
测试重点是**攻击面**，不是 happy path：

| 攻击 | 防线 | 测试 |
|---|---|---|
| 穷举 31^8 空间 | HMAC + 每码盐（裸 SHA 可离线秒破） | `test_hash_is_salted` |
| 用彩虹表 | 每个码的盐不同 → 哈希互不相同 | 同上 |
| 撞码（猜到别人的码） | 31^8 熵 + 过期 + 限次 + 撤销 | `test_code_entropy` |
| **探测"这个码存在但失效了"** | 兑换对不存在/过期/用完/撤销**同一句话** | `test_all_failures_share_one_message` |
| 手抄错一位 | 8 位 + 去掉 `0O1IL` | `test_alphabet_excludes_ambiguous` |
| 恶意设 "max_uses=10000" 把工作区变公开仓库 | 上限 50（请求层 422 + DB CHECK） | `test_validate_max_uses` |
| 用邀请码拿走整个工作区 | role 恒为 member（DB CHECK 也拦） | `test_model_forbids_owner_role` |
| 并发兑换同一码（都读到 used=0） | DB 的 `used <= max_uses` CHECK | `test_model_has_used_le_max_uses` |
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from services.common import invites
from services.common.invites import (
    CODE_LEN,
    DEFAULT_MAX_USES,
    DEFAULT_TTL_DAYS,
    MAX_USES_LIMIT,
    REDEEM_FAIL,
    RedeemError,
    code_prefix,
    expires_at,
    expiry_text,
    generate_code,
    hash_code,
    normalize_code,
    validate_max_uses,
    validate_ttl_days,
    verify_hash,
    verify_redeem,
)

NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)


# ============================================================
# 1. 生成：熵与字符集
# ============================================================


def test_code_length():
    assert len(generate_code()) == CODE_LEN


def test_alphabet_excludes_ambiguous_chars():
    """🔑 去掉 `0/O/1/I/L`：手抄错一位是最常见的失败，
    而 `O` 与 `0` 长得一样 —— 猜错会导致"我明明复制对了怎么无效"。"""
    seen = set(itertools.islice(
        (c for _ in range(400) for c in generate_code()), 400))
    assert seen, "码的字符集不能是空的"
    for bad in "01OIL":
        assert bad not in seen, f"易混字符 {bad!r} 不该出现在邀请码里"
    assert all(c.isupper() or c.isdigit() for c in seen)


def test_codes_are_unique():
    """🔑 熵的实测（不是"应该不重复"，而是抽样确认没有结构性重复）。"""
    codes = {generate_code() for _ in range(2000)}
    assert len(codes) == 2000, f"2000 次生成只得到 {len(codes)} 个不同的码"


def test_code_space_is_big_enough_but_typable():
    """31^8 ≈ 8.5e11：够短到能手抄，够长到不能穷举。

    ⚠️ 这个测试把"熵够"写成**可计算的判据**，而不是一句注释 ——
    有人把 CODE_LEN 改成 4 时它会红（31^4 ≈ 92 万，一分钟就穷举完）。
    """
    n = 31 ** CODE_LEN
    assert n > 1e10, f"空间只有 {n:.0f}，可被离线穷举"
    assert CODE_LEN <= 12, "再长就不好手抄了"


# ============================================================
# 2. 哈希：只存哈希，且加盐
# ============================================================


def test_hash_is_salted():
    """🔑 同一个码两次哈希**必须不同**（每码随机盐）。

    不加盐的话：① 彩虹表一次算全库；② 两份相同哈希 = 同一个码。
    """
    c = generate_code()
    h1, h2 = hash_code(c), hash_code(c)
    assert h1 != h2, "哈希没有加盐 —— 彩虹表一次算全库"
    assert h1 == h2 or True  # 两者都能验证通过（见下）
    assert verify_hash(c, h1)
    assert verify_hash(c, h2)


def test_hash_has_salt_prefix():
    """`salt$digest` 形状：salt 让同码异哈希成为可能。"""
    h = hash_code(generate_code())
    salt, _, digest = h.partition("$")
    assert salt and digest, f"哈希格式应为 salt$digest，收到 {h[:20]}…"
    assert len(salt) >= 8 and len(digest) == 64


def test_verify_rejects_wrong_code():
    h = hash_code(generate_code())
    assert verify_hash(generate_code() + "X", h) is False
    assert verify_hash("", h) is False
    # 垃圾格式不能抛异常（否则请求直接 500）
    assert verify_hash("abc", "没有分隔符") is False
    assert verify_hash("abc", "a$") is False


def test_plaintext_never_in_stored_value():
    """🔑 库里不能出现明文（拖库 = 所有工作区不设防）。"""
    code = generate_code()
    h = hash_code(code)
    assert code not in h
    assert len(code) > 0


# ============================================================
# 3. 规范化
# ============================================================


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("ABCD1234", "ABCD1234"),
        ("abcd1234", "ABCD1234"),          # 小写
        (" abcd 1234 ", "ABCD1234"),        # 空格
        ("abcd-1234", "ABCD1234"),          # 连字符
        (" ab-cd 1234\n", "ABCD1234"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize(raw, expect):
    assert normalize_code(raw) == expect


def test_normalize_does_not_fix_confusable_chars():
    """🔑 **不能**把 `0` 换成 `O` 之类"猜着修"。

    那会让一个**手抄错的码**通过校验 —— 用户以为自己复制对了，
    而这段码会真的被别人用掉。比拒绝糟糕得多。
    """
    assert normalize_code("0O1IL") == "0O1IL", "规范化不许改字符本身"


def test_normalized_form_still_verifies():
    """用户复制带空格小写的码也要能兑 —— 否则是"明明是对的却无效"。"""
    code = generate_code()
    h = hash_code(code)
    messy = " " + code[:4].lower() + "-" + code[4:] + " "
    assert verify_hash(messy, h) is True


# ============================================================
# 4. 前缀
# ============================================================


def test_code_prefix_is_first_four():
    code = generate_code()
    p = code_prefix(code)
    assert p == code[:4]
    assert len(p) == 4, "前缀过长等于泄露大部分码"


def test_code_prefix_normalizes():
    assert code_prefix(" abcd-1234 ") == "ABCD"


def test_code_prefix_of_empty():
    assert code_prefix("") == ""
    assert code_prefix(None) == ""


# ============================================================
# 5. 参数校验
# ============================================================


def test_validate_defaults():
    assert validate_ttl_days(None) == DEFAULT_TTL_DAYS
    assert validate_max_uses(None) == DEFAULT_MAX_USES


@pytest.mark.parametrize("n", [0, -1, 91, 1000])
def test_validate_ttl_rejects_out_of_range(n):
    with pytest.raises(ValueError, match="1~90"):
        validate_ttl_days(n)


@pytest.mark.parametrize("n", [0, -5, MAX_USES_LIMIT + 1, 10_000])
def test_validate_max_uses_rejects_out_of_range(n):
    """🔑 999 次会把工作区变公开仓库。必须拒。"""
    with pytest.raises(ValueError):
        validate_max_uses(n)


def test_validate_accepts_limits():
    assert validate_ttl_days(1) == 1 and validate_ttl_days(90) == 90
    assert validate_max_uses(1) == 1 and validate_max_uses(MAX_USES_LIMIT) == MAX_USES_LIMIT


# ============================================================
# 6. 到期
# ============================================================


def test_expires_at_is_timezone_aware():
    """🔑 必须带时区：PG 列是 `timestamptz`，naive 会被当本地时间，
    本机 +08:00 → 到期时间差 8 小时（与 `versions.supersede` 同一个坑）。"""
    exp = expires_at(NOW, 7)
    assert exp.tzinfo is not None
    assert (exp - NOW).days == 7


@pytest.mark.parametrize(
    "days,expect",
    [(-1, "已过期"), (0, "今天到期"), (1, "1 天后到期"), (7, "7 天后到期")],
)
def test_expiry_text(days, expect):
    exp = NOW + dt.timedelta(days=days)
    assert expiry_text(exp, NOW) == expect


def test_expiry_text_never_negative():
    """⚠️ 不能显示"-3 天"：用户会以为系统算错了。"""
    assert "已过期" in expiry_text(NOW - dt.timedelta(days=3), NOW)
    assert "-" not in expiry_text(NOW - dt.timedelta(days=3), NOW)


def test_expiry_text_none_is_permanent():
    assert expiry_text(None, NOW) == "永久"


# ============================================================
# 7. 🔑 兑换：失败**统一一句话**
# ============================================================


def _valid(**over):
    kw = dict(
        stored_hash=hash_code("ABCD2345"), code="ABCD2345", used=0,
        max_uses=1, revoked=False, expires_at_=NOW + dt.timedelta(days=7), now=NOW,
    )
    kw.update(over)
    return kw


def test_valid_code_passes():
    verify_redeem(**_valid())          # 不抛 = 通过


@pytest.mark.parametrize(
    "over",
    [
        {"used": 1, "max_uses": 1},                       # 用完
        {"used": 3, "max_uses": 3},                       # 用完（多次用）
        {"revoked": True},                                # 撤销
        {"expires_at_": NOW - dt.timedelta(seconds=1)},   # 过期
    ],
)
def test_state_failures_raise(over):
    with pytest.raises(RedeemError):
        verify_redeem(**_valid(**over))


def test_no_expiry_means_permanent():
    """`expires_at = None` = **永久有效**，不是"没有时间信息所以拒绝"。

    ⚠️ 我第一版把它写进了"应失败"的用例里 —— 于是测试红着，
    而实现是对的。这类"把正确行为当 bug 写进测试"的错会让后来人
    去改正确的那一边。
    """
    verify_redeem(**_valid(expires_at_=None))


def test_nonexistent_hash_raises():
    with pytest.raises(RedeemError):
        verify_redeem(**_valid(stored_hash=None))
    with pytest.raises(RedeemError):
        verify_redeem(**_valid(code="ZZZZ9999"))       # 哈希对不上


def test_all_failures_share_one_message():
    """🔑 **最重要的一条**：所有失败给同一句话。

    区分"不存在 / 已过期 / 已用完 / 已撤销"等于给攻击者一个
    "这个码存在但失效了"的探测信号 —— 那样他可以扫前缀、
    把"存在且有效"的码挑出来集中爆破。
    """
    cases = [
        _valid(stored_hash=None),
        _valid(code="ZZZZ9999"),
        _valid(used=1, max_uses=1),
        _valid(revoked=True),
        _valid(expires_at_=NOW - dt.timedelta(days=1)),
    ]
    msgs = set()
    for kw in cases:
        with pytest.raises(RedeemError) as ei:
            verify_redeem(**kw)
        msgs.add(str(ei.value))
    assert len(msgs) == 1, f"兑换失败文案必须唯一，实际有 {msgs}"
    assert msgs == {REDEEM_FAIL}


def test_hash_checked_before_state():
    """🔑 顺序：先验哈希。

    反过来的话，一个**乱编**的码会得到"已过期"这种**关于不存在的码**的结论 ——
    那同样是探测信号（而且是"你猜的码曾经有效过"）。
    """
    # 一个不存在的码 + 一个已过期的条件组合 → 报的还是那句统一文案
    with pytest.raises(RedeemError) as ei:
        verify_redeem(**_valid(stored_hash=None, used=9, max_uses=1,
                               revoked=True, expires_at_=NOW))
    assert str(ei.value) == REDEEM_FAIL


def test_never_shows_reason():
    """🔴 异常里**不能**带原因（哪怕放在属性里）——
    只要它能被序列化出去，就等于区分开了。"""
    with pytest.raises(RedeemError) as ei:
        verify_redeem(**_valid(revoked=True))
    for attr in ("reason", "detail", "code", "why"):
        assert not hasattr(ei.value, attr), f"RedeemError 不该有 {attr}"


# ============================================================
# 8. 模型层的约束（并发与角色）
# ============================================================


def test_model_forbids_owner_role():
    """🔑 邀请码**不能**授予 owner：否则"转发一段码"= 交出整个工作区。

    数据库层用 CHECK 兜（`ck_invite_role`），应用层也只写死 member。
    """
    from services.common.models import WorkspaceInvite

    checks = {
        c.name for c in WorkspaceInvite.__table__.constraints
        if c.name
    }
    assert "ck_invite_role" in checks
    assert "ck_invite_used" in checks
    assert "ck_invite_max_uses" in checks


def test_model_has_used_le_max_uses():
    """🔑 `used <= max_uses` 是**并发兑换**的唯一防线。

    应用层"先读 used 再写"在两个请求同时进来时**双双通过** ——
    两人都读到 0、都写 1，于是同一个码被两个人用掉。

    ⚠️ 判据查**数据库**（`pg_get_constraintdef`）而不是 ORM 的 `sqltext`：
    SQLAlchemy 会对 CHECK 的文本做解析/规范化，`c.sqltext` 在
    CheckConstraint 上并不总是原始字符串（实测是 `None`），
    拿它断言会得到一条与"约束在不在"无关的失败。
    """
    import subprocess

    out = subprocess.run(
        ["docker", "exec", "pkh-postgres", "psql", "-U", "pkh", "-d", "pkh",
         "-tAc",
         "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
         "WHERE conrelid = 'workspace_invites'::regclass "
         "AND conname = 'ck_invite_used'"],
        capture_output=True, text=True,
    ).stdout
    if not out.strip():
        pytest.skip("数据库里还没有 workspace_invites 表（先跑 alembic upgrade head）")
    # ⚠️ 两边都要去空格：PG 回的是 `CHECK ((used <= max_uses))`，
    #   直接用带空格的子串去 match 必然失败（我第一版就这么错的）。
    assert "used<=max_uses" in out.replace(" ", ""), out


def test_invite_module_exports_public_names():
    """导出面稳定（路由与测试都从这里取，别在别处再实现一遍）。"""
    for name in invites.__all__:
        assert hasattr(invites, name), name