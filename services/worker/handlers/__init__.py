"""任务处理器。

每个 kind 一个函数，签名统一：

```python
def handle(session, *, doc_id, payload, progress) -> None
```

`session` 由 worker 以独立事务开好；处理器里 `session.flush()` 即可看到自己的改动，
提交由 worker 负责。

## 分工（D35/D50）

| kind | 谁跑 | 成本 | 说明 |
|---|---|---|---|
| `ingest` | worker | ~50ms/页 | 解析→分块→嵌入→写 Qdrant 文本腿。**一次性做完** |
| `embed_visual` | worker | **15s/图** | colSmol 编码，**必须独立任务**否则 API 会卡 |
| `thumbnail` | worker | ~30ms/页 | D36 缩略图 |
| `cleanup` | worker | 变量 | D26/D44 过期数据清理 |
| `reindex` | worker | 分钟级 | BM25 词表漂移修正 |

⚠️ 老的 `jobs.py` 里 `KIND_PARSE` / `KIND_CHUNK` / `KIND_EMBED` 拆成了三步，
但实测发现没必要：**文本腿全部加起来不到 1 秒**，拆开只是多三次事务。
合并成 `ingest` 一个任务，进度更好回传（D46）。
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from ...common.jobs import (
    KIND_CLEANUP,
    KIND_EMBED_VISUAL,
    KIND_IMPORT_CHAT,
    KIND_IMPORT_DIR,
    KIND_INGEST,
    KIND_PDF_PREVIEW,
    KIND_REINDEX,
    KIND_THUMBNAIL,
    enqueue,
)
from ...common.models import (
    ChatDayStat,
    Chunk,
    Document,
    KnowledgeBase,
)
from ...ingest.pipeline import IngestError, ingest_document, remove_from_indexes
from ...ingest.versions import find_current, next_version_id, supersede
from ...ingest.preview import THUMB_WIDTH, render_page
from ...common.storage import (
    blob_path,
    preview_path,
    register_blob,
    store_bytes,
    thumb_path,
    thumbs_dir,
)
from ...ingest.parsers import guess_mime

logger = logging.getLogger(__name__)

Handler = Callable[..., None]


def handle_ingest(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """文档摄取主流程。

    ⚠️ 进度回调接的是**管线内部的分批上报**，不是只在结束时喊一嗓子。
    1c 把 parse/chunk/embed 合并成一个 kind 之后，`progress` 一度只在
    `progress(1.0)` 出现过一次 —— 于是 `/jobs` 端点接通了，前端进度条却
    从 0% 直接跳到 100%（与"没有进度条"的可用性一样，见 PROGRESS §5.38）。
    """
    if doc_id is None:
        raise IngestError("ingest 任务缺 doc_id", retryable=False)
    result = ingest_document(session, doc_id, progress=progress)
    progress(1.0)
    logger.info(
        "ingest doc=%s → %d chunk, %d 扫描页, %dms",
        doc_id, result.chunk_count, len(result.scan_pages), result.elapsed_ms,
    )


#: 视觉页渲染的**长边像素上限**。🔑 不是画质参数，是**安全阀**。
#:
#: colSmol 内部恒定把长边重采样到 2048px 再切 512×512 图块
#: （实测 400×300 与 3000×4000 都是 ~862 token），
#: 所以渲染出比这更大的图**一个 token 都多不出来**，只是白占内存。
#:
#: ⚠️ 为什么必须有它（实测踩到）：第一版固定 150dpi、**不封顶**，
#: 于是页面尺寸大的 PDF 会直接把 pymupdf 顶死：
#: `FzErrorLimit: code=5: Overly large image`。实测门槛：
#: | 页面(pt) | 150dpi 像素 | 光栅化内存 | 结果 |
#: |---|---|---|---|
#: | 1075×1520（A4） | 2240×3167 | 0.02GB | ✅ |
#: | 4667×6599 | 9723×13748 | 0.40GB | ✅（但已经浪费 200 倍） |
#: | 9723×13748 | 20256×28642 | 1.74GB | ✗ **Overly large image** |
#: | 20000×28280 | 41667×58917 | 7.36GB | ✗ 同一错误 |
#: 而"工程图纸 / 海报 / CAD 导出"这种 135×190 英寸的页面在真实世界里很常见 ——
#: 它一进来，这一页的视觉任务就永久失败，纯扫描件文档永远停在 `pending`。
#:
#: 📌 取 2048 而不是留余量：与 colSmol 的内部目标**一致**，
#: 所以 token 数与之前实测的 ~871 完全相同（不是"降质"）。
VISUAL_MAX_LONG_SIDE_PX = 2048

#: 常规渲染 DPI。⚠️ 它的作用**只是**"小页面别渲太小"，
#: 真正的上限是上面那个 `VISUAL_MAX_LONG_SIDE_PX`。
VISUAL_RENDER_DPI = 150


def _visual_render_scale(width_pt: float, height_pt: float) -> float:
    """渲染缩放系数 = 150dpi 与"长边封顶"两者取小。

    🔑 单独抽出来是为了**能不加载模型就单测**（§5.92：
    "看起来对"的代码必须有判据）。返回 1.0 表示 72dpi。

    ⚠️ 退化保护：页面尺寸为 0（畸形 PDF）时返回 0 而不是除零 ——
    0 会让 pymupdf 报错，那比返回一个假图更容易定位。
    """
    longest = max(width_pt, height_pt)
    if longest <= 0:
        return 0.0
    by_dpi = VISUAL_RENDER_DPI / 72
    by_cap = VISUAL_MAX_LONG_SIDE_PX / longest
    return min(by_dpi, by_cap)


def _visual_page_image(path: Path, file_name: str, page: int):
    """取"要编码的那一张图"。

    🔑 **按 `documents.file_name` 分派，不看 blob 路径**（CONVENTIONS §0.8）：
    内容寻址存储的 blob 是 `{sha}/original`，没有扩展名。

    | 文档类型 | 走哪条 | 为什么 |
    |---|---|---|
    | 图片（jpg/png/webp/…） | `PIL.Image.open` | 照片本来就是一张图，**不需要"渲染"** |
    | PDF（含扫描件） | pymupdf 渲染，**长边封顶 2048px** | PDF 没有位图，得渲染 |

    ⚠️ 第一版**照片也走 pymupdf**（它碰巧能开 jpg：把像素当点，再按
    150/72 放大），于是 160×120 的小照片被无意义地重采样一遍，
    而 webp/tiff 这类 pymupdf 未必认 —— `IMAGE_SUFFIXES` 里就有它们。
    照片直接 PIL 读：格式全覆盖，也省掉一次重编码。

    ⚠️ 第二版 PDF 固定 150dpi **不封顶**，大页面直接被 pymupdf 顶死
    （`Overly large image`），纯扫描件文档永远停在 `pending`。
    现在缩放走 `_visual_render_scale`（见 `VISUAL_MAX_LONG_SIDE_PX` 的实测表）。
    """
    from PIL import Image

    from ...ingest.parsers import IMAGE_SUFFIXES

    if Path(file_name).suffix.lower() in IMAGE_SUFFIXES:
        with Image.open(path) as im:
            return im.convert("RGB")

    import pymupdf

    pdf = pymupdf.open(path)
    try:
        if page < 1 or page > pdf.page_count:
            raise IngestError(
                f"第 {page} 页不存在（这份文档共 {pdf.page_count} 页）",
                retryable=False,
                code="PAGE_OUT_OF_RANGE",
            )
        rect = pdf[page - 1].rect
        scale = _visual_render_scale(rect.width, rect.height)
        pix = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        import io

        return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    finally:
        pdf.close()


def handle_visual(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """colSmol 编码一页（D37/D60）。

    ⚠️ **成本极高**：实测 **15s/图**（13 个 512×512 图块，**与分辨率无关**；
    float32。若跟着 checkpoint 默认的 bf16 跑是 68s —— 本机 CPU 没有
    avx512_bf16，bf16 矩阵乘是软件模拟，见 `retrieval/pool.py`）。所以：
    - 每页一个任务（`ingest` 不等它，否则进度条会卡在 90% 不动）
    - D37 把扫描件限到 50 页（≈12 分钟）
    - `VDR_ENABLED=false` 可以整体关掉（D124）

    🔑 这一页是**页级点**（`point_id_for_page`，chunk_index 传负数），
    与 `kb_dense`/`kb_sparse` 的 chunk 级点 ID 空间独立。

    ⚠️ 这个 handler **只管编码一页**，文档状态由
    `services/worker/doc_state.py` 在任务终态之后统一判定（成功与失败都判）。
    """
    if doc_id is None:
        raise IngestError("embed_visual 任务缺 doc_id", retryable=False)

    doc = session.get(Document, doc_id)
    if doc is None:
        raise IngestError(f"文档 {doc_id} 不存在", retryable=False)

    page = int(payload.get("page", 1))
    path = blob_path(doc.blob_sha)
    if not path.exists():
        raise IngestError(f"blob 缺失: {path}", retryable=False)

    img = _visual_page_image(path, doc.file_name, page)

    # 🔑 D178：写侧可以走远端常驻视觉服务 —— 于是 worker 不加载 colSmol（省 2.65GB）。
    #   ⚠️ 默认 `false`（本地编码，保留既有行为）；SMB 多 worker 部署设 true。
    from ...common.config import get_settings

    if get_settings().visual_write_via_service:
        import io

        from ...retrieval.pool import encode_image_remote_sync

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        # ⚠️ 失败抛 VisualServiceError（RuntimeError 子类）⇒ 任务重试，
        #   绝不吞成空向量（空向量会"成功"却永远召回不到）。
        vector = encode_image_remote_sync(buf.getvalue())
    else:
        from ...retrieval.pool import encode_images

        # colSmol 必须 batch=1（CPU 上批处理无收益，实测反而慢 7%）
        vector = encode_images([img])[0]

    from ...common.qdrant import (
        COLL_VISUAL,
        point_id_for_page,
        sparse_vector as _sv,  # noqa: F401 — 保持导入风格一致
        upsert_points,
    )

    # ⚠️ 视觉点的 ID 用 `point_id_for_page`（chunk_index 传负数），
    # 与 chunk 级 ID 不冲突。见 qdrant.py 的说明。
    upsert_points(
        COLL_VISUAL,
        [{
            "id": point_id_for_page(doc.kb_id, doc.id, page),
            "vector": {"text": vector},
            "payload": _visual_payload(session, doc, page),
        }],
    )
    # D191：记下"视觉腿是用哪个模型建的"——换 colSmol 同样要被校验。
    from ...common.index_models import record_current_visual_model

    record_current_visual_model(session)
    progress(1.0)

    # 🔑 D127：**这里不再改 `documents.status`**。
    #   纯扫描件/照片的文档状态是 `pending`（`ingest` 派发了视觉任务），
    #   而"该不该撤销这个 pending"要看**两件事**：视觉任务是不是都到终态了、
    #   以及 Qdrant 里的页级点够不够。判据在 `services/worker/doc_state.py`，
    #   由 worker 在**每个**视觉任务到达终态时调用（成功与失败两条路径都调）。
    #
    #   ⚠️ 为什么不能留在这里：第一版就是在这收尾时判断"点数够不够"，
    #   而**全失败时这个 handler 根本走不到收尾** —— 于是文档永远停在
    #   `pending`，用户看到的是"一直在处理中"而不是"处理失败了"。
    #   同一个症状的两半（成功不转 / 失败不转）必须由**同一个**判定覆盖。


def _visual_payload(session, doc, page: int) -> dict[str, Any]:  # noqa: ANN001
    """页级点的 payload。

    🔑 **租户字段必须与文本腿逐字一致**（`ingest/pipeline.py::_index_text_legs`
    用的是 `kb.workspace_id` / `kb.visibility` / `kb.source_kind` / `kb.created_by`）。

    ⚠️ 这里原本调的是**两个不存在的函数**（`_workspace_of` / `_source_kind`），
    并且把 `visibility` 写死成 `"workspace"`、`owner_id` 写死成 `None`。
    那段代码**从来没跑过**：第一个缺陷（processor 少传 `text=`）在它之前
    就抛错了，所以没人发现后面还有第二个坑。
    今天 `build_qdrant_filter` 只按 `kb_id` 过滤（租户判定在 PG 侧就折叠成
    `readable_kb_ids` 了），所以写错字段**暂时**不构成越权 ——
    但 payload 撒谎会让 §2.4 建的索引、按 payload 做的排查、
    以及将来任何"按 visibility 过滤"的改动全部错位。照文本腿抄，别自己编。
    """
    kb = session.get(KnowledgeBase, doc.kb_id)
    if kb is None:
        raise IngestError(f"知识库 {doc.kb_id} 不存在", retryable=False)
    return {
        "kb_id": kb.id,
        "doc_id": doc.id,
        "workspace_id": kb.workspace_id,
        "source_kind": kb.source_kind,
        "visibility": kb.visibility,
        "owner_id": kb.created_by,
        # ⚠️ `chunk_index` 为负 = 这是页级点（`kb_visual`）。
        #   `/admin/consistency` 靠这个符号把三条腿分开（D124）。
        "chunk_index": -page,
        "kind": "page",
        "page": page,
        "file_name": doc.file_name,
        "mime": doc.mime_type,
        "text_norm": f"第 {page} 页",
    }


def handle_thumbnail(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """生成页面缩略图（D36：入库时预生成，宽 400px）。

    为什么预生成而不是按需渲染：colSmol 之外最贵的操作是渲染，
    而缩略图在列表页一次要显示几十张。按需渲染会让列表页卡死。
    """
    if doc_id is None:
        raise IngestError("thumbnail 任务缺 doc_id", retryable=False)
    doc = session.get(Document, doc_id)
    if doc is None:
        raise IngestError(f"文档 {doc_id} 不存在", retryable=False)

    path = blob_path(doc.blob_sha)
    if not path.exists():
        return

    pages = int(payload.get("page_count") or doc.page_count or 1)
    thumbs_dir(doc.blob_sha).mkdir(parents=True, exist_ok=True)

    for p in range(1, min(pages, 20) + 1):  # 最多前 20 页
        try:
            data = render_page(path, p, width=THUMB_WIDTH, as_jpeg=True)
        except IndexError:
            break
        thumb_path(doc.blob_sha, p).write_bytes(data)
        progress(p / min(pages, 20))
    progress(1.0)


def handle_cleanup(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """过期数据清理（D26 30 天回收站 + **D125 容器清理**）。

    ⚠️ **删除链不在这里** —— 在 `services/ingest/purge.py`，
    `DELETE /api/v1/trash`（手工清空回收站）走的是同一份代码。
    两条删除路径各自复制一遍六步，df 漏减就会重新长出来（PROGRESS §5.29）。

    这里只负责：选时间窗 → 调 purge → 记日志。
    """
    from ...ingest.purge import purge_containers, purge_documents

    now = dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=30)
    stats = purge_documents(session, older_than=cutoff, progress=progress)
    # 🔑 D125：**先文档、后容器**，顺序不能反 ——
    #   容器行一旦没了，底下文档的 df 就永远减不回去，
    #   而 df 虚高是**静默**的检索质量下降（PROGRESS §5.29）。
    cstats = purge_containers(session, older_than=cutoff)
    logger.info(
        "清理过期文档 %s 份（%s chunk，释放 %s 字节）%s；容器：库 %s 个 / 工作区 %s 个",
        stats.docs, stats.chunks, stats.bytes_freed,
        f"，Qdrant 清失败 {len(stats.qdrant_failed)} 份" if stats.qdrant_failed else "",
        cstats.kbs, cstats.workspaces,
    )


class _DocRef:
    """重建时传给 `_index_text_legs` 的文档替身。

    🔑 为什么不直接用 `Document` 行：那会让 `_index_text_legs` 去访问
    **惰性关系** `doc.kb`，而 reindex 要跑几分钟 ——
    期间那个库被删掉的话 `d.kb` 就是 `None`（实测崩在这里，D124）。
    重建循环显式查好 `kb` 再传进来，就完全没有那个时间窗。

    ⚠️ 字段要**与 `Document` 一致**（`_index_text_legs` 会读 `doc.id` /
    `file_name` / `mime_type`）—— 少一个就是运行到那行才炸。
    🔑 下面那条测试用 `inspect.signature` 盯住这个集合：
    `_index_text_legs` 将来多读一个字段，这条会立刻红。
    """

    __slots__ = ("file_name", "id", "mime_type", "updated_at")

    def __init__(
        self, doc_id: int, file_name: str, mime_type: str | None,
        updated_at: Any = None,
    ) -> None:
        self.id = doc_id
        self.file_name = file_name
        self.mime_type = mime_type
        # D199：文档更新时间 —— 重建也要写进 payload，否则重建过的文档在
        # 时间过滤下会被排除（payload 少字段 = 重摄取欠账）。
        self.updated_at = updated_at


def _swap_rebuild(
    session,
    live_rows,
    chunks_of,
    progress,
    *,
    dense_logical: str | None = None,
    sparse_logical: str | None = None,
) -> dict[str, Any]:
    """🔑 D193 蓝绿重建：把两条文本腿重建到**影子集合**，验证后原子切别名。

    与"按库先删后建"（D179，默认路径）的区别：**全程旧索引可读**。
    新索引验证通过之前，读一直走旧集合；通过后一次 `/collections/aliases`
    请求原子切换；旧物理集合**保留**用于回滚。

    | 步骤 | 动作 |
    |---|---|
    | ① 建影子 | `create_collection(kb_dense__<gen>)`（含 payload 索引）|
    | ② 灌数据 | 逐份活文档 `_index_text_legs(dense_collection=…, sparse_collection=…)` |
    | ③ 验证 | 影子点数 == PG 期望点数（分腿）；不符则**放弃影子、保留旧索引** |
    | ④ 切换 | `switch_alias(kb_dense→影子)`、`switch_alias(kb_sparse→影子)` |

    @param dense_logical / sparse_logical 逻辑腿名，默认取 `COLL_DENSE` / `COLL_SPARSE`。
        **抽成参数是为了可测**：隔离测试传自己的逻辑名（`kb_dense__khtest`），
        从而不必 monkeypatch 模块常量（那样会把"按种类识别稀疏"一起改坏）。

    ⚠️ **视觉腿不重建也不动**（D124）：它是页级点、要重读原件 + colSmol 前向，
    那是"重新摄取"。别名模式下它的别名由 bootstrap 建好，这里不碰。
    """
    import datetime as _dt

    from ...common.consistency import pg_point_ids, qdrant_point_ids
    from ...common.qdrant import (
        COLL_DENSE,
        COLL_SPARSE,
        create_collection,
        delete_collection,
        physical_name,
        resolve_target,
        switch_alias,
    )
    from ...ingest import chunker
    from ...ingest.pipeline import _index_text_legs

    dense_logical = dense_logical or COLL_DENSE
    sparse_logical = sparse_logical or COLL_SPARSE
    legs = (dense_logical, sparse_logical)

    gen = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    shadow = {leg: physical_name(leg, gen) for leg in legs}
    old = {leg: resolve_target(leg) for leg in legs}

    def _drop_shadow() -> None:
        for coll in shadow.values():
            try:
                delete_collection(coll)
            except Exception:  # noqa: BLE001 — 清影子失败不该盖过真正的原因
                logger.warning("放弃影子集合 %s 失败", coll, exc_info=True)

    # ① 建影子集合（空，带 payload 索引）
    for coll in shadow.values():
        create_collection(coll)

    # ②③：建/验**任何一步**失败都要放弃影子、保留旧索引（读不受影响）。
    #   ⚠️ 清理必须包住**整个建过程**，不能只在"验证不过"那一支做 ——
    #      实跑时正是在建过程里抛异常（ORM 行没转 `chunker.Chunk`），
    #      三次重试各留下一套影子集合（孤儿）。
    try:
        # ② 灌数据：只处理"有 chunk 的活文档"
        todo = [r for r in live_rows if chunks_of.get(r[0])]
        total = max(1, len(todo))
        kbs: dict[int, Any] = {}
        for i, (doc_id, kb_id, name, mime, updated_at) in enumerate(todo, start=1):
            kb = kbs.get(kb_id)
            if kb is None:
                kb = session.get(KnowledgeBase, kb_id)
                if kb is None:                  # 重建期间库被删（D124 的教训）
                    continue
                kbs[kb_id] = kb
            # ⚠️ `chunks_of` 里是 **ORM `Chunk` 行**，必须过 `chunker.from_rows`
            #   转成 `chunker.Chunk`（`_index_text_legs` 读的是 `.text`）——
            #   与按库重建**同一份**转换。实跑 swap 时正是漏了它而 AttributeError。
            # ⚠️ `index_terms=False`：词表上面已为全体登记过，重复登记 df 翻倍。
            # 🔑 `encoding` 取该库切分策略：`late` 库重建出的向量才与入库时一致。
            _index_text_legs(
                session, _DocRef(doc_id, name, mime, updated_at), kb,
                chunker.from_rows(chunks_of[doc_id]),
                report=None,
                index_terms=False,
                encoding=getattr(kb, "chunk_strategy", "recursive") or "recursive",
                dense_collection=shadow[dense_logical],
                sparse_collection=shadow[sparse_logical],
            )
            progress(i / total)

        # ③ 验证：影子点数必须等于 PG 期望（分腿）
        live_kb_ids = sorted({r[1] for r in live_rows})
        expected = pg_point_ids(session, live_kb_ids)
        for leg in legs:
            got = qdrant_point_ids([shadow[leg]], live_kb_ids)
            miss, dang = expected - got, got - expected
            if miss or dang:
                raise IngestError(
                    f"蓝绿重建未通过校验：{leg} 缺 {len(miss)}/多 {len(dang)}；"
                    "已放弃影子集合、**保留旧索引**（读不受影响）",
                    retryable=False,
                )
    except Exception:
        _drop_shadow()
        raise

    # ④ 原子切换（两条腿各一次；旧集合保留）
    for leg in legs:
        switch_alias(leg, shadow[leg])
    return {"gen": gen, "new": shadow, "old": old}


def handle_reindex(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """重建索引：全局 BM25 词表 + 目标库的 dense/sparse 两条腿。

    @param payload `kb_id` 给空 = 重建**全部**知识库。

    ⚠️ 顺序是刻意的：**先登记全体活 chunk 的词表，再重建向量**。
    因为 `terms.df` / `corpus_stats` 是全局量（不带 kb_id），
    只重建一个库却只登记它的 chunk 会把其它库的 idf 一起弄坏，
    表现为"别的库检索质量静默变差"—— 不报错，最难查的一类问题。
    向量重建（要跑 embedding）仍然只限目标库：贵 100 倍，不该为别人白跑。

    ⚠️ **视觉腿（`kb_visual`）既不重建、也不删**（D124 实测踩过）：
    页级向量要重读源文件 + colSmol 前向（实测 15s/图），
    那是"重新摄取"不是"重建索引"。

    🔑 **"不重建"必须连"不删"一起** —— 这是本轮真实踩的坑：
    第一版想当然地给删除循环也加上了 `kb_visual`（想着"三条腿要一致"），
    结果 reindex 把 25 个页级点全删了又建不回来 ——
    **比不删更糟**：不删只是漂移，删了是数据没了。

    ✅ 所以两条规则分开：
    | 动作 | 覆盖 | 为什么 |
    |---|---|---|
    | 按库先删后建 | dense + sparse | 删完一定会建回来 |
    | 孤儿清理 | **三条腿** | 库已不在 PG 里 → 那些点**永远**没人能重建，是真垃圾 |

    视觉腿的漂移要靠**重新摄取**修（`POST /documents/{id}/reparse`），
    而"该不该修"由 `deploy/monitor.py` 的「视觉腿对账」与
    `/admin/consistency` 的 `kb_visual` 腿报出来（D124）。
    """
    from ...common.qdrant import (
        COLL_DENSE,
        COLL_SPARSE,
        COLL_VISUAL,
        delete_by_filter,
    )
    from ...ingest import chunker
    from ...ingest.pipeline import _index_text_legs
    from ...retrieval import bm25
    from ...retrieval.terms import register_chunks, reset_terms

    kb_ids = [int(payload["kb_id"])] if payload.get("kb_id") else []

    # ---- ① 词表：全局从零重建（一次性清空 + 登记**全体未 purge 的 chunk**）----
    # 🔑 **口径 = 表里所有 chunk 行，含软删未 purge 的**（D115）。这与
    #   入库路径一致（入库时文档是活的 → 登记；软删**不清 df**，
    #   因为 D26 的 30 天反悔窗口内它还能被恢复 —— 立刻 unregister 的话，
    #   恢复后它的词就不在词表里，BM25 那条腿搜不到它，而 dense 那条还能）。
    #
    # ⚠️ 第一版这里按 `is_current` 过滤，于是**两条路径口径不同**：
    #   入库登记全部、reindex 只登记活的 → 软删有文档时
    #   `n_docs` 与 chunk 行数对不上（实测差 8 = 回收站里那份）。
    #   后果是**任何不变量都不成立**（monitor 的 df 检查恒红、
    #   测试只能靠"碰巧没有回收站内容"而绿）。
    #
    # ⚠️ 代价要说明白：软删文档的 token 会算进 N → idf 被压平一点，
    #   最多 30 天（D26 的窗口）。选它是因为另一条路（软删时 unregister）
    #   要在"恢复"时补登记，漏了就是**恢复后的文档搜不到** ——
    #   那比 idf 偏一点严重得多。
    reset_terms(session)
    session.flush()

    # 🔴 **`live_rows` 与 `live_kb_ids` 必须在同一条 SQL 里取**
    #   （D124 实测踩到）：原来是先查文档、后面用 `d.kb_id` 推 `live_kb_ids`，
    #   再在重建循环里访问**惰性关系** `d.kb`。而 reindex 可以跑很久
    #   （几万个 chunk 的库要几分钟），期间只要有人删了那个库 ——
    #   `d.kb` 就变成 `None`，`kb.id` 抛
    #   `AttributeError: 'NoneType' object has no attribute 'id'`，
    #   **整个重建任务失败**（实测：并行跑 eval + web e2e 时 e2e 清场
    #   删掉了 1d 的库，正好撞上）。
    #   一次查询 + 一个 `kb_id` 集合，从根上避免那个惰性加载。
    # ⚠️ `join(KnowledgeBase)` **只用来保证库行此刻存在**，不加
    #   `deleted_at IS NULL` 之类条件 —— 口径必须与 `admin.py::_pg_point_ids`
    #   （对账的 PG 侧）一致（§2.6.1 的同源精神）：
    #   那边只按 `Document` 判活，这边多一个条件就会让"库软删了但文档没软删"
    #   的那批 chunk 永远重建不出来，于是对账开始报假漂移。
    live_rows = session.execute(
        select(
            Document.id, Document.kb_id, Document.file_name, Document.mime_type,
            Document.updated_at,
        )
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.is_current.is_(True),
            Document.deleted_at.is_(None),
        )
    ).all()
    #: PG 里还活着的库（用来识别"只在 Qdrant 里存在"的孤儿库）
    live_kb_ids = {row[1] for row in live_rows}
    #: 重建期间被发现"库已被删"的 kb_id（要在日志里报出来，见 §5.71）
    skipped_kbs: set[int] = set()

    def _rows_of(d) -> list[Any]:
        return (
            session.execute(
                select(Chunk)
                .where(Chunk.doc_id == d.id)
                .order_by(Chunk.chunk_index)
            )
            .scalars()
            .all()
        )

    # 词表：全部 chunk（不分软删）
    all_docs = (
        session.execute(select(Document)).scalars().all()
    )
    chunks_of: dict[int, list[Any]] = {d.id: _rows_of(d) for d in all_docs}
    total_chunks = sum(len(v) for v in chunks_of.values())

    done = 0
    for d in all_docs:
        rows = chunks_of[d.id]
        if rows:
            register_chunks(session, [bm25.tokenize(r.content_norm) for r in rows])
            done += len(rows)
            progress(done / total_chunks if total_chunks else 1.0)

    # ---- ①b D193：蓝绿重建（swap）—— 建影子 → 验证 → 原子切别名 ----
    if payload.get("swap"):
        from ...common.config import get_settings

        if not get_settings().qdrant_aliases:
            raise IngestError(
                "swap 重建需要别名模式（QDRANT_ALIASES=true）；"
                "当前逻辑名是真实集合，无法切换别名",
                retryable=False,
            )
        result = _swap_rebuild(session, live_rows, chunks_of, progress)
        logger.info(
            "蓝绿重建完成：新 %s（旧集合保留用于回滚：%s）",
            result["new"], result["old"],
        )
        progress(1.0)
        return

    # ---- ② 向量：只重建目标库（先删后建，派生索引可重建 D7）----
    targets = kb_ids or sorted(live_kb_ids)
    targets = sorted(set(targets))
    if not targets:
        progress(1.0)
        return

    # ⚠️ **只删会重建回来的两条腿**（D124）：视觉腿不重建（见函数 docstring），
    #   删了建不回来 —— 实测踩过：加了 `COLL_VISUAL` 之后 reindex 把
    #   25 个页级点全删了，视觉腿从"有数据"变成"0 点"，比漂移更糟。
    #
    # 🔑 D179：删除**按库、紧挨着它自己的重建**做（见下面的循环），
    #   **不再**先把所有目标库一起删光。理由：原实现让**所有**库在整个重建期间
    #   一起空窗 —— 100 万 chunk 实测 ~17ms/chunk ⇒ 约 **4.7 小时**搜不到；
    #   按库逐个做，任一时刻**只有一个库**在空窗，其余库照常可搜。
    #   （单库 = 100 万 chunk 的那种部署仍是它自己空窗那么久 —— 那要靠
    #   集合级别的蓝绿别名，本项目**暂不做**，见 D179 的触发条件。）

    # 🔑 清掉「**库已不存在**」的孤儿点（真实发现，实测 19733 个/腿）。
    #   为什么 targets 覆盖不到它们：`targets` 来自 PG 的活库，
    #   而 eval 的清理用 `DELETE FROM users` **级联硬删**了 documents，
    #   那些点就永远留在 Qdrant 里 —— `delete_by_filter(kb_id=…)` 够不着
    #   一个已经不在 PG 里的 kb_id。
    #   ⚠️ 不清的后果：孤儿点白占内存与召回名额，而且
    #   `deploy/monitor.py` 的对账项会一直报"多 19733"——
    #   一个长期红的监控项等于没有监控项（人会开始忽略它）。
    # 🔴 孤儿清理要覆盖**三条腿**（D124）：`kb_visual` 有了数据之后，
    #   eval 硬删用户留下的页级点同样是孤儿 —— 而"库已不在 PG 里"的点
    #   **永远不可能被重建**（重建的输入来自 PG），所以清掉它们零风险。
    #   实测：加了视觉腿对账项之后第一次跑就报"多 8"，正是这批点。
    orphan_kbs = _orphan_kb_ids_in_qdrant(
        {COLL_DENSE, COLL_SPARSE, COLL_VISUAL}, live_kb_ids
    )
    for kb_id in sorted(orphan_kbs):
        logger.info("清理孤儿点：库 %s 已不在 PG 里，删它的向量", kb_id)
        for coll in (COLL_DENSE, COLL_SPARSE, COLL_VISUAL):
            delete_by_filter(
                coll, {"must": [{"key": "kb_id", "match": {"value": kb_id}}]}
            )
        progress(0.0)

    # 🔑 **向量只用 live 的**（软删文档的点在 delete_by_filter 时会被一起清掉，
    #   重建时不该再加回来 —— 它搜不到，加回来只是浪费空间并让
    #   `/admin/consistency` 的点数看起来"多出来"）。
    # ⚠️ 这里用 `live_rows` 的 `(doc_id, kb_id)` 而不是 `Document` 对象：
    #   重建要跑 embedding，全程几分钟，期间那个库可能被删 ——
    #   拿着 `Document` 对象就得访问**惰性关系** `d.kb`，
    #   而它那时已经是 `None`（实测崩在这，见上面那条注释）。
    #   要 `kb` 就现查一行，或者干脆只用 `kb_id`（`_index_text_legs`
    #   只用到 `kb.id` 与 `kb.visibility/source_kind/workspace_id/created_by`）。
    target_set = set(targets)
    todo = [row for row in live_rows if row[1] in target_set and chunks_of.get(row[0])]
    # 🔑 D179：**按 kb_id 排序**，使每个库的文档连续 —— 这样才能"切到下一个库时
    #   先删这个库的点、紧接着重建它"。⚠️ 不排序的话同一个库会在不同位置被删多次，
    #   后一次删会**抹掉前一次刚重建的点**（静默地重建了又删，结果比漂移更糟）。
    todo.sort(key=lambda r: r[1])
    total = max(1, len(todo))
    kbs: dict[int, Any] = {}
    current_kb: int | None = None
    for i, (doc_id, kb_id, name, mime, updated_at) in enumerate(todo, start=1):
        if kb_id != current_kb:
            # 切到新库：先删**这个库**的两条文本腿，再开始重建它（D179）。
            for coll in (COLL_DENSE, COLL_SPARSE):
                delete_by_filter(
                    coll, {"must": [{"key": "kb_id", "match": {"value": kb_id}}]}
                )
            current_kb = kb_id
        kb = kbs.get(kb_id)
        if kb is None:
            kb = session.get(KnowledgeBase, kb_id)
            if kb is None:
                # 🔴 库在重建期间被删了：**跳过并继续**，不抛。
                #   不抛的理由：它是**并发**导致的状态，不是本次任务出错，
                #   而让整个重建失败会连带词表白重建（已提交的部分不回滚）。
                #   ⚠️ 但必须**记账**（§5.71：静默早退是把"没做"伪装成"做对"）——
                #   计数进最后那行日志，用户能从 `/jobs` 与 worker 日志看到。
                skipped_kbs.add(kb_id)
                progress(0.5 + 0.5 * i / total)
                continue
            kbs[kb_id] = kb
        # ⚠️ `index_terms=False`：词表上面已为全体登记过，重复登记 df 会翻倍
        # 🔑 D164：重建也要走**该库的切分策略对应的编码**，否则 `late` 库
        #   重建出来的向量与入库时不同 —— 那就不是"重建"而是"改了语料"。
        _index_text_legs(
            session, _DocRef(doc_id, name, mime, updated_at), kb,
            chunker.from_rows(chunks_of[doc_id]), index_terms=False,
            encoding=getattr(kb, "chunk_strategy", "recursive") or "recursive",
        )
        progress(0.5 + 0.5 * i / total)

    session.flush()
    progress(1.0)
    logger.info(
        "重建完成：词表 %s 个 chunk，向量 %s 份文档（视觉腿未重建，见函数说明）"
        + (f"，跳过 {len(skipped_kbs)} 个已被删掉的库 {sorted(skipped_kbs)}"
           if skipped_kbs else ""),
        total_chunks, len(todo),
    )



def handle_pdf_preview(
    session, *, doc_id: int | None, payload: dict[str, Any], progress
) -> None:
    """Office → PDF 预览（D34 v1.1），**并把坐标补上**（D131）。

    ⚠️ 失败**只标任务失败**，不动文档状态：转换是增强，
    转不出来时文档照样能搜、能引用、能下载（ingest 早就完成了）。
    反过来做（转换失败 → 文档标 failed）会让人以为"文件没导入成功"。

    🔑 **D131：转换成功之后顺手抽一遍坐标**，写进那些还没有坐标的 chunk。
    Office 文档的解析器不产坐标（`parsers.py::_parse_docx` 的 docstring：
    "⚠️ 无坐标"），所以带框预览对它们一直是关的（D106）。
    而坐标的正确来源就是**这份转换版** —— 用户看到的页图就是它，
    文本与坐标同源，于是"重排导致框指错字"这个 D106 担心的风险不存在。
    实测（`deploy/verify_l3_office.py`，真实 docx）：8 个 chunk 全部 ≥95% 可定位。

    ⚠️ **定位不到的不给框**（`relocate.MIN_COVERAGE`）：
    宁可没有框，也不要错的框 —— 那比没有框更误导（D106 的原话）。
    """
    from ...ingest.office import (
        ConversionError,
        convert_to_pdf,
        needs_conversion,
        soffice_available,
    )

    if doc_id is None:
        raise IngestError("pdf_preview 任务缺 doc_id", retryable=False)
    doc = session.get(Document, doc_id)
    if doc is None:
        raise IngestError(f"文档 {doc_id} 不存在", retryable=False)

    if not needs_conversion(doc.file_name, doc.mime_type):
        # 已经是 PDF（或压根不是 Office）：这是**正常情况**，不是失败
        progress(1.0)
        logger.info("文档 %s 不需要转换（%s）", doc_id, doc.file_name)
        return

    if soffice_available() is None:
        raise IngestError(
            "没装 LibreOffice，Office 文档无法生成预览"
            "（apt install libreoffice-writer libreoffice-impress libreoffice-calc）",
            retryable=False,
            code="PREVIEW_UNAVAILABLE",
        )

    src = blob_path(doc.blob_sha)
    dest = preview_path(doc.blob_sha)
    progress(0.2)
    try:
        convert_to_pdf(src, dest)
    except ConversionError as exc:
        raise IngestError(str(exc), retryable=True,
                          code="PREVIEW_CONVERT_FAILED") from exc
    progress(0.8)
    n_located = _locate_office_chunks(session, doc_id, dest)
    session.flush()
    progress(1.0)
    logger.info("预览已生成 doc=%s %s（%d KB）；补坐标 %s/%s 个 chunk",
                doc_id, dest.name, dest.stat().st_size // 1024,
                n_located, _chunk_count(session, doc_id))


def _chunk_count(session, doc_id: int) -> int:  # noqa: ANN001
    rows = session.execute(
        select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
    ).scalar_one()
    return int(rows or 0)


def _locate_office_chunks(session, doc_id: int, pdf_path: Path) -> int:  # noqa: ANN001
    """在转换版上给还没有坐标的 chunk 补坐标。返回补上的份数。

    ⚠️ **只补没有坐标的**（`locations` 为空）：PDF 文档的坐标来自原件排版，
    那是更准的一份，不能用转换版覆盖（CONVENTIONS §6.10）。
    ⚠️ 抽取**失败不许让任务失败**：预览已经生成成功了，坐标是增强
    （与"转换失败不动文档状态"同一条纪律）。
    """
    from ...ingest.locations import parse_pdf_with_locations
    from ...ingest.relocate import LineBox, relocate_chunks

    try:
        parsed = parse_pdf_with_locations(pdf_path)
    except Exception as exc:  # noqa: BLE001 — 坐标是增强，不是必需
        logger.warning("转换版坐标抽取失败 doc=%s：%s", doc_id, exc)
        return 0

    lines: list[LineBox] = [
        LineBox(
            text="".join((pc.text or "").split()),
            bbox=tuple(float(v) for v in loc["bbox"]),   # type: ignore[index]
            page=int(loc.get("page") or 0),
        )
        for pc in parsed.chunks
        for loc in (pc.locations or [])
        if loc.get("bbox")
    ]
    if not lines:
        return 0

    # 🔑 用 `locations.has_locations()` 判"还没有坐标"，**不用**
    #   `Chunk.locations.is_(None)` —— 这一列是 JSONB，存进去的常常是
    #   JSON `null` 而不是 SQL `NULL`，用 SQL 判会**一条都筛不出来**
    #   （实测静默补了 0 个）。见 `locations.has_locations` 的说明。
    from ...ingest.locations import has_locations

    todo: list[tuple[int, str]] = []
    rows = session.execute(
        select(Chunk.chunk_index, Chunk.content_norm, Chunk.locations)
        .where(Chunk.doc_id == doc_id)
    ).all()
    for chunk_index, text, loc in rows:
        if text and not has_locations(loc):
            todo.append((int(chunk_index), text))

    found = relocate_chunks(todo, lines)
    if not found:
        return 0
    for c in session.execute(
        select(Chunk).where(Chunk.doc_id == doc_id)
    ).scalars():
        loc = found.get(c.chunk_index)
        if loc:
            c.locations = loc
    session.flush()
    return len(found)


# ============================================================
# 导入任务（D47 聊天 / D48 图片目录）
# ============================================================


def _accumulate_days(days: dict[str, Any], msgs: list[dict[str, Any]]) -> int:
    """把一个切片的消息累加进 `days`（内存里）。返回**无日期**的消息数。

    ⚠️ 没有日期的消息**不进任何一天**：把它们塞进某一天会让时间线凭空多出
    一天，而那天的数字永远对不上（JSON 里也查不到来源）。数量由调用方报出来。
    """
    from ...ingest.timeline import DayAgg

    undated = 0
    for m in msgs:
        day = str(m.get("date") or "").strip()
        if not day:
            undated += 1
            continue
        agg = days.get(day)
        if agg is None:
            agg = days[day] = DayAgg()
        agg.add_message(m)
    return undated


def _flush_day_stats(session, kb_id: int, days: dict[str, Any]) -> None:  # noqa: ANN001
    """把内存里的按天聚合并进 `chat_day_stats`（`SELECT FOR UPDATE` + 合并）。

    ⚠️ 不用 UPSERT + JSONB `||`：`||` 是**覆盖**语义，同一个说话人在两个切片里
    都出现时会丢掉一边的计数，而时间线只显示数字、用户没地方对账。
    `FOR UPDATE` 让并发的两个导入任务串行化。
    """
    from ...ingest.timeline import MAX_HIGHLIGHTS

    for day in sorted(days):
        agg = days[day]
        d = dt.date.fromisoformat(day)
        row = (
            session.execute(
                select(ChatDayStat)
                .where(ChatDayStat.kb_id == kb_id, ChatDayStat.day == d)
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if row is None:
            session.add(ChatDayStat(kb_id=kb_id, day=d, **agg.as_row()))
            continue
        row.total += agg.total
        row.docs += agg.docs
        merged_types = dict(row.types or {})
        for k, v in agg.types.items():
            merged_types[k] = merged_types.get(k, 0) + v
        row.types = merged_types
        merged_speakers = dict(row.speakers or {})
        for k, v in agg.speakers.items():
            merged_speakers[k] = merged_speakers.get(k, 0) + v
        row.speakers = merged_speakers
        # ⚠️ highlights 取"更长的 N 条"而不是相加：相加会列两遍同一段话
        pool = list(row.highlights or []) + list(agg.highlights)
        pool.sort(key=lambda h: len(str((h or {}).get("text", ""))), reverse=True)
        row.highlights = pool[:MAX_HIGHLIGHTS]


def _photo_date(file_name: str, data: bytes):  # noqa: ANN201
    """照片的事件日期（EXIF → 文件名 → None）。见 `ingest/timeline.py`。"""
    from ...ingest.timeline import photo_date

    return photo_date(file_name, data)


def _uploader(payload: dict[str, Any]) -> int | None:
    """发起导入的人（`documents.uploaded_by`）。

    ⚠️ 取不到就给 None 而不是报错 —— 任务可能来自旧 payload 或手工入队，
    而 `uploaded_by` 是可空列，**不该因为缺 provenance 让整个导入失败**。
    """
    v = payload.get("uploaded_by")
    return int(v) if isinstance(v, int) else None


def _create_doc(
    session,
    *,
    kb,
    file_name: str,
    data: bytes,
    mime_type: str,
    doc_source: str,
    source_path: str | None,
    uploaded_by: int | None,
):
    """文件字节 → `Document` 行（+ blob 登记）。

    🔑 与上传端点**共用这一份**：blob 登记漏了会直接撞外键
    （`documents_blob_sha_fkey`，踩过），而这类错误只在真正写入时才炸。

    ## 返回值的三种情况（增量索引 D114）

    | 返回 | 含义 |
    |---|---|
    | `(doc, "created")` | 新建 |
    | `(doc, "superseded")` | 新建，并把同名旧版本挪进回收站 |
    | `(None, "same_content")` | **跳过**：同名**且**内容完全相同（重复导入要幂等） |

    ⚠️ 🔴 **同名但内容变了必须 supersede 旧版本**，不能直接再建一行。
    第一版只判"同名同内容"，于是同一个库里出现两个 `is_current=True`
    的同名文档：搜出重复内容、旧版本的向量永远不清（一致性漂移），
    而**上传端点**那句 `scalar_one_or_none()` 之后会抛
    `MultipleResultsFound` —— 那个库再也传不了同名文件（500），
    且症状与原因隔了一周（"上周导过一次照片，今天传同名就 500"）。

    ⚠️ 判据与动作都在 `ingest/versions.py`（唯一实现，见那里的说明）。
    这里**不要**再写一遍 supersede 逻辑。
    """
    sha, size = store_bytes(data)
    register_blob(session, sha, size)

    dup = (
        session.execute(
            select(Document).where(
                Document.kb_id == kb.id,
                Document.file_name == file_name,
                Document.is_current.is_(True),
                Document.deleted_at.is_(None),
                Document.blob_sha == sha,
            )
        )
        .scalars()
        .first()
    )
    if dup is not None:
        return None, "same_content"      # 幂等跳过

    # 🔑 同名、内容不同 → 旧版本进回收站（D28 的 30 天反悔窗口）
    superseded = supersede(find_current(session, kb.id, file_name))

    doc = Document(
        kb_id=kb.id,
        file_name=file_name,
        source_path=source_path,
        content_hash=sha,
        blob_sha=sha,
        mime_type=mime_type,
        size_bytes=size,
        doc_source=doc_source,
        status="pending",
        uploaded_by=uploaded_by,
        # D192：同一 (kb_id, file_name) 的版本号，覆盖式更新时 +1。
        version_id=next_version_id(session, kb.id, file_name),
    )
    session.add(doc)
    session.flush()
    enqueue(session, KIND_INGEST, doc_id=doc.id, kb_id=kb.id)
    return doc, "superseded" if superseded else "created"


def _orphan_kb_ids_in_qdrant(colls: set[str], live_kb_ids: set[int]) -> set[int]:
    """Qdrant 里有、而 PG 里已经没有的 `kb_id`。

    🔑 为什么需要它（真实发现，实测每腿 19733 个孤儿点）：
    eval 的清理用 `DELETE FROM users WHERE email LIKE …` **级联硬删**文档，
    而 Qdrant 的点只在**软删/清理任务**里被删 —— 硬删路径从不碰索引。
    于是那些点永远留在 Qdrant 里，`delete_by_filter(kb_id=…)` 也够不着
    （那个 kb_id 已经不在 PG 里，`targets` 不含它）。

    ⚠️ **只取 payload 里的 `kb_id`，不要 scroll 全量点**：几万个点每次重建
    都全量拉一遍会让 reindex 慢一个数量级（实测本地 2 万点很快，
    但真实语料 10 万点就是几十秒）。`with_payload=["kb_id"]` 让 Qdrant
    只回那一个字段。

    @return 孤儿 kb_id 集合（两腿取并集：任一腿有就算孤儿）
    """
    from ...common.qdrant import get_client

    found: set[int] = set()
    client = get_client()
    for coll in sorted(colls):
        offset = None
        while True:
            body: dict = {
                "limit": 1000, "with_payload": ["kb_id"], "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            r = client.post(f"/collections/{coll}/points/scroll", json=body)
            r.raise_for_status()
            res = (r.json() or {}).get("result") or {}
            points = res.get("points") or []
            for p in points:
                kb = ((p.get("payload") or {}).get("kb_id"))
                if kb is not None and int(kb) not in live_kb_ids:
                    found.add(int(kb))
            offset = res.get("next_page_offset")
            if not offset or not points:
                break
    return found


def handle_import_chat(
    session, *, job_id: int | None, payload: dict[str, Any], progress, report
) -> None:
    """把聊天 jsonl 切成文档并入队摄取（D47）。

    ⚠️ **畸形行跳过并计数**：87k 行里坏掉 1 行不该让整批白导，
    但必须报出来（`bad_lines`）—— 否则用户以为全导进去了。
    ⚠️ **重跑 owner_classifier**（D47）：旧审计结果被改过 3 次，不能继承。
    但判定**只记录不拦截** —— 实测聊天切片 100% 判 `other`
    （对话方名字必然出现在正文里），拿它当门禁会把整个聊天库导空。
    见 REQUIREMENTS D91。
    """
    from ...ingest.importers import (
        DEFAULT_MESSAGES_PER_DOC,
        count_messages,
        is_malformed,
        iter_messages,
        plan_chat_slices,
        render_chat_slice,
        slice_stats,
    )

    # 🔑 模块级累加器必须在**每次任务开始时清空**：worker 进程是复用的，
    #   不清的话第二个导入任务会把上一个任务的判定一起报出去
    #   （`/jobs` 上写着 owner_other=8，其实是两次任务各 4）。
    _owner_tally.clear()

    kb_id = int(payload["kb_id"])
    src = Path(str(payload["source_path"]))
    per_doc = int(payload.get("messages_per_doc") or DEFAULT_MESSAGES_PER_DOC)
    kb = session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise IngestError(f"知识库 {kb_id} 不存在", retryable=False)

    total = int(payload.get("total_msgs") or 0) or count_messages(src)
    report(processed_msgs=0, total_msgs=total, created_docs=0, bad_lines=0)

    processed = created = bad = undated = 0
    #: 🔑 按天聚合在**内存里**累加，整个任务结束时才落库（模块 docstring：
    #:   逐切片 UPSERT 会因为 JSONB `||` 是覆盖语义而丢掉同键计数）。
    #: 只统计**新建**文档覆盖的那几天。
    days: dict[str, Any] = {}
    #: 切片序号 = **它在源文件里的位置**（单调递增，与"建了多少文档"无关）。
    #: 🔑 第一版用 `created + 1` 当编号，于是重复导入时"跳过重复"不递增，
    #:   后面每个切片的文件名都往前挪一位 → 名字变了 → 内容 sha 变了 →
    #:   **不再被判为重复** → 每次重导都多出一批文档（实测 4 → 7）。
    #:   幂等必须靠"同样的输入得到同样的名字"，而不是靠计数器的当前值。
    slice_no = 0
    buf: list[dict[str, Any]] = []

    def flush(force: bool = False) -> None:
        """把缓冲区切成文档。**每 50 条 flush 一次**（约 1 个文档）。"""
        nonlocal processed, created, bad, undated, slice_no
        if not buf or (not force and len(buf) < per_doc):
            return
        slices = plan_chat_slices(buf, per_doc)
        buf.clear()
        for sl in slices:
            slice_no += 1
            name, text = render_chat_slice(sl, slice_no)
            doc, _outcome = _create_doc(
                session,
                kb=kb,
                file_name=name,
                data=text.encode("utf-8"),
                mime_type="text/markdown",
                doc_source="chat_import",
                source_path=str(src),
                uploaded_by=_uploader(payload),
            )
            processed += len(sl)
            if doc is None:
                continue          # 重复导入：文档已存在，不算新建
            created += 1
            _classify_and_count(text, name)
            # 🔑 只对**新建**的文档累加聚合（重复导入不加，否则数字翻倍）
            undated += _accumulate_days(days, sl)

    for msg in iter_messages(src):
        if is_malformed(msg):
            bad += 1
            continue
        buf.append(msg)
        flush()
        if processed and processed % (per_doc * 10) < len(buf):
            report(
                processed_msgs=processed,
                total_msgs=total,
                created_docs=created,
                bad_lines=bad,
            )
    flush(force=True)

    # 时间线数据落库（D20）：**先落聚合再报完成** ——
    # 顺序反过来会出现"任务已 done，用户刷时间线却什么都看不到"。
    _flush_day_stats(session, kb_id, days)

    # 🔑 结束时的 `total_msgs` 用**真实消费行数**，不是请求时的估算：
    #   估算数的是非空行（含畸形行），所以结束时会出现
    #   `processed(200) < total(201)` —— 看起来像"丢了一条消息"。
    #   这里让 `processed + bad_lines == total_msgs` 恒等，
    #   坏行单独计数，用户一眼能对上账。
    verdict = _owner_tally
    report(
        processed_msgs=processed,
        total_msgs=processed + bad,
        created_docs=created,
        bad_lines=bad,
        days_indexed=len(days),
        undated_msgs=undated,
        owner_self=verdict.get("self", 0),
        owner_other=verdict.get("other", 0),
        owner_rule_applied=False,
    )
    session.flush()
    progress(1.0)
    logger.info(
        "导入聊天：%s → %s 条消息 / %s 个文档（坏行 %s，判定 %s）",
        src.name, processed, created, bad, _owner_tally,
    )


#: `owner_classifier` 的累计判定（handler 内状态，不落库单独表）。
_owner_tally: dict[str, int] = {}


def _classify_and_count(text: str, name: str) -> str:
    """重跑归属判定并累计（D47）。

    🔑 **判定不可用不许让导入失败**：隐私复检是"加固"，
    而把 1740 个文档的导入卡死在"判定模块导入不上"上是本末倒置。
    判定挂了就记 `unknown` + 日志，`/jobs` 的 payload 里能看到计数。

    ⚠️ 判定**只记录不拦截**（D91）：聊天切片 100% 判 other
    （对话方名字必然出现在正文里），拿它当门禁会把整个聊天库导空。
    """
    global _owner_tally
    try:
        from ...ingest.privacy import classify_owner

        r = classify_owner(text, name)
        owner = str(r.get("owner") or "unknown")
    except Exception as exc:  # noqa: BLE001
        logger.warning("归属判定不可用，记为 unknown：%s", exc)
        owner = "unknown"
    _owner_tally[owner] = _owner_tally.get(owner, 0) + 1
    return owner


def handle_import_dir(
    session, *, job_id: int | None, payload: dict[str, Any], progress, report
) -> None:
    """把服务器目录里的图片批量导入（D48）。

    ⚠️ **handler 里重新扫目录**而不是照抄端点扫好的清单：
    请求与执行之间文件可能变了（用户又拷了几张进来），
    而 payload 里塞几千个路径既臃肿又可能过期。端点扫的那次只用于
    给用户一个 `estimated_files` 的量级。
    """
    from ...ingest.parsers import IMAGE_SUFFIXES
    from ...ingest.importers import scan_dir

    kb_id = int(payload["kb_id"])
    src = Path(str(payload["path"]))
    recursive = bool(payload.get("recursive", True))
    kb = session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise IngestError(f"知识库 {kb_id} 不存在", retryable=False)

    files = scan_dir(src, recursive=recursive, suffixes=IMAGE_SUFFIXES)
    total = len(files)
    report(processed_files=0, total_files=total, created_docs=0,
           skipped=0, superseded=0, dated=0)

    created = skipped = dated = superseded = 0
    for i, f in enumerate(files, start=1):
        try:
            data = f.read_bytes()
        except OSError as exc:
            logger.warning("读不了 %s：%s", f, exc)
            skipped += 1
            continue
        doc, outcome = _create_doc(
            session,
            kb=kb,
            file_name=f.name,
            data=data,
            # ⚠️ `guess_mime(path)` 收的是 Path（它内部用 path.name）——
            #   传字符串会 AttributeError，而且任务失败信息只有一行很难定位
            mime_type=guess_mime(f),
            doc_source="photo_dir",
            source_path=str(f),
            uploaded_by=_uploader(payload),
        )
        if doc is None:
            skipped += 1          # 内容重复 → 幂等跳过（增量索引：只有变化的文件进库）
        else:
            created += 1
            # 🔑 增量索引（D114）：同名但内容变了 → 旧版本已被挪进回收站。
            #   必须单独计数：用户要能分清"跳过了多少"（没变）
            #   与"替换了多少"（变了）—— 两者混成一个数字就没法判断增量对不对。
            if outcome == "superseded":
                superseded += 1
            # 🔑 事件日期（D20 时间线/照片墙/纪念日）：
            #   EXIF → 文件名 → None（**不猜**，见 ingest/timeline.py）
            taken = _photo_date(f.name, data)
            if taken is not None:
                doc.taken_at = taken
                dated += 1
        if i % 25 == 0 or i == total:
            report(
                processed_files=i,
                total_files=total,
                created_docs=created,
                skipped=skipped,
                superseded=superseded,
                dated=dated,
            )
            session.flush()        # 分批落库：2000 张图不该只有一个巨型事务

    session.flush()
    progress(1.0)
    logger.info(
        "目录导入：%s → %s 个文件，建 %s 个文档（跳过 %s，抽到日期 %s）",
        src, total, created, skipped, dated,
    )


HANDLERS: dict[str, Handler] = {
    KIND_INGEST: handle_ingest,
    KIND_EMBED_VISUAL: handle_visual,
    KIND_THUMBNAIL: handle_thumbnail,
    KIND_CLEANUP: handle_cleanup,
    KIND_REINDEX: handle_reindex,
    # ⚠️ 这两个曾经**缺 handler**：worker 遇到未注册的 kind 只标失败不崩
    #   （见 worker/main.py），于是任务能入队、永远跑不了 ——
    #   `tests/test_jobs_api.py` 里那个"两个已知缺口"白名单就是它们的。
    KIND_PDF_PREVIEW: handle_pdf_preview,
    KIND_IMPORT_CHAT: handle_import_chat,
    KIND_IMPORT_DIR: handle_import_dir,
}


def run_handler(
    kind: str,
    session,
    *,
    doc_id: int | None,
    payload: dict[str, Any],
    progress,
    job_id: int | None = None,
    report=None,
) -> None:
    """跑一个任务的 handler。

    ⚠️ **`report` 只传给声明了它的 handler**（靠签名判断）。
    写成"一律传 5 个参数"的话，每个 handler 都得多写一个用不到的
    `report=None` 参数；而靠签名判断的话，"这个 handler 要不要计数器"
    一眼能从签名看出来（§0.9 的同源精神：能力写在声明处）。
    """
    handler = HANDLERS[kind]
    # ⚠️ 只传 handler **声明过**的参数（第一版无条件传 doc_id →
    #   导入类 handler 不需要 doc_id，直接 TypeError 崩掉：
    #   "handle_import_chat() got an unexpected keyword argument 'doc_id'"）。
    #   统一按签名裁剪，"这个 handler 要什么"就写在它的签名里（§0.9）。
    #
    #   ⚠️ 这里**不要**反过来断言"handler 必须声明全部参数"：
    #   ingest 类 handler 本来就不需要 `report`（它只回进度），
    #   导入类不需要 `doc_id`。而"声明了 run_handler 给不了的东西"已经由
    #   TypeError 自然挡住了（那是真正需要发现的错）。
    given = {
        "doc_id": doc_id, "payload": payload, "progress": progress,
        "job_id": job_id, "report": report,
    }
    wanted = inspect.signature(handler).parameters
    handler(session, **{k: v for k, v in given.items() if k in wanted})
