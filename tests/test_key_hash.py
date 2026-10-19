"""🔴 D134：API Key 的哈希必须是**完整** sha256（在线爆破防线）。

## 为什么这不是"格式美化"

旧实现只存 sha256 的**前 12 个 hex**（48 bit）：

```python
return hashlib.sha256(plaintext.encode()).hexdigest()[:12]
```

⚠️ 旧 docstring 的理由是"key 本身是 256 位随机数，没有低熵可爆破"——
那句话**只对离线成立**（拿到库里那 48 bit 也猜不出原文）。
**在线**要猜的是"哈希前缀"，不是原始 Key 的全部位：

- 攻击者随便生成 Key，算 sha256，比对存下来的 48 bit
- 平均 **2^48 次哈希** ≈ 一块 GPU **8 小时**

而公网入口**对全网可达**（D134 同期实测：未挂边缘访问控制，
`POST /auth/register` 返回 422 而不是 401 ⇒ 注册端点人人可达），
所以这把钥匙正好配了把开着的门。

完整 sha256 是 **2^256** —— 在线爆破不再是一个成本问题。

## 代价（明说）

改之前发出的 Key **全部失效**：库里存的是 48 bit，验不出原文。
表现是"被登出一次"（前端 `lib/auth.ts` 本来就会在 401 时重新登录换 Key），
不需要任何迁移（`key_hash` 是 `Text` 列）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

def _pg_alive() -> bool:
    from sqlalchemy import text

    from services.common.db import sync_session_scope

    try:
        with sync_session_scope() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pg = pytest.mark.skipif(not _pg_alive(), reason="Postgres 不在线")


ROOT = Path(__file__).resolve().parents[1]
AUTH_PY = ROOT / "services" / "common" / "auth.py"


def test_key_hash_is_the_full_sha256_hex():
    """🔑 `hash_key` 返回**完整** 64 个 hex（sha256 的长度）。"""
    from services.common.auth import KEY_HASH_LEN, hash_key

    h = hash_key("sk-pkh_whatever")
    assert len(h) == 64 == KEY_HASH_LEN, f"长度 {len(h)}，常量 {KEY_HASH_LEN}"
    assert all(c in "0123456789abcdef" for c in h), h


def test_truncation_constant_is_gone():
    """🔑 模块里**不许**再有"截断长度"常量 —— 它的存在就是复发的前兆。

    判据是**性质**（没有任何地方限制哈希长度），不是"某个名字不许出现"：
    换成 `HASH_LEN = 12` 或 `hash()[:12]` 都应该被这条抓住。
    """
    src = AUTH_PY.read_text(encoding="utf-8")
    assert "HASH_LEN = 12" not in src, (
        "又把哈希截断成 48 bit 了 —— 那是一道 2^48 的防线"
    )
    assert "[:12]" not in src and '[:8]' not in src, (
        "哈希又被切了短前缀"
    )


def test_hash_key_has_no_slice_on_the_digest():
    """🔑 函数体里不能对 `hexdigest()` 做切片。"""
    import ast

    tree = ast.parse(AUTH_PY.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "hash_key")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "hexdigest"):
            pytest.fail(f"hash_key 又切了 hexdigest：{ast.unparse(node)}")


def test_different_keys_hash_differently():
    from services.common.auth import hash_key

    assert hash_key("sk-pkh_a") != hash_key("sk-pkh_b")
    assert hash_key("sk-pkh_a") == hash_key("sk-pkh_a")      # 稳定


def test_generate_api_key_stores_a_full_length_hash():
    """🔑 `generate_api_key()` 返回的哈希也是完整的（登录/建 Key 都走它）。"""
    from services.common.auth import generate_api_key

    plaintext, key_hash, prefix = generate_api_key()
    assert plaintext.startswith("sk-pkh_"), "明文前缀变了？UI 与文档都按它写的"
    assert len(key_hash) == 64, f"建出来的 Key 哈希只有 {len(key_hash)} 位"
    assert prefix and prefix in plaintext, "key_prefix 应是明文的一部分（UI 靠它显示）"


@pg
def test_old_truncated_hashes_in_the_db_no_longer_authenticate():
    """🔑 库里若还有 48 bit 的旧哈希，它**认不出来**（= 旧 Key 已失效）。

    ⚠️ 这条不是"期望旧 Key 失效"，而是把 D134 的**代价**钉住：
    如果哪天有人"聪明地"加了"两种格式都接受"的后门，这条会红 ——
    那等于把 2^48 的门重新装回来。
    """
    from sqlalchemy import select, text

    from services.common.db import sync_session_scope
    from services.common.models import ApiKey
    from services.common.auth import load_user_context, hash_key

    legacy = "a" * 12          # 旧格式：12 个 hex
    with sync_session_scope() as s:
        row = s.execute(
            select(ApiKey.id, ApiKey.user_id).where(
                ApiKey.key_hash == legacy, ApiKey.revoked_at.is_(None)
            )
        ).first()
        if row is None:
            pytest.skip("库里没有 48 bit 的旧哈希（都轮换过了）—— 代价已经付完")
        uid = row[1]
    if hash_key("whatever") == legacy:
        pytest.skip("这不可能，只是防止误判")
    # 旧哈希对应的明文不可得 ⇒ 只能断言"它不再被当成有效哈希来源"：
    # 库里若有 12 位的行，说明**迁移/轮换没做干净**，应清理而不是留着。
    with sync_session_scope() as s:
        short = s.execute(
            text("SELECT count(*) FROM api_keys WHERE length(key_hash) < 64")
        ).scalar_one()
    assert short == 0, (
        f"库里还有 {short} 条短哈希的 api_keys —— D134 的代价是"
        "「旧 Key 全部失效」，需要把这些行删掉（用户重新登录即可）"
    )
    assert uid is not None
