"""超管用户管理（D173）的单元测试。

覆盖：`assert_superadmin` 门禁、`PATCH /admin/users/{id}` 的两条防自锁守卫。
用假 session，不连库、不发网络请求。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from services.common.auth import UserContext, assert_superadmin, hash_password, verify_password
from services.common.errors import Forbidden, InvalidRequest
from services.common.schemas import UpdateMeReq, UpdateUserReq

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def teardown_module() -> None:
    _LOOP.close()


def _ctx(user_id: int = 1, *, superadmin: bool = True) -> UserContext:
    return UserContext(
        user_id=user_id, display_name="u", email="u@t.local", api_key_id=None,
        is_superadmin=superadmin,
    )


class _Res:
    def __init__(self, one=None, one_or_none=None) -> None:
        self._one = one
        self._oon = one_or_none

    def scalar_one(self):
        return self._one

    def scalar_one_or_none(self):
        return self._oon


class _Sess:
    def __init__(self, obj=None) -> None:
        self.obj = obj
        self.commits = 0

    async def get(self, _model, _pk):  # noqa: ANN001
        return self.obj

    async def execute(self, _stmt):  # noqa: ANN001 — 昵称查重返回"没占用"
        return _Res(one=None, one_or_none=None)

    async def commit(self) -> None:
        self.commits += 1


def _user(**kw):
    base = dict(id=2, email="x@t.local", display_name="X", nickname=None,
                role="user", is_active=True, created_at=dt.datetime.now(dt.UTC))
    base.update(kw)
    return SimpleNamespace(**base)


# ============================================================
# 门禁
# ============================================================


def test_assert_superadmin_rejects_normal_user():
    with pytest.raises(Forbidden):
        assert_superadmin(_ctx(superadmin=False))


def test_assert_superadmin_allows_superadmin():
    assert_superadmin(_ctx(superadmin=True))  # 不抛


# ============================================================
# 防自锁守卫
# ============================================================


def test_cannot_demote_last_superadmin(monkeypatch):
    from services.api.routers import users as ur

    async def _one(_s):
        return 1

    monkeypatch.setattr(ur, "_active_superadmin_count", _one)
    target = _user(id=2, role="superadmin", is_active=True)
    sess = _Sess(target)
    with pytest.raises(InvalidRequest):
        run(ur.update_user(2, UpdateUserReq(role="user"), _ctx(), sess))
    assert sess.commits == 0, "被拒时不能提交"


def test_cannot_deactivate_last_superadmin(monkeypatch):
    from services.api.routers import users as ur

    async def _one(_s):
        return 1

    monkeypatch.setattr(ur, "_active_superadmin_count", _one)
    target = _user(id=2, role="superadmin", is_active=True)
    with pytest.raises(InvalidRequest):
        run(ur.update_user(2, UpdateUserReq(is_active=False), _ctx(), _Sess(target)))


def test_cannot_deactivate_self(monkeypatch):
    from services.api.routers import users as ur

    async def _many(_s):
        return 5

    monkeypatch.setattr(ur, "_active_superadmin_count", _many)
    target = _user(id=1, role="superadmin", is_active=True)
    with pytest.raises(InvalidRequest):
        run(ur.update_user(1, UpdateUserReq(is_active=False), _ctx(user_id=1), _Sess(target)))


def test_update_role_and_nickname(monkeypatch):
    from services.api.routers import users as ur

    async def _many(_s):
        return 5

    monkeypatch.setattr(ur, "_active_superadmin_count", _many)
    target = _user(id=2, role="user", is_active=True)
    sess = _Sess(target)
    out = run(ur.update_user(
        2, UpdateUserReq(role="superadmin", nickname="bob"), _ctx(), sess
    ))
    assert target.role == "superadmin"
    assert target.nickname == "bob"
    assert out.role == "superadmin"
    assert sess.commits == 1


def test_update_missing_user_is_404():
    from services.api.routers import users as ur
    from services.common.errors import ResourceNotFound

    with pytest.raises(ResourceNotFound):
        run(ur.update_user(999, UpdateUserReq(role="user"), _ctx(), _Sess(None)))


def test_update_requires_superadmin():
    from services.api.routers import users as ur

    with pytest.raises(Forbidden):
        run(ur.update_user(2, UpdateUserReq(role="user"), _ctx(superadmin=False),
                           _Sess(_user())))


# ============================================================
# 改自己的资料（D175 · PATCH /auth/me）
# ============================================================


def _me_ctx(uid: int = 1) -> UserContext:
    return UserContext(user_id=uid, display_name="u", email="u@t.local", api_key_id=None)


def test_update_me_display_name_needs_no_password():
    from services.api.routers.auth import update_me

    u = _user(id=1, password_hash=hash_password("old-pass-123"))
    run(update_me(UpdateMeReq(display_name="New Name"), _Sess(u), _me_ctx(1)))
    assert u.display_name == "New Name"


def test_update_me_email_requires_current_password():
    from services.api.routers.auth import update_me

    u = _user(id=1, password_hash=hash_password("old-pass-123"))
    with pytest.raises(InvalidRequest):
        run(update_me(UpdateMeReq(email="new@t.local"), _Sess(u), _me_ctx(1)))
    with pytest.raises(InvalidRequest):
        run(update_me(UpdateMeReq(email="new@t.local", current_password="wrong"),
                      _Sess(u), _me_ctx(1)))
    assert u.email == "x@t.local", "校验失败时不能改邮箱"


def test_update_me_email_ok_with_current_password():
    from services.api.routers.auth import update_me

    u = _user(id=1, password_hash=hash_password("old-pass-123"))
    run(update_me(
        UpdateMeReq(email="new@t.local", current_password="old-pass-123"),
        _Sess(u), _me_ctx(1),
    ))
    assert u.email == "new@t.local"


def test_update_me_password_changes_hash():
    from services.api.routers.auth import update_me

    u = _user(id=1, password_hash=hash_password("old-pass-123"))
    run(update_me(
        UpdateMeReq(new_password="brand-new-123", current_password="old-pass-123"),
        _Sess(u), _me_ctx(1),
    ))
    assert verify_password(u.password_hash, "brand-new-123")


# ============================================================
# 超管改任意用户：邮箱 + 重置密码（D175）
# ============================================================


def test_admin_can_change_email_and_reset_password(monkeypatch):
    from services.api.routers import users as ur

    async def _many(_s):
        return 5

    monkeypatch.setattr(ur, "_active_superadmin_count", _many)
    target = _user(id=2)
    run(ur.update_user(
        2, UpdateUserReq(email="z@t.local", new_password="reset-me-123"),
        _ctx(), _Sess(target),
    ))
    assert target.email == "z@t.local"
    assert verify_password(target.password_hash, "reset-me-123")
