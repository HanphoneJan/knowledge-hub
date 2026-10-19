"""🔴 D135：`/auth/register` 限流（它对**全网**可达）。

## 缺口是怎么确认的

2026-10-04 实测（部署形态：本机 + Cloudflare Tunnel 对外，
但**没有** Access）：

```
POST /api/v1/auth/register  {}   → 422（不是 401）
```

422 意味着**没有 API Key 也能走到注册端点的业务校验** ⇒
"任何人都能往库里塞账号"，而且 `auth.py` 里 grep 不到任何 429/限流逻辑。
D113 早就把它列为"必需加固"的第一条。

## 桶的键怎么算（这一段是本模块最难的部分）

限流的第一要素是"按谁算"。本项目的部署形态是 **Cloudflare Tunnel**：
外部请求由本机的 `cloudflared` 转发到 `127.0.0.1:8010`，于是
**uvicorn 看到的对端地址不是真实调用方**（实测经公网域名来的请求，
对端是本机的 IPv6 —— cloudflared 按 dashboard 里的 service 配置拨回去的）。

所以：

- 🔑 真实客户端 IP 在 **`CF-Connecting-IP`** 头里（Cloudflare 会覆盖它，
  外部调用方自己设不了）
- ⚠️ **只在"对端是本机/内网"时才信这个头** —— 如果哪天有人给这台机器
  直连开端口，外部就能自己伪造 `CF-Connecting-IP` 来绕过限流
- 🔑 **对端是 loopback 且没有任何转发头 ⇒ 只可能来自本机**
  （验收脚本、本机 curl）⇒ 走**单独的宽松桶**，否则基线每轮注册 10 个账号
  会把自己限死
- 剩下的情况（对端是可路由地址）直接用它 —— 那本来就是真实来源

## 为什么用内存计数器而不用表

注册是低频操作；进程重启丢失计数**无害**（最坏是多注册几个账号），
而加一张表 = 一个迁移 + 一个清理任务 + 一个新的"这张表该不该清"的判断，
代价远大于收益。真需要跨进程精确配额时再加。
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict

#: 远端来源（真实客户端 IP）的注册配额：每 ``REGISTER_WINDOW_SECONDS`` 秒 N 次。
#: ⚠️ 默认值要能撑住一次全量基线（eval + e2e 一轮约 10 次注册），
#:   但要小到"批量刷号"明显划不来。
REGISTER_LIMIT = 20
REGISTER_WINDOW_SECONDS = 3600

#: 本机来源（loopback 且没有转发头）的配额。⚠️ 刻意宽松：
#: 验收脚本与本机 curl 都在这个桶里，压到和公网一样会天天假红。
LOCAL_REGISTER_LIMIT = 500

# ---------------- 登录失败（D138）----------------
#
# 🔑 **只数失败，不数成功**。这是与注册限流最重要的差别：
#   注册是"每次请求都算一次尝试"，登录不是 —— 正常用户一天登录十几次，
#   按"次数"限会**天天把主人锁在门外**，而它挡不住的恰恰是成功登录。
#   ⇒ 成功**不记录**（基线里三个 eval + 三套 e2e 每轮几十次登录，
#   若成功也计数，本机宽松桶也会被自己的基线填满）。
#
#: 同一客户端 IP 在 ``LOGIN_FAIL_WINDOW_SECONDS`` 秒内第 N+1 次**失败** → 429。
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW_SECONDS = 300

#: 同一**账号**在窗口内第 N+1 次失败 → 429。
#: 🔑 与 IP 桶**都要有**：IP 桶挡"一个来源喷很多账号"，
#: 账号桶挡"很多来源打同一个账号"（换 IP 就没用）—— 而后者才是
#: "保护某个具体用户的密码"这件事真正需要的。
LOGIN_FAIL_ACCOUNT_LIMIT = 20
LOGIN_FAIL_ACCOUNT_WINDOW_SECONDS = 900

#: 本机来源的登录失败桶：比公网宽（基线里几乎没有失败登录），
#: 但**仍然是有限的** —— 否则"本机来源不受限"就等于本机也没有防护。
LOCAL_LOGIN_FAIL_LIMIT = 100

#: 账号桶的键前缀（与 `ip:` / `local` 区分，避免撞键）
_ACCOUNT_PREFIX = "acct:"

#: `x-forwarded-for` 与 `cf-connecting-ip` 都收 —— 两个都只在"对端不可信地
#: 属于本机/内网"时采信。⚠️ **第一个 XFF 值是调用方自己写的**，
#: 采信它的前提是对端不是外部（见模块 docstring）。
FORWARD_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")

_buckets: dict[str, list[float]] = defaultdict(list)
_lock = threading.Lock()

#: 便于测试清场（也便于 eval 脚本在必要时重来一轮）
def reset_buckets() -> None:
    with _lock:
        _buckets.clear()


#: 只有这些网段的调用方，才允许我们采信它发来的转发头。
#:
#: 🔑 **不用 `ipaddress.is_private`**：它把 RFC 5737 的文档网段
#: （`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`）也报成 private，
#: 于是"对端是公网"的判断在这些地址上会翻车 —— 而这些地址正是测试里
#: 常用的"公网样例"（第一版就是这么错的：测试用 198.51.100.7 当公网对端，
#: 结果被当成内网，于是采信了伪造的转发头）。
#:
#: 这里显式列出"**外部不可能拥有的源地址**"：回环、RFC1918、ULA、链路本地。
_TRUSTED_PEERS = tuple(
    ipaddress.ip_network(n) for n in (
        "127.0.0.0/8", "::1/128",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "fc00::/7", "fe80::/10",
    )
)


def _is_private(host: str) -> bool:
    """对端是不是本机/内网（决定能不能信转发头）。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP（Unix socket / 代理后的怪值）⇒ 当成"不可信"，走直连分支
        return False
    return any(addr in net for net in _TRUSTED_PEERS)


def client_key(peer: str | None, headers) -> str | None:  # noqa: ANN001
    """算出限流桶的键。

    @return 桶键；`"local"` 表示"来自本机"。
    """
    peer = (peer or "").strip()
    trusted_forward = bool(peer) and _is_private(peer)
    if trusted_forward:
        for name in FORWARD_HEADERS:
            raw = headers.get(name)
            if not raw:
                continue
            value = raw.split(",")[0].strip()
            if value:
                return f"ip:{value}"
    if peer and not _is_private(peer):
        return f"ip:{peer}"
    # loopback 且没有可用转发头 ⇒ 本机来源
    return "local"


def peek(limit: int, window: int, key: str, now: float | None = None) -> tuple[bool, int]:
    """看一眼还有没有配额，**不记录**。返回 `(是否放行, Retry-After 秒数)`。

    ⚠️ 窗口用**滑动窗口**（保留每次请求的时间戳）而不是固定分桶：
    固定分桶的边界效应会让"窗口末尾打 N 次 + 窗口开头打 N 次"在 1 秒内通过 2N 次，
    而滑动的代价只是几行。
    """
    now = time.monotonic() if now is None else now
    with _lock:
        hits = [h for h in _buckets[key] if now - h < window]
        _buckets[key] = hits              # 顺手清掉过期项（不记录新命中）
        if len(hits) >= limit:
            return False, max(1, int(window - (now - hits[0])))
        return True, 0


def record(window: int, key: str, now: float | None = None) -> None:
    """记一次命中（滑动窗口）。"""
    now = time.monotonic() if now is None else now
    with _lock:
        _buckets[key] = [h for h in _buckets[key] if now - h < window]
        _buckets[key].append(now)


def check(limit: int, window: int = REGISTER_WINDOW_SECONDS, key: str = "local",
          now: float | None = None) -> tuple[bool, int]:
    """占一个配额 = `peek` + `record`。返回 `(是否放行, 建议的 Retry-After 秒数)`。"""
    ok, retry = peek(limit, window, key, now)
    if ok:
        record(window, key, now)
    return ok, retry


def account_key(email: str) -> str:
    """账号桶的键。⚠️ **必须归一化**：大小写或首尾空格不同就是另一个桶，
    而 `User.email` 的唯一约束是按原样比对的 —— 归一化错了等于限流可绕。"""
    return f"{_ACCOUNT_PREFIX}{(email or '').strip().lower()}"


def login_fail_budget(ip_key: str, email: str) -> tuple[bool, int, str]:
    """登录**前置**检查：IP 桶与账号桶都要有配额。

    @return `(是否放行, Retry-After 秒数, 被拒的是哪个桶)`
    """
    now = time.monotonic()
    limit = LOCAL_LOGIN_FAIL_LIMIT if ip_key == "local" else LOGIN_FAIL_LIMIT
    ok, retry = peek(limit, LOGIN_FAIL_WINDOW_SECONDS, ip_key, now)
    if not ok:
        return False, retry, "ip"
    ok, retry = peek(LOGIN_FAIL_ACCOUNT_LIMIT, LOGIN_FAIL_ACCOUNT_WINDOW_SECONDS,
                     account_key(email), now)
    if not ok:
        return False, retry, "account"
    return True, 0, ""


def record_login_fail(ip_key: str, email: str) -> None:
    """记一次**失败**（IP 桶与账号桶各一次）。⚠️ 只在密码校验失败后调用。"""
    now = time.monotonic()
    record(LOGIN_FAIL_WINDOW_SECONDS, ip_key, now)
    record(LOGIN_FAIL_ACCOUNT_WINDOW_SECONDS, account_key(email), now)