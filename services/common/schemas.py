"""Pydantic 请求/响应模型 —— 对应 `docs/DESIGN-API.md`。

三条铁律：
1. **请求模型里不出现** `user_id` / `workspace_id` 之外的租户字段
   （`workspace_id` 本身是允许的，因为它必须由服务端校验成员资格）
2. 所有响应都用 `model_config = ConfigDict(from_attributes=True)` 直出 ORM
3. 错误响应统一走 `errors.PkhError.to_dict()`
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Visibility = Literal["private", "workspace", "global"]
SourceKind = Literal["file", "chat", "photo"]
#: D164 切分策略。⚠️ 取值必须与 `services/ingest/strategies.STRATEGY_NAMES` 一致
#: （`common` 不能 import `ingest`，否则成环 ⇒ 用守卫测试钉住两边相等）。
ChunkStrategy = Literal[
    "fixed", "recursive", "semantic", "parent_child", "late", "contextual"
]
DocStatus = Literal[
    "pending", "parsing", "chunking", "embedding", "ready", "failed", "deleted"
]
SearchMode = Literal["semantic", "exact", "hybrid"]

#: 用户标识（登录名）。**刻意不用** pydantic 的 `EmailStr`：
#:
#: `EmailStr` 会调 email-validator 拒绝"特殊用途/保留域名"，实测：
#: ```
#:   alice@t.local      ❌  The part after the @-sign is a special-use
#:   a@mycompany.local  ❌  or reserved name
#:   a@localhost        ❌
#:   a@pkhtest.dev      ✅
#: ```
#:
#: 本项目是**局域网自部署**（D8 单机部署，D23 团队 5~20 人），
#: 同事的域账号极可能是 `@mycompany.local` 这类 —— 用 EmailStr 的话
#: 他们**一个都注册不了**。所以只校验基本形状（有一个 @、两侧非空、
#: 无空格），把"是不是能送达的邮箱"交给部署方。
#:
#: 真正的唯一性约束在 DB：`users.email` 有 UNIQUE 索引。
EmailStrLocal = Annotated[str, StringConstraints(
    min_length=3,
    max_length=254,
    pattern=r"^[^@\s]+@[^@\s]+$",
)]

#: 🔑 D174：唯一昵称，可作登录名。3~32 字符，不含空白与 `@`（避免与邮箱混淆）。
NicknameStr = Annotated[str, StringConstraints(
    min_length=3,
    max_length=32,
    pattern=r"^[^\s@]+$",
)]

# D30：硬上限
MAX_FILE_BYTES = 50 * 1024 * 1024
# D45：单请求文件数
MAX_FILES_PER_REQUEST = 20
# D30：页数上限
MAX_PAGES = 2000
# D37：扫描件页数上限
MAX_SCAN_PAGES = 50


class PageMeta(BaseModel):
    total: int
    page: int = 1
    page_size: int = 20


# ============================================================
# 认证
# ============================================================


class RegisterReq(BaseModel):
    email: EmailStrLocal
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    #: D174：唯一昵称（可选；给了就能用它登录）。唯一性由 DB 约束兜底。
    nickname: NicknameStr | None = None


class LoginReq(BaseModel):
    #: 邮箱**或**昵称（D174）。刻意用 `str` 而非 `EmailStrLocal` —— 昵称不是邮箱。
    email: str = Field(min_length=3, max_length=254)
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    #: D174 唯一昵称（可作登录名）；存量用户可能为 null。
    nickname: str | None = None
    #: D173 全局角色：`user` | `superadmin`。
    role: str = "user"
    created_at: dt.datetime


# ============================================================
# 超管：用户管理（D173）
# ============================================================


class AdminUserOut(BaseModel):
    """`GET /admin/users` 的一行（**超级管理员**才能看）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    nickname: str | None = None
    display_name: str
    role: str = "user"
    is_active: bool = True
    created_at: dt.datetime


class CreateUserReq(BaseModel):
    """`POST /admin/users`：超管直接建号（自助注册关掉时的 UI 入口）。"""

    email: EmailStrLocal
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    nickname: NicknameStr | None = None
    role: Literal["user", "superadmin"] = "user"


class UpdateUserReq(BaseModel):
    """`PATCH /admin/users/{id}`。不传的字段保持不变。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    nickname: NicknameStr | None = None
    clear_nickname: bool = False
    #: 超管可直接改邮箱（唯一）；改自己也算改任意用户，但唯一性照查。
    email: EmailStrLocal | None = None
    #: 超管**重置**密码（不需要旧密码 —— 这正是"超管能改任意用户信息"的一部分）。
    new_password: str | None = Field(default=None, min_length=8, max_length=128)
    role: Literal["user", "superadmin"] | None = None
    is_active: bool | None = None


class UpdateMeReq(BaseModel):
    """`PATCH /auth/me`：**任何用户改自己的资料**（D175）。

    ⚠️ 改 `email` / `new_password` **必须**带 `current_password`（校验旧密码）——
    否则一次 XSS 或借用的设备就能把账号锁走。改昵称/显示名不需要。
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    nickname: NicknameStr | None = None
    clear_nickname: bool = False
    email: EmailStrLocal | None = None
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class LoginResp(BaseModel):
    """明文 key 只在这里出现一次，之后只有 hash 存库。"""

    user: UserOut
    api_key: str
    key_prefix: str


class CreateKeyReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    scopes: list[str]
    #: 最后一次**成功**使用的时间（D139 审计时实测确认的语义）：
    #: 请求作用域的 session 在响应前无条件 `commit`，而**失败请求走 rollback**
    #: ⇒ 401/403/500 都不会更新它。未用过是 `null`（不是 0、不是"1970"）。
    last_used_at: dt.datetime | None
    created_at: dt.datetime


class CreateKeyResp(BaseModel):
    """新建 key 的响应。

    明文 `api_key` **只在这个响应里出现一次**，之后库里只有哈希、无法再取。
    单独用这个类型而不是复用 `ApiKeyOut` + 响应头 ——
    明文是这个响应的正式组成部分，藏 header 里调用方会漏读。
    """

    key: ApiKeyOut
    api_key: str


# ============================================================
# 工作区
# ============================================================


class CreateWorkspaceReq(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class UpdateWorkspaceReq(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    owner_id: int
    created_at: dt.datetime
    #: D129：软删时间。**只有 `GET /workspaces/trash` 里非空**，
    #: 用来算"还能恢复几天"（D42 的 30 天窗口）。
    deleted_at: dt.datetime | None = None


class AddMemberReq(BaseModel):
    email: EmailStrLocal
    role: Literal["owner", "member"] = "member"


class CreateInviteReq(BaseModel):
    """生成邀请码的入参。⚠️ **没有 `role`** —— 邀请码只能给 member（D4）。

    给了 role 就等于"转发一段码就能把整个工作区交出去"。
    """

    ttl_days: int | None = Field(
        default=None, ge=1, le=90,
        description="有效期（天）。不传 = 7 天（invites.DEFAULT_TTL_DAYS）",
    )
    max_uses: int | None = Field(
        default=None, ge=1, le=50,
        description="可用次数。不传 = 1（一码一人）",
    )


class InviteOut(BaseModel):
    """邀请码（🔴 **永远不含明文**，只有 `code_prefix`）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    code_prefix: str
    role: str
    max_uses: int
    used: int
    created_at: dt.datetime
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None
    #: 给人看的一行（"3 天后到期" / "已过期"）
    expiry_text: str = ""
    #: 可用 / 已用完 / 已过期 / 已撤销（**列表**可以区分，兑换时不可以）
    state: str = "可用"


class CreateInviteResp(BaseModel):
    """生成成功。🔑 `code` 是明文，**只出现在这一个响应里**（D64）。"""

    invite: InviteOut
    code: str
    notice: str


class RedeemInviteReq(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RedeemInviteResp(BaseModel):
    workspace_id: int
    workspace_name: str
    role: str
    #: 已经是成员时为 True（不算失败，但要说清"不用重复兑换"）
    already_member: bool = False
    message: str = ""


class MemberOut(BaseModel):
    user_id: int
    email: str
    display_name: str
    role: str
    joined_at: dt.datetime


# ============================================================
# 知识库
# ============================================================


class CreateKbReq(BaseModel):
    workspace_id: int
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    visibility: Visibility = "private"
    source_kind: SourceKind = "file"
    chunk_size: int = Field(default=500, ge=100, le=2000)
    chunk_overlap: int = Field(default=50, ge=0, le=500)
    # D164：切分策略，默认 recursive = 现行为。
    chunk_strategy: ChunkStrategy = "recursive"
    # D31：图片内容描述可选
    generate_image_desc: bool = False


class UpdateKbReq(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    visibility: Visibility | None = None
    generate_image_desc: bool | None = None
    # D121：运行时 PII 掩码开关（🔴 默认关闭）。
    # ⚠️ **只对之后摄取的文档生效** —— 已经切好的 chunk 不会被回溯改写
    #   （改写等于重建整个索引，而 PATCH 不能同步等它）。
    #   报告端点 `GET /kb/{id}/pii` 会告诉用户"索引里还剩多少处"。
    pii_mask: bool | None = None
    # 改分块参数需要重跑索引，用 PATCH 的文档默认不自动触发，
    # 由前端显式调 /documents/{id}/reparse
    chunk_size: int | None = Field(default=None, ge=100, le=2000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=500)
    # D164：改切分策略同样**只对之后重新摄取的文档生效**（与 chunk_size 同款）。
    chunk_strategy: ChunkStrategy | None = None
    # 阶段 3 per-KB 精排（D87）。
    # 🔑 **`null` 与 `""` 都表示"不启用"**（D90），**不是**"用全局默认"：
    #   全局默认（`settings.rerank_model`）只是"配置里写着哪个模型可用"，
    #   实际启用与否由这个字段决定（不启用时 `/search/debug` 的
    #   `rerank.skipped` 会**如实说明**"该库未配置"）。
    #   若把 `null` 解释成"回退默认"，每个库都会跑精排而用户没要求过
    #   （第一版正是这样：实测 `applied=true`、白花 5.2s，而界面上看不出来）。
    rerank_model: str | None = Field(default=None, max_length=200)


class KbOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    name: str
    description: str | None
    visibility: Visibility
    source_kind: SourceKind
    embed_model: str
    chunk_size: int
    chunk_overlap: int
    generate_image_desc: bool
    #: D164：切分策略（默认 recursive）。
    chunk_strategy: ChunkStrategy = "recursive"
    #: D121：掩码开关（默认 False）
    pii_mask: bool
    #: 阶段 3 per-KB 精排（D87）。`null` = **未启用**（不是"用全局默认"，D90）。
    #: ⚠️ 之前这个字段**只存在于数据库**、读写 API 都不碰它 ——
    #: 于是"按库开启精排"这件事只能靠手写 SQL（`api_1d_check.py` 就是这么干的），
    #: 界面上也永远看不到当前状态。列在 `KbOut` 里就是为了让用户看得见。
    rerank_model: str | None = None
    created_by: int
    created_at: dt.datetime
    updated_at: dt.datetime
    #: D129：软删时间。**只有回收站列表里非空**，其它响应恒为 `null`。
    #: 🔑 前端靠它算"还能恢复几天"（与文档回收站同一个口径，`lib/trash.ts`）——
    #:   看不见剩余窗口的回收站等于没有（D26）。
    deleted_at: dt.datetime | None = None


class KbDetail(KbOut):
    doc_count: int = 0
    chunk_count: int = 0
    size_bytes: int = 0
    #: 🔑 D181：**还停在旧切分策略**的当前文档数（对应文章「坑三：一库里混多套切法」）。
    #: 判据 = 文档摄取时记在 `documents.quality.strategy` 的策略 ≠ 该库**当前**
    #: 的 `chunk_strategy`（D164）。`0` = 干净。
    #: ⚠️ 改 `chunk_strategy` **只对之后重新摄取的文档生效**（`PATCH /kb/{id}` 不自动重跑）
    #:   ⇒ 这个数字就是"重摄取欠账"。前端的用法是：>0 时提示"这 N 份可点重解析"。
    #: ⚠️ 加 D166 之前的文档 `quality` 为 NULL，按**当时的默认 `recursive`** 计
    #:   （`coalesce(…, 'recursive')`）—— 所以默认策略的库不会平白报一堆欠账。
    stale_strategy_docs: int = 0


# ============================================================
# 文档
# ============================================================


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    file_name: str
    source_path: str | None
    mime_type: str | None
    size_bytes: int | None
    status: DocStatus
    status_msg: str | None
    is_current: bool
    #: D192：同一 (kb_id, file_name) 的版本号（从 1 起，覆盖式更新 +1）。
    #: `is_current` 说"哪一版在检索里"，它说"这是第几版"——审计/历史追踪用。
    version_id: int = 1
    #: 软删时间（D26）。**回收站视图靠它算"还能恢复几天"** ——
    #: `/kb/{id}/trash` 按它筛选却不返回它，前端只能显示 "-"，
    #: 那正是"用户不知道自己的反悔窗口还剩几天"。
    #: 普通列表里恒为 null（那些是活文档）。
    deleted_at: dt.datetime | None = None
    page_count: int | None
    chunk_count: int
    # D37/D60：扫描件没有坐标，只能整页高亮。前端据此提示用户。
    is_scanned: bool = False
    #: ⚠️ **可空**：`documents.uploaded_by` 是可空列，而导入类文档
    #: （chat_import / photo_dir）由后台任务创建、可能没有"上传人"
    #: 这个概念。写成 `int` 会让文档列表在有导入文档时**整个 500**
    #: （pydantic 校验失败，不是那一行出错，是整个响应挂掉）。
    uploaded_by: int | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def doc_id(self) -> int:
        """`id` 的别名。

        契约早期写作 `doc_id`，实现按 ORM 字段名用了 `id`。
        保留别名是为了不让已经按 `doc_id` 写的客户端突然 422/KeyError。
        """
        return self.id


class DocumentDetail(DocumentOut):
    preview_url: str
    content_hash: str


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_index: int
    content_norm: str
    char_start: int | None
    char_end: int | None
    locations: list[dict[str, Any]] | None
    token_count: int | None
    # D58：前端靠 kind 区分单框/多框，表格还要靠 row/col 排网格
    kind: Literal["text", "table", "page", "chat"] = "text"
    table_index: int | None = None
    row_count: int | None = None
    col_count: int | None = None
    #: D203：表格的**结构化**网格 —— 前端据此把表格渲染成真表格
    #: （而不是等宽 Markdown）。非表格 chunk 为 `null`。
    table_json: dict[str, Any] | None = None
    #: D165 章节路径（`H1 > H2`）。前端"看切分质量"时能一眼看出块属于哪一节。
    #: ⚠️ 刻意**不返回 `parent_text`**：它可能上千字，chunk 列表一次几十上百条
    #:   会把响应撑大（父块上下文只走证据回传那条路）。
    section_path: str | None = None


class UploadResp(BaseModel):
    batch_id: str
    documents: list[DocumentOut]


class ImportDirReq(BaseModel):
    """D48：从服务器目录批量导入图片/文档。"""

    path: str
    recursive: bool = True


class ImportChatReq(BaseModel):
    """D47：聊天数据导入。服务端读文件，不走 HTTP 上传。"""

    kb_id: int
    source_path: str
    # D41：每 50 条消息切一个文档
    messages_per_doc: int = Field(default=50, ge=10, le=500)


class ImportResp(BaseModel):
    job_id: int
    estimated: int | None = None


class PurgeResp(BaseModel):
    """`DELETE /trash` 的结果（契约 §4.3）。

    `dry_run=true` 时这些数字是**预测值**（"点下去会删多少"），
    不动任何数据。前端确认框用它渲染"确定清空 N 份 / X MB？"。
    """

    docs: int = 0
    #: 连带清掉的 chunk 行数（chunk 靠 `ON DELETE CASCADE` 跟着文档走）
    chunks: int = 0
    #: 引用计数归零、**物理删除**了 blob 文件的份数
    blobs_dropped: int = 0
    #: 释放的磁盘字节（只算真正物理删掉的那些）
    bytes_freed: int = 0
    doc_ids: list[int] = Field(default_factory=list)
    #: `doc_ids` 被截断了吗（超过 `PurgeStats.ID_LIST_CAP`）
    ids_truncated: bool = False
    #: PG 已删但 Qdrant 点没清掉的文档。**不吞**：阶段 4 的对账会捡到，
    #: 现在报出来是为了让人知道"索引要等对账才干净"。
    qdrant_failed: list[int] = Field(default_factory=list)
    dry_run: bool = False


# ============================================================
# 检索
# ============================================================


class SearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode = "hybrid"
    top_k: int = Field(default=20, ge=1, le=100)
    page: int = Field(default=1, ge=1)
    #: D161：把 `/search/debug` 的过程数据并进结果响应。
    #: 默认 false —— 普通检索不需要四条腿的原始命中（响应会大几倍）。
    include_debug: bool = False
    #: D196：查询改写（文章 §Query Rewrite）。默认 false = 行为与引入前一致。
    #: true 时会额外用**规则层**变体检索并融合；是否用外部 LLM 由库配置
    #: 的 `use_for_rewrite` + 凭据决定（见 `retrieval/rewrite.py`）。
    rewrite: bool = False
    #: D199：按**文档更新时间**收紧范围（文章 §Metadata 的时间过滤）。
    #: 都为空 = 不过滤（默认路径逐字不变）。⚠️ 旧点没有 `updated_at` 字段，
    #: 加了时间过滤后它们会被排除 —— 这是"重摄取欠账"。
    updated_after: dt.datetime | None = None
    updated_before: dt.datetime | None = None


class CrossKbSearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    # kb_ids=None 表示"有权限的所有 KB"（服务端推导，D8）
    kb_ids: list[int] | None = None
    mode: SearchMode = "hybrid"
    top_k: int = Field(default=20, ge=1, le=100)
    #: D161：跨库检索也支持分页与过程数据，与单库端点对齐。
    page: int = Field(default=1, ge=1)
    include_debug: bool = False
    #: D196：查询改写（见 `SearchReq.rewrite`）。
    rewrite: bool = False
    #: D199：按文档更新时间收紧范围（见 `SearchReq.updated_after`）。
    updated_after: dt.datetime | None = None
    updated_before: dt.datetime | None = None


class HighlightSpan(BaseModel):
    text: str
    highlight: str
    # 命中位置 [[start, end], ...]（字符偏移，前端渲染高亮）
    positions: list[list[int]] = Field(default_factory=list)


class SearchHit(BaseModel):
    doc_id: int
    file_name: str
    chunk_index: int
    #: chunk 正文（`text_raw` 优先，D35）。
    #:
    #: ⚠️ 为什么需要它（D77）：`snippets` 是**字面子串**匹配的产物
    #: （`trgm.highlight_spans` 用 `re.escape(query)` 找连续子串），
    #: 而自然语言提问从来不是原文的子串 —— 实测 hybrid 检索
    #: 「入职日期那栏填什么」时 **10 条结果的 snippets 全是空的**。
    #: 没有它，调用方（前端 / MCP / A2A）只能再发一轮请求去
    #: `GET /documents/{id}/chunks` 按 chunk_index 捞正文。
    text: str = ""
    snippets: list[HighlightSpan] = Field(default_factory=list)
    #: **RRF 分**（`1/60 + 1/62 ≈ 0.0326`），只能用来排序，不是置信度。
    score: float = 0.0
    kind: Literal["text", "page", "image"] = "text"
    locations: list[dict[str, Any]] | None = None
    thumb_url: str | None = None


class SearchResp(BaseModel):
    items: list[SearchHit]
    total: int
    mode: SearchMode
    page: int = 1
    #: D161：`include_debug=true` 时附上四路召回 + RRF 融合过程
    #: （形状与 `POST /kb/{id}/search/debug` 一致）。
    #: 默认 `None` —— 普通检索不该背上调试负载。
    debug: dict[str, Any] | None = None


class AskReq(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    stream: bool = True


class CrossAskReq(BaseModel):
    """D163：多库问答。`kb_ids=None` = 全部有权限的库（D8 服务端推导）。"""

    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[int] | None = None
    session_id: int | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    stream: bool = True


class SourceItem(BaseModel):
    idx: int
    kind: Literal["text", "page", "image"]
    score: float
    title: str
    doc_id: int
    chunk_index: int
    date: str = ""
    text: str = ""
    locations: list[dict[str, Any]] | None = None
    thumb_url: str | None = None


class AskDone(BaseModel):
    citations: list[int] = Field(default_factory=list)
    elapsed_ms: int


# ============================================================
# 会话
# ============================================================


class CreateSessionReq(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    #: D163：这次会话的检索范围。`None` = 全部有权限的库。
    kb_ids: list[int] | None = None


class UpdateSessionReq(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: Literal["user", "assistant", "system"]
    content: str
    citations: list[int] | None
    created_at: dt.datetime


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int | None
    #: D163：检索范围。老（单库）会话这里也会有值（迁移时回填成 [kb_id]）。
    kb_ids: list[int] | None = None
    title: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut] = Field(default_factory=list)


# ============================================================
# 任务
# ============================================================


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    doc_id: int | None
    kb_id: int | None
    status: Literal["pending", "running", "done", "failed", "cancelled"]
    progress: float
    attempts: int
    error: str | None
    payload: dict[str, Any] | None
    created_at: dt.datetime
    updated_at: dt.datetime


# ============================================================
# 运维
# ============================================================


class HealthResp(BaseModel):
    """`GET /health`（免鉴权）与 `GET /api/v1/health`（鉴权）**共用**一份。

    ⚠️ `models` / `queue` **只在鉴权版里有值**（免鉴权版是空 dict）：
    它们是部署细节（"bge 没加载"、"堆了 200 个任务、24 小时失败 37 个"），
    不该出现在公网探活端点上。共用一个模型是为了让反代与监控脚本
    **一个解析器**读两个端点（`routers/health.py` 有完整理由）。
    """

    status: Literal["ok", "degraded"]
    db: dict[str, Any]
    qdrant: dict[str, Any]
    worker: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)
    #: 🔑 D158：这个部署**实际能跑哪几条检索腿**。
    #: 接通视觉服务之前它恒是 `["dense","sparse","trgm"]`，而那个"恒"是
    #: `CONVENTIONS §4.1` 的副作用（API 进程不装 colSmol），不是能力上限。
    #: 现在视觉服务装没装会真的改变这个列表 ⇒ 必须**如实**报，
    #: 别硬编码（硬编码的判据在两种情况下都"对"，测试不会红，
    #: 而运维会照着一个错的 `/health` 排障）。
    #: ⚠️ 同样**只在鉴权版里有值**（与 `models` 同一条理由，见模块 docstring）。
    available_legs: list[str] = Field(default_factory=list)
    #: D133 · M9「`/health` 含 DB/Qdrant/**队列**状态」：
    #: `{pending, running, failed_24h, oldest_running_seconds, stuck_after_seconds}`。
    #: ⚠️ 这**不是心跳**（worker 是独立进程，这里没有心跳表）——
    #:   它回答"任务堆了多深、最老的还在跑多久"。
    queue: dict[str, Any] = Field(default_factory=dict)
    #: 🔑 D191：每条向量腿"建索引用的模型 vs 当前配置"。三态：
    #: `matched=True` 一致 / `False` 不一致（dense 腿会降级）/ `None` 未知（空表）。
    #: ⚠️ 与 `models`/`queue` 同一条理由，**只在鉴权版里有值**。
    index_models: dict[str, Any] = Field(default_factory=dict)
    version: str = "0.1.0"


__all__ = [
    # 常量
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_REQUEST",
    "MAX_PAGES",
    "MAX_SCAN_PAGES",
    # 字面量类型
    "DocStatus",
    "SearchMode",
    "SourceKind",
    "Visibility",
    "ChunkStrategy",
    # 请求
    "AddMemberReq",
    "AskReq",
    "CreateKbReq",
    "CreateKeyReq",
    "CreateKeyResp",
    "CreateSessionReq",
    "CreateWorkspaceReq",
    "CrossKbSearchReq",
    "CreateUserReq",
    "ImportChatReq",
    "ImportDirReq",
    "LoginReq",
    "RegisterReq",
    "SearchReq",
    "UpdateKbReq",
    "UpdateMeReq",
    "UpdateSessionReq",
    "UpdateUserReq",
    "UpdateWorkspaceReq",
    # 响应
    "AdminUserOut",
    "ApiKeyOut",
    "AskDone",
    "ChunkOut",
    "DocumentDetail",
    "DocumentOut",
    "HealthResp",
    "HighlightSpan",
    "ImportResp",
    "JobOut",
    "KbDetail",
    "KbOut",
    "LoginResp",
    "MemberOut",
    "MessageOut",
    "PageMeta",
    "SearchHit",
    "SearchResp",
    "SessionDetail",
    "SessionOut",
    "SourceItem",
    "UploadResp",
    "UserOut",
    "WorkspaceOut",
]


#: 检索/文档接口给调用方的正文截断长度。
#:
#: ⚠️ 别想着"全给"：契约允许 `top_k=100`，`chunk_size` 可配到 2000，
#: 全给就是几百 KB 的响应，纯属浪费。800 字够渲染一张卡片、
#: 也够 MCP / A2A 做引用摘要；要看全文时调用方还有 `GET /documents/{id}/chunks`。
#: （生成层的证据上限是 400，见 `retrieval/ask.py::MAX_EVIDENCE_CHARS`。）
#:
#: 🔑 住在 `common/` 而不是 `api/routers/search.py`：MCP 的工具也要用同一个上限，
#: 而从 `services/mcp` 去 import `services/api` 是**反向依赖**（§0.1 的分层）。
SEARCH_TEXT_CHARS = 800
