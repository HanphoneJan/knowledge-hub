"""开发环境种子数据。

创建两个用户、三个知识库，并打印各自的 API Key。
**明文 key 只在这里输出一次**（库里只存 hash）。

用法：
    uv run python -m services.api.scripts.seed_dev
    uv run python -m services.api.scripts.seed_dev --reset
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from ...common.auth import generate_api_key, hash_password
from ...common.config import get_settings
from ...common.db import dispose_engines, sync_session_scope
from ...common.models import (
    ApiKey,
    KnowledgeBase,
    User,
    Workspace,
    WorkspaceMember,
)

# 🔑 口令从配置读（`SEED_DEV_PASSWORD`），**不在仓库里写死**：
#   原来这里有一个固定口令 + 真实姓名，公开后等于给出可登录的开发账号。
from ...common.config import get_settings as _get_settings

_DEV_PASSWORD = _get_settings().seed_dev_password

SEED_USERS = [
    {
        "email": "dev-a@example.test",
        "password": _DEV_PASSWORD,
        "display_name": "开发账号 A",
    },
    {
        "email": "dev-b@example.test",
        "password": _DEV_PASSWORD,
        "display_name": "同事 B",
    },
]

SEED_KBS = [
    {"name": "我的文档", "visibility": "private", "source_kind": "file",
     "description": "个人文档与笔记"},
    {"name": "聊天记录", "visibility": "private", "source_kind": "chat",
     "description": "D41：每 50 条消息一个文档"},
    {"name": "团队共享", "visibility": "workspace", "source_kind": "file",
     "description": "D43：工作区可见，所有成员可编辑"},
]

WORKSPACE = {"name": "个人知识库", "description": "默认工作区"}


def reset() -> None:
    with sync_session_scope() as s:
        s.execute(
            text(
                "TRUNCATE audit_log, chat_messages, chat_sessions, chunks, jobs, "
                "documents, knowledge_bases, workspace_members, workspaces, "
                "api_keys, users, blobs RESTART IDENTITY CASCADE"
            )
        )
    print("已清空所有表")


def seed() -> None:
    settings = get_settings()

    with sync_session_scope() as s:
        # 先建用户（工作区必须有 owner_id，所以顺序不能反）
        keys: list[tuple[str, str, int]] = []
        for u in SEED_USERS:
            user = User(
                email=u["email"],
                password_hash=hash_password(u["password"]),
                display_name=u["display_name"],
            )
            s.add(user)
            s.flush()
            plaintext, key_hash, prefix = generate_api_key()
            s.add(
                ApiKey(
                    user_id=user.id,
                    name="dev",
                    key_hash=key_hash,
                    key_prefix=prefix,
                )
            )
            keys.append((u["email"], plaintext, user.id))

        ws = Workspace(
            name=WORKSPACE["name"],
            description=WORKSPACE["description"],
            owner_id=keys[0][2],
        )
        s.add(ws)
        s.flush()

        for i, (_, _, uid) in enumerate(keys):
            s.add(
                WorkspaceMember(
                    workspace_id=ws.id,
                    user_id=uid,
                    role="owner" if i == 0 else "member",
                )
            )

        owner_id = keys[0][2]
        for kb in SEED_KBS:
            s.add(
                KnowledgeBase(
                    workspace_id=ws.id,
                    name=kb["name"],
                    description=kb["description"],
                    visibility=kb["visibility"],
                    source_kind=kb["source_kind"],
                    embed_model=settings.text_embed_model,
                    created_by=owner_id,
                )
            )

    print(f"\n工作区: {WORKSPACE['name']}（id=1）")
    print(f"知识库: " + " · ".join(f"{kb['name']}" for kb in SEED_KBS))
    print("\nAPI Key（只显示这次，库里只存哈希）:")
    for email, plaintext, uid in keys:
        print(f"  {email:26s} {plaintext}")
        print(f"  {'':26s} user_id={uid}")
    print(f"\n密码统一为 {_DEV_PASSWORD}（来自 SEED_DEV_PASSWORD 配置）")


def _require_password() -> None:
    """口令没配就停：默认口令进公开仓库比报错糟得多。"""
    if not _DEV_PASSWORD:
        raise SystemExit("❌ SEED_DEV_PASSWORD 没配（见 .env.example），拒绝用默认口令建号。")


def main() -> int:
    _require_password()
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if args.reset:
        reset()
    seed()
    dispose_engines()
    return 0


if __name__ == "__main__":
    sys.exit(main())
