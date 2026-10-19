"""建一个账号（自助注册关闭后唯一的入口，D138）。

## 为什么需要它

`ALLOW_REGISTER=false`（对外部署的推荐配置）把 `POST /auth/register`
对公网关掉了 —— 这是对的（个人实例不该让陌生人自助注册）。
但那个端点**本来也是自己加第二个账号的便手段**，关掉它就一起没了。

⇒ 所以要有**CLI 入口**：它是本机操作（要能读库、要能执行代码），
不是网络暴露面。**刻意不做"带一次性 token 的 HTTP 后门"** ——
那种东西会变成"又一个能被猜到的入口"。

## 用法

```bash
# 交互（密码不回显、不进 shell 历史）—— 推荐
uv run python -m services.api.scripts.create_user

# 非交互（CI / 一次性）
uv run python -m services.api.scripts.create_user \\
    --email a@example.com --display-name "A"
```

⚠️ **明文 API Key 只在这里输出一次**（库里只存哈希，取不回来）。
⚠️ 邮箱已存在时**直接报错退出**，不做"改密码"之类的事 ——
改密码走另一个端点/流程，不该混在这个脚本里。

## 刻意不做的事

- ❌ 不加 `--reset` / `--password`：清库与弱密码都不是这个脚本该干的事
- ❌ 不打印密码哈希、不接受"明文密码从命令行传"以外的落盘方式
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from ...common.auth import generate_api_key, hash_password
from ...common.db import sync_session_scope
from ...common.models import ApiKey, User, Workspace, WorkspaceMember


def create(
    email: str,
    password: str,
    display_name: str,
    *,
    nickname: str | None = None,
    role: str = "user",
) -> tuple[str, int]:
    """建账号 + 一把 key + 一个个人工作区（owner）。

    🔑 与 `POST /auth/register` **同一套东西**（同样的默认值、同样的
    "同时建个人工作区并设为 owner"），否则会出现"CLI 建的账号进首页
    看到一个空白页、连第一个 KB 都建不了"的分歧。
    """
    with sync_session_scope() as s:
        exists = s.execute(select(User.id).where(User.email == email)).first()
        if exists is not None:
            raise SystemExit(f"邮箱已注册：{email}（这个脚本不改密码）")
        if nickname:
            nick = s.execute(
                select(User.id).where(User.nickname == nickname)
            ).first()
            if nick is not None:
                raise SystemExit(f"昵称已被占用：{nickname}")

        user = User(email=email, password_hash=hash_password(password),
                    display_name=display_name or email.split("@")[0],
                    nickname=nickname or None, role=role)
        s.add(user)
        s.flush()

        plaintext, key_hash, prefix = generate_api_key()
        s.add(ApiKey(user_id=user.id, name="cli",
                     key_hash=key_hash, key_prefix=prefix))

        ws = Workspace(name=f"{user.display_name} 的工作区",
                       description=None, owner_id=user.id)
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        return plaintext, user.id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="建一个账号（D138/D173）")
    ap.add_argument("--email", help="登录邮箱；不给就交互问")
    ap.add_argument("--display-name", default="", help="显示名（默认取邮箱前缀）")
    ap.add_argument("--nickname", default="", help="唯一昵称（可作登录名，D174）")
    ap.add_argument(
        "--superadmin", action="store_true",
        help="建成超级管理员（D173，越过三档可见性）",
    )
    args = ap.parse_args(argv)

    email = (args.email or input("邮箱: ")).strip()
    if "@" not in email:
        print(f"邮箱不像样：{email!r}", file=sys.stderr)
        return 2
    password = getpass.getpass("密码（不回显）: ")
    if len(password) < 8:
        print("密码至少 8 位", file=sys.stderr)
        return 2

    plaintext, uid = create(
        email, password, args.display_name,
        nickname=args.nickname.strip() or None,
        role="superadmin" if args.superadmin else "user",
    )
    print(f"✅ 已建账号 id={uid} · {email}"
          + ("（超级管理员）" if args.superadmin else ""))
    print(f"🔑 API Key（**只显示这一次**，库里只存哈希）：\n   {plaintext}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())