"""运维/审计的响应模型（M8 + M9）。

⚠️ 对账报告**必须给具体 id**，不能只给布尔值 —— 布尔值只能告诉你"有问题"，
不能告诉你"修哪几个"，而对账的价值全在后者（CONVENTIONS §0.3 的错误码精神：
失败要指向原因）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditOut(BaseModel):
    """审计记录（契约 §12 `GET /audit`）。

    ⚠️ `detail` 里含 query 摘要 —— 那**是用户的数据**，
    所以这个模型只能出现在已鉴权到工作区级的端点里。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    api_key_id: int | None
    user_id: int | None
    action: str
    kb_ids: list[int]
    detail: dict[str, Any] | None = None
    created_at: dt.datetime


class ReindexResp(BaseModel):
    """`POST /admin/reindex`：入队了哪些库、任务 id 是多少。"""

    kb_ids: list[int]
    job_ids: list[int]


class LegReport(BaseModel):
    """一条腿（`kb_dense` / `kb_sparse` / `kb_visual`）的对账结果。

    | 字段 | 含义 | 该做什么 |
    |---|---|---|
    | `missing` | PG 有、这条腿没有 | 漏索引 → 文本腿 `KIND_REINDEX` **能**修（从 PG 重建）；视觉腿要重跑 `embed_visual` |
    | `dangling` | 这条腿有、PG 没有 | 残留点 → reindex **修不了**（它不造 PG 行），要重跑摄取/软删 |

    ⚠️ 列表截断到 200 条，完整数量看 `*_count`。
    ⚠️ **三条腿的 PG 对应物不同**（D124）：文本腿对 `chunks` 行，
    视觉腿对 `documents.scan_pages`。
    """

    collection: str
    points: int = 0
    missing: list[int] = []
    dangling: list[int] = []
    missing_count: int = 0
    dangling_count: int = 0
    consistent: bool = False


class ConsistencyReport(BaseModel):
    """`GET /admin/consistency`：PG 与 Qdrant 的差异明细。

    🔑 **`legs` 是重点**：三条腿**分开**对账。
    第一版把两条腿的点 ID 取并集，于是"只有 sparse 缺了"这种半坏状态
    会报成"一致" —— 而那正是真实的故障模式（老 reindex 只重建 sparse）。

    ⚠️ D124：`legs` 现在有**三**项（多了 `kb_visual`）。
    少一条腿就等于那条腿坏了没人知道 —— D123 就是这么烂了没人发现的。

    ⚠️ 顶层 `*_count` / `*_in_*` 是**文本腿并集**口径，单腿差异只在 `legs` 里体现。
    ⚠️ 两个列表都截断到 200 条（用 `*_count` 看总数）——真漂移上千条时，
    全量返回会把响应撑到几百 KB，而调用方只需要知道"有多少、去哪修"。
    """

    kb_ids: list[int]
    pg_chunks: int
    qdrant_points: int
    legs: dict[str, LegReport] = {}
    missing_in_qdrant: list[int] = []
    dangling_in_pg: list[int] = []
    missing_count: int = 0
    dangling_count: int = 0
    consistent: bool = False


class WorkspaceStats(BaseModel):
    """`GET /workspaces/{id}/stats`（M9：按工作区统计）。

    ⚠️ 两个字节数量的**不是同一个东西**（少了任何一个都会误导用户去清磁盘）：
    - `sum_doc_size_bytes`：各库文档 `size_bytes` 之和 —— **只有原始文件**
    - `blob_disk_bytes`：blob **目录**实际占盘 = 原始 + 派生文件
      （`preview.pdf`、`thumbs/*.jpg`），所以通常**更大**（实测 318KB → 722KB）

    去重体现在另一个方向：同一份内容被多个库引用时 `sum_doc_size_bytes`
    会重复计算，而磁盘只占一份。
    """

    workspace_id: int
    kb_count: int = 0
    doc_count: int = 0
    chunk_count: int = 0
    sum_doc_size_bytes: int = 0
    exclusive_blob_count: int = 0
    blob_disk_bytes: int = 0

# ============================================================
# D20 时间线与照片墙
# ============================================================


class MonthDayOut(BaseModel):
    """月视图里的**一天**（`TimelineMonthOut.days` 的一项）。

    🔑 `photo_count` 是**数量**，而 `TimelineDayOut.photos` 是**列表** ——
    两个查询形态不同，字段名也就不能同名。第一版共用一个模型、字段都叫
    `photos`，于是月视图塞了个 int 进去 → pydantic 报
    `Input should be a valid list` → 响应 500，而错误信息完全指不到
    "这是设计错了"。
    """

    date: str
    total: int = 0
    photo_count: int = 0
    #: 当天最长的几条消息摘要（截断，≤3 条 × ≤160 字）
    highlights: list[dict[str, Any]] = []


class TimelineDayOut(BaseModel):
    """当天明细（`?date=YYYY-MM-DD`）。`photos` 是**列表**（可点开）。

    ⚠️ `total == 0` 且 `photos == []` 的日子**不会出现在月视图的 days 里**
    （那里只放值得点开的日子），所以在月视图里看到的每一天都有内容。
    """

    date: str
    total: int = 0
    types: dict[str, int] = {}
    speakers: dict[str, int] = {}
    #: 当天最长的几条消息摘要（截断，≤3 条 × ≤160 字）
    highlights: list[dict[str, Any]] = []
    photos: list[dict[str, Any]] = []
    docs: int = 0


class TimelineMonthOut(BaseModel):
    """月视图。

    ⚠️ `days` 是**子集**（有照片 / 消息量 ≥ 该月中位数），不是全部日期 ——
    两年数据有 697 天，全列出来没人看得完，而月视图要的是"值得点开的那几天"。
    `total`/`active_days`/`speakers` 是**整月合计**。
    """

    kb_id: int
    month: str
    total: int
    active_days: int
    photo_count: int = 0
    speakers: dict[str, int] = {}
    days: list[MonthDayOut] = []


class AnniversaryOut(BaseModel):
    """纪念日 = **有照片的日子**（契约 §6 的原话）。

    ⚠️ 只列有照片的日子：把没有照片的日子也列进来会让"纪念日"
    退化成"消息最多的一天"，那不是用户对"纪念日"的预期。
    """

    date: str
    count: int
    photos: list[str] = []
    chat_total: int = 0


class MilestoneOut(BaseModel):
    """里程碑 = 消息量 top-N 的日子。

    ⚠️ 判据是"按 `chat_total` 降序"，**不设阈值魔法数**：
    「哪天最热闹」是可解释的规则，而"超过均值 1.5 倍"不是
    （换个语料规模含义就变，用户也看不懂为什么某天没进列表）。
    """

    date: str
    chat_total: int
    photos: int = 0
    note: str = ""


class PhotoOut(BaseModel):
    """照片墙的一行。

    ⚠️ `date` 可空：没有 EXIF 也没有日期文件名的照片挂不上时间线
    （`taken_at` 留空，**不猜**），但照片墙仍然列得出来 —— 藏在最后，
    而不是被过滤掉（过滤掉等于让用户以为照片丢了）。
    """

    doc_id: int
    file_name: str
    date: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    status: str = "pending"
    #: ⚠️ 缩略图端点要 Bearer，前端必须走 fetch（§6.8）
    thumb_url: str | None = None


# ============================================================
# 外部 LLM 配置（D170）
# ============================================================


class LlmConfigOut(BaseModel):
    """`GET /admin/llm`：当前外部 LLM 配置（**API Key 只回掩码**）。

    🔑 `has_key` 与 `api_key_masked` 分开：前者是"配没配"，
    后者是"配的是哪一把的前后缀"（让人认得出，但拿不到）。
    明文**永远不出现在响应里**（否则读一次配置就泄露一次）。
    """

    provider: str = "extractive"
    base_url: str | None = None
    model: str | None = None
    enabled: bool = False
    has_key: bool = False
    #: 如 `sk-xx…xxxx`（首 5 + 尾 4），**不是**明文
    api_key_masked: str | None = None
    extra_headers: dict[str, Any] = Field(default_factory=dict)
    temperature: float = 0.3
    #: 🔑 D171：哪些功能用外部 LLM（细粒度开关）
    use_for_ask: bool = True
    use_for_contextual: bool = True
    updated_at: dt.datetime | None = None
    updated_by: int | None = None
    #: 🔑 生效判据（后端算好，前端不重算）：能不能真的走外部 LLM
    external_active: bool = False


class UpdateLlmReq(BaseModel):
    """`PUT /admin/llm`。

    ⚠️ `api_key` **不传 = 保持原值**（不是清空）—— 否则用户在页面上
    只改模型名就会把 key 抹掉，而界面上看不出来。清空要用 `clear_api_key=True`。
    """

    provider: Literal["extractive", "openai_compat"] = "extractive"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    clear_api_key: bool = False
    extra_headers: dict[str, str] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    enabled: bool | None = None
    #: 🔑 D171：细粒度功能开关（不传 = 保持原值）
    use_for_ask: bool | None = None
    use_for_contextual: bool | None = None


class LlmTestResp(BaseModel):
    """`POST /admin/llm/test`：用**当前配置**发一次最小请求。"""

    ok: bool
    message: str = ""
    latency_ms: int | None = None
    model: str | None = None


# ============================================================
# PII 检出报告（D121 · M8）
# ============================================================


class PiiDocOut(BaseModel):
    """一份文档的 PII 计数。

    🔑 **不含原文片段** —— 报告的用途是"要不要开掩码 / 要不要分享"，
    给了片段它就成了新的泄露面（见 `routers/pii.py` 的模块 docstring）。
    """

    doc_id: int
    file_name: str
    #: 文档状态（`ready` / `pending` …）—— 用来区分"检测过"与"还没摄取完"
    status: str = "pending"
    total: int
    by_type: dict[str, int] = Field(default_factory=dict)


class PiiReport(BaseModel):
    """一个知识库的 PII 报告。

    ⚠️ `source_*` 与 `indexed_*` 是**两套数字**，不是同一件事：
    `source_*` = 原文里检出几处（摄取时记下，掩码**前**）；
    `indexed_*` = 索引文本现在还剩几处（现场扫 chunk）。
    开了掩码就是 `source_total=7 / indexed_total=0`。
    合成一个数字会让用户以为掩码没生效（D121）。
    """

    kb_id: int
    #: `on` / `off` —— 只作用于**之后**摄取的文档，不回溯已有 chunk
    masking: str = "off"
    source_total: int = 0
    source_by_type: dict[str, int] = Field(default_factory=dict)
    indexed_total: int = 0
    indexed_by_type: dict[str, int] = Field(default_factory=dict)
    #: 本库可见文档总数
    doc_count: int = 0
    #: 检出过 PII 的文档数
    docs_with_pii: int = 0
    #: ⚠️ `pii_hits IS NULL` 的文档数（**还没摄取完**：排队中 / 失败）。
    #:   它**不是**"干净"，报告里必须单列出来。
    docs_not_scanned: int = 0
    #: 🔴 已摄取但**没有文本层**的文档数（照片 / 纯扫描件，`chunk_count = 0`）。
    #:   这类文档**根本没有可供检测的文本**，所以报告里它们的贡献是 0 ——
    #:   而那个 0 **不代表图片里没有敏感信息**（OCR 层不在检测范围内）。
    #:   ⚠️ 必须与 `docs_not_scanned` 分开报：两者都是"没测到"，但原因不同，
    #:   而用户能采取的动作也不同（等摄取完 / 知道图片这条路管不了）。
    docs_no_text: int = 0
    docs: list[PiiDocOut] = Field(default_factory=list)
    #: 明细被 `DOC_LIMIT` 截断（`source_total` 仍是全量）
    docs_truncated: bool = False
    scan_chars: int = 0
    scan_truncated: bool = False
