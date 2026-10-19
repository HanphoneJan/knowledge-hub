#!/usr/bin/env python
"""🔑 D193 一次性迁移：把三个真实集合迁成「物理集合 + 别名」（蓝绿重建的前提）。

## 什么时候跑

开了 `QDRANT_ALIASES=true` 之前**必须**先跑一次本脚本。之后
`POST /api/v1/admin/reindex?swap=true` 才能"建影子 → 验证 → 原子切别名 → 留旧回滚"。

## 它做什么（每个逻辑集合 `kb_dense` / `kb_sparse` / `kb_visual`）

| 现状 | 动作 |
|---|---|
| 已经是别名 | 跳过（**幂等**，可重复跑） |
| 是**真实集合** | 建 `kb_dense__boot<时间戳>` → **复制全部点**（含向量/payload）→ 删原真实集合 → 建别名指向它 |
| 都没有 | 建物理集合 + 别名（空库） |

```bash
uv run python deploy/bootstrap_index_aliases.py --yes      # 真迁移
uv run python deploy/bootstrap_index_aliases.py            # 只看计划（dry-run）
```

## ⚠️ 破坏性与窗口

- 第 2 行是**破坏性**的：复制完成后会**删掉**原真实集合，所以必须 `--yes`。
- 「删原集合 → 建别名」之间有毫秒级窗口，期间该腿读会 404 ⇒ 检索**降级成
  `skipped`**，不会 500（与视觉腿同款纪律）。
- 跑完记得把 `.env` 的 `QDRANT_ALIASES` 设为 `true` 并重启 API/worker。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.common.qdrant import (  # noqa: E402
    ALL_COLLECTIONS,
    copy_points,
    create_collection,
    delete_collection,
    list_aliases,
    physical_name,
    switch_alias,
)


def _existing() -> set[str]:
    from services.common.qdrant import get_client

    return {
        c["name"]
        for c in get_client().get("/collections").json()["result"]["collections"]
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="把 Qdrant 真实集合迁成物理集合 + 别名")
    ap.add_argument("--yes", action="store_true", help="确认执行（否则只打印计划）")
    args = ap.parse_args()

    aliases = list_aliases()
    existing = _existing()
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")

    plans: list[tuple[str, str, str]] = []   # (logical, action, physical)
    for logical in ALL_COLLECTIONS:
        if logical in aliases:
            plans.append((logical, "skip(已是别名)", aliases[logical]))
        elif logical in existing:
            plans.append((logical, "migrate(复制+删原集合+建别名)",
                          physical_name(logical, f"boot{ts}")))
        else:
            plans.append((logical, "create(空库+别名)",
                          physical_name(logical, f"boot{ts}")))

    print(f"{'逻辑名':<12} {'动作':<28} 物理集合")
    for logical, action, physical in plans:
        print(f"{logical:<12} {action:<28} {physical}")

    if not args.yes:
        print("\n（dry-run）加 `--yes` 真正执行。")
        return 0

    for logical, action, physical in plans:
        if action.startswith("skip"):
            print(f"→ {logical}：已迁移，跳过")
            continue
        if action.startswith("create"):
            print(f"→ {logical}：建空物理集合 + 别名")
            create_collection(physical)
            switch_alias(logical, physical)
            continue
        # migrate
        print(f"→ {logical}：复制 {logical} → {physical}（含向量与 payload）")
        create_collection(physical)
        n = copy_points(logical, physical)
        print(f"   复制 {n} 个点；删原集合 `{logical}` 并切别名")
        delete_collection(logical)
        switch_alias(logical, physical)
        print(f"   完成：{logical} → {physical}")

    print("\n✅ 迁移完成。请把 .env 的 QDRANT_ALIASES 设为 true 并重启 API/worker。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
