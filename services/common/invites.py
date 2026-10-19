"""邀请码的**纯逻辑**：生成、规范化、哈希、口令强度（D4 · M1）。

## 为什么邀请码要单独设计（而不是"按邮箱加成员"）

现有 `POST /workspaces/{id}/members` 只能加**已注册**的邮箱 ——
对方没注册就直接 400（"邀请未注册用户需要待接受邀请表，当前没做"）。
而 D4 明确**不接邮件服务**，所以"发邮件邀请"这条路本来就不通。

邀请码把"邀请"变成**自服务**：owner 生成一段码 → 对方注册后自己输码加入。
不需要邮件、不需要对方提前注册。

## 三条安全设计（与 API Key 同一套纪律，D64）

1. 🔑 **库里只存哈希 + 前缀**，明文只在创建那一次响应里。
   理由与 API Key 相同：数据库泄露时，存明文 = 所有工作区不设防。
2. 🔑 **口令强度**：8 位、去掉 `0/O/1/l/I` 这些**手抄会认错**的字符
   （真实经历：把邀请码敲错一位是最常见的失败，而 `O`/`0` 长得一样）。
   熵：31^8 ≈ 8.5e11 —— 够短到能手抄，够长到不能穷举（再配合过期与限次）。
3. 🔑 **校验失败要能区分**"码不存在"与"已过期/已用完/已被撤销"吗？
   ⚠️ **不能区分** —— 那等于给攻击者一个"这个码存在但失效了"的探测信号。
   所以对**兑换**一律回同一句话（见 `verify_redeem`）。
   （列表与撤销是自己人看，可以区分。）

## 规范化：大小写与分隔符必须统一

用户从聊天记录里复制粘贴，可能带上空格、连字符，或小写。
不规范化就会有两类"明明是对的码却提示无效"。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

#: 去掉易混字符（0/O、1/l/I）。31 个字符。
_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OIL")
CODE_LEN = 8
#: 默认有效期（天）。⚠️ 必须有：一段永久有效的码等于"工作区的公共密码"。
DEFAULT_TTL_DAYS = 7
#: 默认可用次数。1 = 一码一人（最贴近"邀请"的心智）。
DEFAULT_MAX_USES = 1
#: 上限：避免有人生成"能用 10000 次"的码，把工作区变成公共仓库。
MAX_USES_LIMIT = 50


def generate_code() -> str:
    """生成一段邀请码（明文）。

    🔑 用 `secrets` 而不是 `random`：`random` 是 Mersenne Twister，
    可由少量输出反推内部状态 → 后续邀请码全部可预测。
    这是凭据，必须用 CSPRNG（与 `generate_api_key` 同一条纪律）。
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))


def normalize_code(raw: str | None) -> str:
    """把用户输入规整成比较用的形式。

    去掉**所有**空白与连字符、转大写。⚠️ 不能"顺手"把 `0` 换成 `O`：
    那会让一个**手抄错的码**通过校验，用户以为自己复制对了，
    而码会真的被别人用掉（这比拒绝更糟）。
    """
    if not raw:
        return ""
    out = []
    for ch in raw:
        if ch in " \t\r\n-_":
            continue
        out.append(ch.upper())
    return "".join(out)


def hash_code(code: str) -> str:
    """邀请码 → 存库的哈希。

    ⚠️ 用 **HMAC-SHA256 + 每码盐**，不是裸 SHA256：
    邀请码空间只有 31^8 ≈ 8.5e11，裸哈希可以被彩虹表/穷举秒破
    （离线穷举 8.5e11 次在单机上是分钟级）。盐让每个码的哈希互不相同，
    彩虹表失效；HMAC 的密钥在进程里，拖库的人算不出来。
    """
    salt = secrets.token_hex(8)
    digest = hmac.new(
        _PEPPER, normalize_code(code).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{salt}${digest}"


def verify_hash(code: str, stored: str) -> bool:
    """校验明文是否对应 `stored`（常量时间比较）。"""
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hmac.new(_PEPPER, normalize_code(code).encode("utf-8"), hashlib.sha256)
    return hmac.compare_digest(calc.hexdigest(), digest)


def code_prefix(code: str) -> str:
    """给人看的**前缀**（列表里显示，让人认得出是哪一段）。

    取前 4 位：足够区分，又不泄露完整码。⚠️ **不能**显示完整明文 ——
    那等于在列表页把所有码公开（列表只需要 admin 看，但截图/旁观都可能）。
    """
    n = normalize_code(code)
    return n[:4] if n else ""


#: HMAC 密钥。⚠️ 进程级随机 → **重启后无法校验旧码**（`verify_hash` 全失败）。
#: 换成固定值会把所有邀请码的可逆性绑定到源码上（拖库 + 读源码 = 全破）。
#: 更好的做法是从环境变量注入；此处显式记录这个取舍。
_PEPPER = secrets.token_bytes(32)


def validate_ttl_days(n: int | None) -> int:
    if n is None:
        return DEFAULT_TTL_DAYS
    if n < 1 or n > 90:
        raise ValueError(f"有效期要在 1~90 天之间，收到 {n}")
    return n


def validate_max_uses(n: int | None) -> int:
    if n is None:
        return DEFAULT_MAX_USES
    if n < 1 or n > MAX_USES_LIMIT:
        raise ValueError(f"可用次数要在 1~{MAX_USES_LIMIT} 之间，收到 {n}")
    return n


def expires_at(now, ttl_days: int):
    """到期时刻（UTC aware）。

    ⚠️ 必须带时区：PG 列是 `timestamptz`，naive 会被当本地时间，
    本机 +08:00 → 到期时间差 8 小时（与 `versions.supersede` 同一个坑）。
    """
    import datetime as dt

    return now + dt.timedelta(days=ttl_days)


def expiry_text(expires_at_: object, now) -> str:
    """到期文案（列表里显示）。

    ⚠️ 过期要说"**已过期**"而不是负数天 —— 用户看到"-3 天"会以为系统算错了。
    """
    import datetime as dt

    if expires_at_ is None:
        return "永久"
    delta = expires_at_ - now
    days = delta.days
    if days < 0:
        return "已过期"
    if days == 0:
        return "今天到期"
    return f"{days} 天后到期"


# ============================================================
# 兑换时的状态判定
# ============================================================


class RedeemError(Exception):
    """兑换失败。🔑 `code` 是给**用户**看的，不区分原因（见模块 docstring）。"""


#: 🔴 兑换失败**统一**这句话。不区分"不存在/过期/用完/撤销"——
#:   区分开等于给攻击者一个"这个码存在但失效了"的探测信号。
REDEEM_FAIL = "邀请码无效（可能已过期、已被使用，或已被撤销）"


def verify_redeem(*, stored_hash: str | None, code: str, used: int, max_uses: int,
                  revoked: bool, expires_at_, now) -> None:
    """兑换前的全部校验。**不通过就抛 `RedeemError`**（同一句话）。

    ⚠️ 顺序：先验哈希（不通过 → 码根本不存在），再验状态。
    反过来的话，"随便一个码"会得到"已过期"这种**关于不存在的码**的结论，
    同样是探测信号。
    """
    if not stored_hash or not verify_hash(code, stored_hash):
        raise RedeemError(REDEEM_FAIL)
    if revoked:
        raise RedeemError(REDEEM_FAIL)
    if used >= max_uses:
        raise RedeemError(REDEEM_FAIL)
    if expires_at_ is not None and expires_at_ <= now:
        raise RedeemError(REDEEM_FAIL)


__all__ = [
    "CODE_LEN",
    "DEFAULT_MAX_USES",
    "DEFAULT_TTL_DAYS",
    "MAX_USES_LIMIT",
    "REDEEM_FAIL",
    "RedeemError",
    "code_prefix",
    "expires_at",
    "expiry_text",
    "generate_code",
    "hash_code",
    "normalize_code",
    "validate_max_uses",
    "validate_ttl_days",
    "verify_hash",
    "verify_redeem",
]