"""内容寻址存储（D35 双份存储的"原文"侧）。

布局（每个 blob 一个目录，原始文件叫 `original`）：
    data/blobs/{sha[:2]}/{sha}/original            原始文件
    data/blobs/{sha[:2]}/{sha}/preview.pdf         Office 转换预览（D34）
    data/blobs/{sha[:2]}/{sha}/thumbs/{chunk}.jpg  缩略图（D36）

⚠️ 这里踩过一个坑：最初设计是"原始文件直接叫 `{sha}`，preview 放在
`{sha}/preview.pdf`"，也就是 `{sha}` 既是文件又是目录 —— 写入时先建成文件，
生成预览时再 mkdir 就报 `NotADirectoryError`。文件系统不可能同时支持，
所以改为 `{sha}/` 目录 + 内部的 `original` 文件。

为什么按 sha 前两位分目录：单目录放几十万文件会让 ext4 目录项退化
（实测 10 万文件后 ls 明显变慢）。两位十六进制 = 256 个子目录，均匀。

为什么不用文件名：同一份文件被多个文档引用时（D28 覆盖式更新），
内容寻址天然去重，且改名不影响引用。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .config import get_settings

CHUNK_SIZE = 1024 * 1024  # 1MB 流式哈希


#: blob 目录里原始文件的名字。目录名已经是 sha256，文件本身用固定名。
ORIGINAL_NAME = "original"


def blob_dir(sha256: str) -> Path:
    """一个 blob 的目录。里面放原始文件 + 预览 + 缩略图。"""
    return get_settings().blob_path / sha256[:2] / sha256


def blob_path(sha256: str) -> Path:
    """原始文件路径。"""
    return blob_dir(sha256) / ORIGINAL_NAME


def preview_path(sha256: str) -> Path:
    """D34：Office 转出的 PDF。生成它需要 blob_dir 是目录，所以原始文件不能占用该路径。"""
    return blob_dir(sha256) / "preview.pdf"


def thumbs_dir(sha256: str) -> Path:
    return blob_dir(sha256) / "thumbs"


def thumb_path(sha256: str, chunk_id: int) -> Path:
    return thumbs_dir(sha256) / f"{chunk_id}.jpg"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def store_blob(src: Path) -> tuple[str, int]:
    """把文件存入内容寻址存储。

    返回 `(sha256, size_bytes)`。**已存在同内容则直接返回**，不重复写 ——
    这就是内容寻址的去重。
    """
    sha = sha256_file(src)
    dst = blob_path(sha)
    if dst.exists():
        return sha, dst.stat().st_size

    dst.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子改名，避免半截文件被当成有效 blob
    tmp = dst.with_name(ORIGINAL_NAME + ".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dst)
    return sha, dst.stat().st_size


def store_bytes(data: bytes) -> tuple[str, int]:
    """同上，但源是内存字节（聊天导入等场景）。"""
    sha = hashlib.sha256(data).hexdigest()
    dst = blob_path(sha)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(ORIGINAL_NAME + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
    return sha, len(data)


def exists(sha256: str) -> bool:
    return blob_path(sha256).exists()


def delete_blob(sha256: str) -> bool:
    """物理删除。**调用方必须先确认引用计数归零**（见 models.Blob.ref_count）。"""
    d = blob_dir(sha256)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def ensure_dirs() -> None:
    s = get_settings()
    for p in (s.blob_path, s.thumb_path):
        p.mkdir(parents=True, exist_ok=True)


def disk_usage() -> int:
    """blobs 目录总字节数，给 KB 统计用。"""
    root = get_settings().blob_path
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


# ============================================================
# blob 的 DB 登记（引用计数）
# ============================================================
#
# `documents.blob_sha` 有外键指向 `blobs.sha256`，而 `blobs.ref_count`
# 用来判断"这个文件还有没有文档引用"（D28 覆盖式更新后旧版本仍在回收站，
# 同一份文件可能被多个文档版本引用，引用计数归零才能物理删）。
#
# ⚠️ 曾漏掉这一整块：文件写进磁盘了、`documents` 行也插了，
# 但 `blobs` 表从没写过 → 外键报错 `documents_blob_sha_fkey`。
# 教训：**有外键就要想清楚谁负责写被引用方**，别只写引用方。


def register_blob(session, sha256: str, size_bytes: int) -> None:
    """登记一个 blob 并把引用计数 +1。**同步 session**（worker 用）。"""
    import datetime as dt

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from .models import Blob

    session.execute(
        _blob_upsert_stmt(sha256, size_bytes)
        .on_conflict_do_update(
            index_elements=[Blob.sha256],
            set_={"ref_count": Blob.ref_count + 1},
        )
    )


async def register_blob_async(session, sha256: str, size_bytes: int) -> None:
    """登记 blob。**异步 session**（API 用）。

    ⚠️ 必须有独立的 async 版本：在 `AsyncSession` 上调同步的 `session.execute()`
    **不会报错**，只会返回一个**从未被 await 的 coroutine**，
    INSERT 根本没发出去 → 后面插 `documents` 行时外键报错
    `documents_blob_sha_fkey`，而日志里看不到任何提示（只有一条
    `coroutine ... was never awaited` 的 RuntimeWarning）。
    这个坑很隐蔽：同步 session 上一切正常，只有异步路径会挂。
    """
    from .models import Blob

    await session.execute(
        _blob_upsert_stmt(sha256, size_bytes)
        .on_conflict_do_update(
            index_elements=[Blob.sha256],
            set_={"ref_count": Blob.ref_count + 1},
        )
    )


def _blob_upsert_stmt(sha256: str, size_bytes: int):
    import datetime as dt

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from .models import Blob

    return pg_insert(Blob).values(
        sha256=sha256,
        size_bytes=size_bytes,
        mime_type=None,
        storage_path=str(blob_path(sha256)),
        # ⚠️ 必须是 **1** 不是 0 —— 这次 `register_blob` 调用本身就是一次引用。
        # 写成 0 的后果（实测发现）：一个文档引用的 blob 计数是 0，
        # 一次 `release_blob` 减到 0（被 GREATEST 夹住）→ **物理删除文件**，
        # 而那个文档还在引用它 → 后续读取直接 FileNotFoundError。
        ref_count=1,
        created_at=dt.datetime.now(dt.UTC),
    )


def release_blob(session, sha256: str) -> bool:
    """引用计数 -1（同步）。归零时删除行并物理删除文件。"""
    from sqlalchemy import delete, select

    from .models import Blob

    row = session.execute(
        select(Blob).where(Blob.sha256 == sha256).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return False

    row.ref_count = max(0, row.ref_count - 1)
    if row.ref_count > 0:
        return False

    session.execute(delete(Blob).where(Blob.sha256 == sha256))
    session.flush()
    return delete_blob(sha256)


async def release_blob_async(session, sha256: str) -> bool:
    """引用计数 -1（异步）。见 `register_blob_async` 关于 async 的坑。"""
    from sqlalchemy import delete, select

    from .models import Blob

    row = (
        await session.execute(
            select(Blob).where(Blob.sha256 == sha256).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    row.ref_count = max(0, row.ref_count - 1)
    if row.ref_count > 0:
        return False

    await session.execute(delete(Blob).where(Blob.sha256 == sha256))
    return delete_blob(sha256)
