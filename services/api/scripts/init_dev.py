"""阶段 0 验收脚本：`GET /health` 返回 db:ok, qdrant:ok。

用法：
    uv run python -m services.api.scripts.init_dev          # 只建基础设施
    uv run python -m services.api.scripts.init_dev --reset  # 清库重来

覆盖 D52 的验收标准，并额外做一轮冒烟（建用户/工作区/KB、Qdrant 过滤、
任务队列并发领取），确保阶段 1 起步时地基是可靠的。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import secrets
import sys
import time

from sqlalchemy import text

from ...common.auth import (
    UserContext,
    build_qdrant_filter,
    generate_api_key,
    hash_password,
    load_user_context,
)
from ...common.config import get_settings
from ...common.config import get_settings

from ...common.db import check_db, dispose_engines, sync_session_scope
from ...common.jobs import enqueue, job_stats
from ...common.models import (
    ApiKey,
    Blob,
    Document,
    KnowledgeBase,
    User,
    Workspace,
    WorkspaceMember,
)
from ...common.qdrant import (
    ALL_COLLECTIONS,
    close_client,
    ensure_collections,
    payload_schema,
    check_qdrant,
)
from ...common.storage import ensure_dirs, store_bytes

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "  "

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = PASS if ok else FAIL
    print(f"{mark} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ============================================================


TEST_EMAIL = "dev@local.test"
#: 验收用的占位 blob。固定值会让脚本不能重复跑（上一轮残留会撞主键），
#: 所以每次生成新的 —— 它只用来满足 documents.blob_sha 的外键约束。
_PLACEHOLDER_SALT = secrets.token_hex(8)


def placeholder_blob() -> Blob:
    return Blob(
        sha256=hashlib.sha256(_PLACEHOLDER_SALT.encode()).hexdigest(),
        size_bytes=1,
        storage_path="(验收占位，非真实文件)",
    )


def cleanup_test_data() -> None:
    """只删验收脚本自己造的那份数据。

    刻意**不用 TRUNCATE**：那会把 `seed_dev.py` 建的开发数据一起清掉，
    变成"跑一次验收脚本就丢一次种子数据"的陷阱。
    删除用户会靠 ON DELETE CASCADE 带走它的 KB / 文档 / 任务。

    顺带清理**引用计数为 0 的 blob**（验收脚本建的占位 blob）。
    靠 CASCADE 清不到它们 —— blob 没有反向外键指向 users。
    """
    with sync_session_scope() as s:
        uid = s.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": TEST_EMAIL}
        ).scalar()
        if uid is not None:
            s.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})

        orphans = s.execute(
            text(
                "DELETE FROM blobs WHERE sha256 NOT IN "
                "(SELECT blob_sha FROM documents)"
            )
        ).rowcount
        if orphans:
            print(f"{INFO} 清理 {orphans} 个无引用 blob")


def reset_db() -> None:
    section("重置数据库（--reset：清空全部表）")
    tables = (
        "audit_log, chat_messages, chat_sessions, chunks, jobs, documents, "
        "knowledge_bases, workspace_members, workspaces, api_keys, users, blobs"
    )
    with sync_session_scope() as s:
        s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    print(f"{PASS} 已清空全部 12 张表并重置序列")


async def check_infra() -> bool:
    section("1. 基础设施连通性")

    db = await check_db()
    check("Postgres 可连接", bool(db.get("ok")), db.get("version", db.get("error", "")))

    qd = await check_qdrant()
    check("Qdrant 可连接", bool(qd.get("ok")), str(qd.get("collections", qd.get("error", ""))))

    if db.get("ok"):
        with sync_session_scope() as s:
            ver = s.execute(text("SELECT extversion FROM pg_extension WHERE extname='pg_trgm'")).scalar()
        check("pg_trgm 扩展已装（D17 子串搜索依赖）", ver is not None, f"v{ver}")

        with sync_session_scope() as s:
            cnt = s.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public'"
                )
            ).scalar()
        check("表结构已建", cnt >= 12, f"{cnt} 张表（含 alembic_version）")

    return bool(db.get("ok")) and bool(qd.get("ok"))


def check_qdrant_layout() -> None:
    section("2. Qdrant 布局（D7 三 collection + payload 索引）")

    created = ensure_collections()
    print(f"{INFO} collection: {created}")

    for name in ALL_COLLECTIONS:
        schema = payload_schema(name)
        missing = {"kb_id", "doc_id", "workspace_id"} - set(schema)
        check(
            f"{name} payload 索引完整",
            not missing,
            f"{len(schema)} 个索引" if not missing else f"缺 {sorted(missing)}",
        )


def check_blob_store() -> None:
    section("3. 内容寻址存储（D35）")
    ensure_dirs()
    sha, size = store_bytes(b"pkh smoke test" * 100)
    from ...common.storage import blob_path, delete_blob, exists

    check("写入 blob", exists(sha), f"sha={sha[:12]}… {size} 字节")
    check("内容寻址可复算", blob_path(sha).exists())

    sha2, _ = store_bytes(b"pkh smoke test" * 100)
    check("同内容去重（同 sha）", sha == sha2)

    delete_blob(sha)
    check("删除 blob", not exists(sha))


def check_orm_and_jobs() -> tuple[int, str, int]:
    section("4. ORM + 任务队列（D14）")

    with sync_session_scope() as s:
        # 只清自己的测试数据（TRUNCATE 会连 seed_dev 的开发数据一起清掉）
        cleanup_test_data()

        user = User(
            email=TEST_EMAIL,
            password_hash=hash_password(_DEV_PASSWORD),
            display_name="本地开发",
        )
        s.add(user)
        s.flush()

        ws = Workspace(name="默认工作区", owner_id=user.id)
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))

        plaintext, key_hash, prefix = generate_api_key()
        s.add(
            ApiKey(
                user_id=user.id, name="dev", key_hash=key_hash, key_prefix=prefix
            )
        )
        s.flush()

        kb = KnowledgeBase(
            workspace_id=ws.id, name="冒烟测试库", created_by=user.id,
            embed_model=get_settings().text_embed_model,
        )
        s.add(kb)
        s.flush()

        blob = placeholder_blob()
        s.add(blob)
        s.flush()
        doc = Document(
            kb_id=kb.id, file_name="smoke.pdf", content_hash=blob.sha256,
            blob_sha=blob.sha256, uploaded_by=user.id,
        )
        s.add(doc)
        s.flush()

        job = enqueue(s, "parse", doc_id=doc.id, kb_id=kb.id)
        uid, kid, did, jid = user.id, kb.id, doc.id, job.id

    check("建用户/工作区/KB/文档", True, f"kb={kid} doc={did}")

    with sync_session_scope() as s:
        dup = enqueue(s, "parse", doc_id=did, kb_id=kid)
        check("任务去重（同 doc+kind 不重复入队）", dup.id == jid)

    with sync_session_scope() as s:
        stats = job_stats(s)
    check("任务统计可读", isinstance(stats, dict), str(stats))

    # 注意：这里**不能**清库 —— 下面的认证检查还要用这份数据。
    # 清理放到最后（main 的收尾）。
    return uid, plaintext, kid


async def check_auth(uid: int, plaintext: str, kid: int) -> None:
    section("5. 认证与租户过滤（D8 fail-closed）")

    from ...common.auth import assert_kb_access
    from ...common.db import get_async_sessionmaker
    from ...common.errors import ResourceNotFound

    factory = get_async_sessionmaker()
    async with factory() as session:
        ctx = await load_user_context(session, plaintext)
        # load_user_context 会更新 last_used_at，要提交才落库
        await session.commit()
        check("API Key 校验通过", ctx.user_id == uid, f"user={ctx.display_name}")
        check("可读 KB 范围已推导", kid in ctx.readable_kb_ids, str(sorted(ctx.readable_kb_ids)))

        await assert_kb_access(session, ctx, kid)
        check("有权访问自己的 KB", True)

        try:
            await assert_kb_access(session, ctx, 99999)
            check("越权访问被拒", False, "未抛异常 —— 这是安全问题")
        except ResourceNotFound as exc:
            check("越权/不存在被拒", exc.code == "KB_NOT_FOUND",
                  f"code={exc.code} status={exc.status_code}")

    # fail-closed 过滤构造
    allowed = build_qdrant_filter(ctx, kid)
    denied = build_qdrant_filter(ctx, 99999)
    empty_ctx = UserContext(
        user_id=99, display_name="X", email="x@y.z", api_key_id=None,
        readable_kb_ids=frozenset(),
    )
    check("有权限 → 具体 kb_id 过滤", "value" in str(allowed) and str(kid) in str(allowed))
    check("无权限 → 必然空集（非报错，避免存在性探测）", "-1" in str(denied))
    check("空权限范围 → 必然空集", "-1" in str(build_qdrant_filter(empty_ctx, None)))

    # 坏 key 必须被拒
    factory = get_async_sessionmaker()
    async with factory() as session:
        from ...common.auth import InvalidToken

        try:
            await load_user_context(session, "sk-pkh_totally_bogus")
            check("无效 API Key 被拒", False, "未抛异常 —— 这是安全问题")
        except InvalidToken as exc:
            check("无效 API Key 被拒", exc.status_code == 401, f"code={exc.code}")

    # 清掉验收数据（只删自己的，不动 seed_dev 的开发数据）
    cleanup_test_data()
    print(f"{PASS} 验收数据已清理")


async def amain() -> int:
    t0 = time.time()
    if not await check_infra():
        print(
            f"\n{FAIL} 基础设施不可用，后续检查跳过。"
            "先按 deploy/README.md 启动服务（bash deploy/dev.sh start）。"
        )
        return 1

    check_qdrant_layout()
    check_blob_store()
    uid, plaintext, kid = check_orm_and_jobs()
    await check_auth(uid, plaintext, kid)

    elapsed = (time.time() - t0) * 1000
    section("结果")
    if _failures:
        print(f"{FAIL} {len(_failures)} 项失败：")
        for f in _failures:
            print(f"     - {f}")
    else:
        print(f"{PASS} 全部通过（{elapsed:.0f}ms）")
        print("\n下一步：阶段 1 竖切片（D52）—— 认证 → 上传 → 索引 → 检索 → L3")

    close_client()
    dispose_engines()
    return 1 if _failures else 0


_DEV_PASSWORD = get_settings().seed_dev_password


def _require_password() -> None:
    """口令没配就停（默认口令进公开仓库比报错糟得多）。"""
    if not _DEV_PASSWORD:
        raise SystemExit("❌ SEED_DEV_PASSWORD 没配（见 .env.example），拒绝用默认口令建号。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先清空所有表")
    args = parser.parse_args()

    print("\033[1mKnowledge Hub · 阶段 0 验收\033[0m")
    print(f"环境: {get_settings().api_env} · Qdrant {get_settings().qdrant_url}")

    if args.reset:
        reset_db()

    # 全部异步检查必须在**同一个** event loop 里跑：
    # 异步引擎是模块级单例，asyncpg 连接池绑定创建它的 loop，
    # 跨 loop 复用会抛 "attached to a different loop"。
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
