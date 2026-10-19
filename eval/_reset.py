"""eval 脚本共用的测试数据清理。

## 为什么不能只 `DELETE FROM users`

⚠️ **这是 1d 实现时实测踩到的坑**（`PROGRESS.md` §5.29）：

`DELETE FROM users` 会级联删掉 documents / chunks（外键 CASCADE），
但 **不会**回退 BM25 的 `terms.df` 与 `corpus_stats.n_docs` ——
那两个数只在两个地方被维护：

1. `terms.register_chunks`（入库时 +1）
2. `terms.unregister_chunks`（**worker cleanup，30 天后**才跑）

所以每跑一轮 eval 脚本，`n_docs` 就虚高 74+（一份 PDF 的 chunk 数），
而真实 chunk 数不变。实测跑到第 4 轮时：

```
n_docs = 1332   真实 chunk 数 = 222     ← 6 倍
```

后果不是报错，是**检索质量静默下降**：`idf = log(1 + (N - df + .5)/(df + .5))`
里 N 虚高 6 倍 → 所有词的 idf 被压平 → 罕见词不再罕见 →
短 chunk 里单个高频字（`栏`）的权重盖过长 chunk 的多词共现。
实测表现：sparse 腿的 top1 从答案段落变成一个表格标题。

## 所以 `reset()` 必须三步走

1. 删用户（级联清业务数据）
2. 删**软删但还没到期**的文档 —— 它们的 chunk 也还在 counts 里
3. 重跑 `handle_reindex`（`KIND_REINDEX`）重建词表与稀疏向量

第 3 步同时修好稀疏向量本身 —— 因为文档侧权重是用当时的 idf 算的，
`n_docs` 变了就得重算，否则 Qdrant 里存的是**过期权重**。
"""

from __future__ import annotations

import os

# 🔴 离线加载模型（说明见 tests/conftest.py）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def psql(sql: str) -> str:
    """跑一条 SQL 并返回输出。"""
    out = subprocess.run(
        ["docker", "exec", "pkh-postgres", "psql", "-U", "pkh", "-d", "pkh", "-tAc", sql],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout or "").strip()


def drop_users(emails: list[str]) -> None:
    """删指定邮箱的用户（级联清其 ws/kb/doc/chunk）。

    🔴 **不删超级管理员**（D173/D176）：eval 与测试用 `%@t.local` 收集"测试账号"，
    而超管账号很可能也落在那个域里（实测：`admin@t.local` 被整套 eval 清掉了一次）。
    superadmin 是**运维身份**，不是测试数据 —— 一行 `AND role <> 'superadmin'`
    就把它排除在清场之外。
    """
    if not emails:
        return
    quoted = ", ".join(f"'{e}'" for e in emails)
    psql(
        f"DELETE FROM users WHERE email IN ({quoted}) "
        "AND role <> 'superadmin'"
    )


def rebuild_bm25() -> None:
    """重建 BM25 词表 + 稀疏向量（等价于跑一个 `KIND_REINDEX` 任务）。

    在**进程内**直接调 `handle_reindex` 而不是入队：
    eval 脚本要的是"立刻修好"，不等 worker 轮询。
    """
    from services.common.db import sync_session_scope
    from services.worker.handlers import handle_reindex

    with sync_session_scope() as s:
        handle_reindex(s, doc_id=None, payload={}, progress=lambda _x: None)
        s.commit()


def reset_users_and_bm25(emails: list[str]) -> dict[str, int | str]:
    """标准清理：删用户 → 重建 BM25。返回清理后的统计量（供脚本打印）。"""
    drop_users(emails)
    rebuild_bm25()
    return {
        "n_docs": psql("SELECT n_docs FROM corpus_stats"),
        # 🔑 口径 = **全部 chunk 行**（含软删未 purge 的），与 `handle_reindex`、
        #   `deploy/monitor.py`、单测四处一致（D115）。
        #   ⚠️ 别改成"活文档 chunk 数"：那是**向量侧**口径
        #   （reindex 只重建活文档的点），回收站里有一份文档就差一截
        #   → 这条断言恒红（实测差 8 = 1c §16 软删那份 docx 的 chunk）。
        "real_chunks": psql("SELECT count(*) FROM chunks"),
        # 另存活文档数（要报"回收站里还剩多少"时用，不参与一致性判据）
        "live_chunks": psql(
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id=c.doc_id "
            "WHERE d.is_current IS TRUE AND d.deleted_at IS NULL"
        ),
        "terms": psql("SELECT count(*) FROM terms"),
    }