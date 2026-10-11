#!/usr/bin/env python3
"""造一批**持久**的照片，让视觉腿（`kb_visual`）真的有活数据（D152）。

## 为什么需要这个脚本

视觉腿在本机长期是 **0 个点**，而那**不是坏了**：唯一扫过图的文档都被 e2e
清库软删了，按 D7「先删 PG → 异步清 Qdrant」点也被清掉 —— 0 点是**正确的稳态**。
但它让两个"承诺"没法兑现：

- **M4**「扫描件走 colSmol 视觉召回」在当前数据上**看不到**（代码跑过，没东西可看）
- **D50**（要不要让 API 进程也加载 colSmol）**没法决策** —— 没有数据就测不出
  视觉腿查询的收益，而代价是确定的（实测加载 13s / 2.5GB 常驻）

## 关键设计：造完之后**每轮基线都不会删掉它**

| 谁会清场 | 清什么 | 本脚本造的东西 |
|---|---|---|
| `eval/api_1b` | `%@t.local` | 持久照片库账号（`KEEPER_EMAIL`）⇒ **不碰** |
| `eval/api_1c` / `1d` | 自己的固定邮箱 | 不碰 |
| `web/e2e/*` | `1e-` / `1f-` / `1g-` **前缀** | 无此前缀 ⇒ **不碰** |
| `deploy/monitor.py` | 只读 | 不碰 |

⚠️ **别把 `KEEPER_EMAIL` 改成 `@t.local` 或带 `1e-` 前缀** ——
那会让这批数据在下一轮基线里消失，而症状是"视觉腿又变 0 点了"，
看起来像 colSmol 又坏了（§5.93/§5.94 那一族）。

## 用法

```bash
uv run python deploy/seed_photos.py            # 造 + 导入 + 等就绪
uv run python deploy/seed_photos.py --check    # 只体检，不改任何东西
```

**幂等**：库已存在且有就绪文档时不重复导入（重复导入只会得到 `same_content`
跳过，白等一轮视觉编码）。

⚠️ **耗时**：每张照片的视觉编码 ~15s（colSmol fp32，与分辨率无关）⇒
8 张约 2 分钟。这是**唯一**的成本，也是当初不放进 e2e 的原因（§5.146）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 🔑 `python deploy/seed_photos.py` 的 `sys.path[0]` 是 `deploy/`，
    #   于是 `from services...` 会 ModuleNotFoundError ——
    #   而症状是"这个脚本自己崩了"，与被测系统毫无关系（与 monitor.py 同款）。
    sys.path.insert(0, str(ROOT))

#: 🔑 刻意**不**用 `@t.local`、也**不**带 `1e-`/`1f-`/`1g-` 前缀 —— 见模块 docstring。
# 🔑 账号从**配置**读（`services/common/config.py` 的 `keeper_email` / `keeper_password`）：
#   原来写死在代码里（真实邮箱 + 口令），一公开就等于把开发实例的凭据发出去。
#   ⚠️ 密码留空 ⇒ 本脚本**拒绝运行**（宁可报错，也不要一个默认口令进公开仓库）。
from services.common.config import get_settings as _get_settings

_CFG = _get_settings()
KEEPER_EMAIL = _CFG.keeper_email
KEEPER_PASSWORD = _CFG.keeper_password
KEEPER_DISPLAY = "照片库主人"
N_PHOTOS = 8


def _qdrant_count(coll: str) -> int:
    """Qdrant 里某个 collection 的点数（带分页 —— 忘了翻页会少算）。

    ⚠️ 这里**不用** `common.qdrant.get_client()`：那个客户端是给 API/worker 用的
    单例，而这个脚本要能在**不重启任何东西**的情况下单独跑。用完即弃的 httpx
    更合适（也避免把 API 进程的单例状态搅乱）。
    """
    import httpx

    from services.common.config import get_settings

    total = 0
    offset = None
    with httpx.Client(base_url=get_settings().qdrant_url, trust_env=False,
                      timeout=30.0) as c:
        while True:
            body: dict = {"limit": 1000, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = c.post(f"/collections/{coll}/points/scroll", json=body)
            r.raise_for_status()
            data = r.json().get("result") or {}
            total += len(data.get("points") or [])
            offset = data.get("next_page_offset")
            if offset is None:
                return total


def check() -> int:
    """体检：只读。回答"视觉腿现在有没有活数据"。"""
    from sqlalchemy import select

    from services.common.consistency import check_drift, current_kb_ids
    from services.common.db import sync_session_scope
    from services.common.models import Document, KnowledgeBase, User

    with sync_session_scope() as s:
        uid = s.execute(select(User.id).where(User.email == KEEPER_EMAIL)).scalar_one_or_none()
        print(f"账号 {KEEPER_EMAIL}: {'存在 #' + str(uid) if uid else '不存在'}")
        if uid is None:
            return 1
        # ⚠️ 别去 `knowledge_bases.owner_id` 查他的库 —— **那一列不存在**
        #   （库归工作区，`created_by` 才是建库的人）。查不存在的列会抛
        #   ProgrammingError，而症状是"体检脚本自己崩了"。
        kb_id = s.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.name == "keeper 照片",
                                           KnowledgeBase.deleted_at.is_(None))
        ).scalar_one_or_none()
        print(f"  keeper 照片库：{'#' + str(kb_id) if kb_id else '不存在'}")
        rows = s.execute(
            select(Document.status, Document.scan_pages).where(
                Document.kb_id == kb_id
            )
        ).all() if kb_id else []
    print(f"  照片库里的文档 {len(rows)} 份："
          f"{sorted({r[0] for r in rows}) or '（无）'}")
    print(f"  其中 scan_pages 非空：{sum(1 for r in rows if r[1])}")

    with sync_session_scope() as s:
        drift = check_drift(s, current_kb_ids(s))
    vis = drift["kb_visual"]
    print(f"\nkb_visual：期望 {vis['expected']} 点 · 实有 {vis['actual']} 点 "
          f"· 缺 {vis['missing']} / 多 {vis['dangling']}")
    ok = vis["actual"] > 0 and vis["missing"] == 0
    print("⇒ " + ("视觉腿有活数据且一致 ✅" if ok else
                  "视觉腿**没有**活数据（缺 %d）—— 跑 seed_photos.py 造一批" % vis["missing"]))
    return 0 if ok else 1


def seed() -> int:
    """造数据 → 导入 → 等就绪 → 体检。"""
    import httpx

    from sqlalchemy import select

    from eval._fixtures import make_photo_keeper_fixture
    from services.common.auth import hash_password
    from services.common.config import get_settings
    from services.common.db import sync_session_scope
    from services.common.models import (
        KnowledgeBase,
        User,
        Workspace,
        WorkspaceMember,
    )

    src = make_photo_keeper_fixture(N_PHOTOS)
    n_files = len(list(src.glob("*.jpg")))
    print(f"fixture：{src}（{n_files} 张）")
    if n_files == 0:
        print("❌ 一张都没造出来", file=sys.stderr)
        return 1

    api = os.environ.get("PKH_API_URL", "http://127.0.0.1:8010")
    # 🔑 **明文 Key 从环境变量拿，不从库里查** —— 库里只有哈希，取不回来。
    #   `.env` 里有 `PKH_KEEPER_KEY`（D152 建库时写下的，只此一次）。
    #   ⚠️ 账号已存在但环境变量里没有 Key ⇒ **明确报错退出**，不要去建第二把：
    #   第二把 Key 只会让"我手上有几把能用的 Key"变成不确定的事，
    #   而真正要解决的问题是"当初那把没记下来"（办法见报错文案）。
    key = os.environ.get("PKH_KEEPER_KEY", "").strip()
    if not key:
        print("❌ 环境变量里没有 `PKH_KEEPER_KEY`。\n"
              "   它是建这个账号时明文给出的 API Key（只此一次，库里只有哈希）。\n"
              "   找回办法：`grep PKH_KEEPER_KEY .env`；若确实丢了，删掉这个账号\n"
              "   （`DELETE FROM users WHERE email='<KEEPER_EMAIL>'`）再跑一次本脚本。",
              file=sys.stderr)
        return 1
    print(f"用环境变量里的 Key（{key[:14]}…）")

    # ---- 账号（幂等：已存在就复用，不重复建）----
    with sync_session_scope() as s:
        user = s.execute(select(User).where(User.email == KEEPER_EMAIL)).scalar_one_or_none()
        if user is None:
            user = User(email=KEEPER_EMAIL, password_hash=hash_password(KEEPER_PASSWORD),
                        display_name=KEEPER_DISPLAY)
            s.add(user)
            s.flush()
            print(f"建账号 #{user.id}")
        else:
            print(f"账号已存在 #{user.id}（复用，不新建 —— 重建会让旧库变成孤儿）")
        # ---- 工作区 + 照片库（幂等：按名找）----
        # ⚠️ `workspaces` **没有** `visibility` 列（可见性在 `knowledge_bases` 上）
        #   —— 照着"工作区也有可见性"的印象写会报
        #   `'visibility' is an invalid keyword argument for Workspace`，
        #   而报错完全指不到"我记错了表结构"。
        #   `workspaces` 只有 owner_id；成员身份在 `workspace_members` 里。
        ws = s.execute(select(Workspace).where(
            Workspace.name == "照片库", Workspace.deleted_at.is_(None))).scalar_one_or_none()
        if ws is None:
            ws = Workspace(name="照片库", description="视觉腿的活数据（D152）",
                           owner_id=user.id)
            s.add(ws)
            s.flush()
            # 🔑 owner 也要是**成员**：可见性判定走 `workspace_members`
            #   （`build_qb_scope_sql` / `visible_kb_clause`），
            #   只写 `owner_id` 而不插成员行 ⇒ 这个库谁都看不见（D8 踩过）。
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner",
                                  invited_by=user.id))
        kb = s.execute(select(KnowledgeBase).where(
            KnowledgeBase.workspace_id == ws.id,
            KnowledgeBase.name == "keeper 照片",
            KnowledgeBase.deleted_at.is_(None))).scalar_one_or_none()
        if kb is None:
            # ⚠️ `embed_model` 是 **NOT NULL**：建库时抄一份当时的全局设置
            #   （D22 的"只作记录"，检索路径不读它，但列不允许空）。
            #   漏了它会在 INSERT 时炸 `NotNullViolation`，而报错只提到
            #   "knowledge_bases.embed_model" —— 不会告诉你"建库要抄这个"。
            kb = KnowledgeBase(workspace_id=ws.id, name="keeper 照片",
                               visibility="private", source_kind="photo",
                               embed_model=get_settings().text_embed_model,
                               created_by=user.id)
            s.add(kb)
            s.flush()
            print(f"建库 #{kb.id}")
        else:
            print(f"库已存在 #{kb.id}")
        kb_id = kb.id

    h = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=api, trust_env=False, timeout=120.0) as c:
        # 🔑 幂等的关键：**先看库里有没有就绪文档**，有就不导。
        #   重复导入同内容只会拿到 `same_content` 跳过（D114/D148），
        #   但仍然要跑一遍 job 派发与终态结算，白等 ~2 分钟的视觉编码。
        docs = c.get(f"/api/v1/kb/{kb_id}/documents?page_size=100", headers=h).json()
        ready = [d for d in (docs or []) if d.get("status") == "ready"]
        if ready:
            print(f"库里已有 {len(ready)} 份就绪文档 ⇒ 跳过导入"
                  f"（省掉 ~{len(ready) * 15}s 的视觉编码）")
        elif docs and all(d.get("status") in ("ready", "failed") for d in docs):
            # ⚠️ 全是 failed：**不能**当"已完成"跳过，也不能直接重导 ——
            #   重导同内容是 `same_content` 跳过（等于什么都没做）。
            #   ⇒ 明说该怎么处理，而不是让脚本"看起来成功"。
            print(f"⚠️ 库里 {len(docs)} 份文档**全部 failed** —— "
                  "重导同内容会被 `same_content` 跳过（等于什么都没做）。\n"
                  "   要重来请先把这些文档删掉（DELETE /api/v1/documents/{id}），"
                  "或直接换一批图（改 make_photo_keeper_fixture 的形状）。",
                  file=sys.stderr)
            return 1
        else:
            r = c.post(f"/api/v1/kb/{kb_id}/import/dir", headers=h,
                       json={"path": str(src), "recursive": True})
            print(f"入队 HTTP {r.status_code} · {str(r.json())[:90]}")
            if r.status_code != 202:
                return 1
            job = r.json()["job_id"]
            t0 = time.time()
            while time.time() - t0 < 900:
                time.sleep(3)
                j = c.get(f"/api/v1/jobs/{job}", headers=h).json()
                if j.get("status") in ("done", "failed"):
                    print(f"导入任务 {j['status']} · 耗时 {time.time() - t0:.0f}s "
                          f"· {str(j.get('error') or '')[:80]}")
                    break
            else:
                print("⏱ 导入超时（900s）", file=sys.stderr)
                return 1
            print("等视觉编码（每张 ~15s）…")
            t0 = time.time()
            while time.time() - t0 < 900:
                time.sleep(5)
                rows = c.get(f"/api/v1/kb/{kb_id}/documents?page_size=100",
                             headers=h).json()
                done = sum(1 for d in rows if d.get("status") in ("ready", "failed"))
                if done == len(rows) and rows:
                    print(f"全部终态：{done}/{len(rows)} · 耗时 {time.time() - t0:.0f}s")
                    break

    print(f"\nkb_dense={_qdrant_count('kb_dense')} · "
          f"kb_sparse={_qdrant_count('kb_sparse')} · "
          f"kb_visual={_qdrant_count('kb_visual')}")
    return check()


def _require_password() -> None:
    """密码没配就直接停 —— 一个"默认口令"进公开仓库比报错糟得多。"""
    if not KEEPER_PASSWORD:
        raise SystemExit(
            "❌ KEEPER_PASSWORD 没配。请在 .env 里设 KEEPER_EMAIL / KEEPER_PASSWORD 再跑本脚本。"
        )


def main() -> int:
    _require_password()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只体检，不改任何东西")
    args = ap.parse_args()
    return check() if args.check else seed()


if __name__ == "__main__":
    sys.exit(main())
