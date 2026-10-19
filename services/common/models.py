"""SQLAlchemy 2.0 ORM 模型 —— 对应 `DESIGN-DATA-MODEL.md` 的 9 张表。

约定：
- 全部用 `Mapped[...]` 声明式类型，不写 `Column()`
- 时间戳统一 `TIMESTAMPTZ`，服务端 `now()`
- 软删除字段 `deleted_at`，查询一律带 `deleted_at IS NULL`（除回收站视图外）
- 扩展（`citext` / `pg_trgm`）由 alembic 迁移创建，不在模型里声明
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


#: 所有时间列统一带时区（D8 数据模型约定）。
#: 不加 timezone 的 DateTime 存进去是"本地时间"，跨时区读会错位。
TIMESTAMP = DateTime(timezone=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ============================================================
# 2.1 身份与工作区
# ============================================================


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: 🔑 D173：**全局角色**。`user`（默认）| `superadmin`。
    #: ⚠️ 与工作区的 owner/member（`workspace_members.role`）**不是一回事**：
    #: 那个是"某个工作区里的身份"，这个是"整个实例的身份"。
    #: superadmin 越过三档可见性（读写所有库、管所有工作区、管用户、全局配置）。
    role: Mapped[str] = mapped_column(Text, default="user", nullable=False)
    #: 🔑 D174：**唯一昵称**，可作登录名（与 email 二选一登录）。
    #: 可空（加这一列之前的用户没有；唯一约束允许多个 NULL）。
    nickname: Mapped[str | None] = mapped_column(Text, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('user','superadmin')", name="ck_user_role"
        ),
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 注销账号 → 工作区随之删除。
    # 换句话说：**别把不想要的数据放进自己独资的工作区**，
    # 因为 workspace 级 CASCADE 会带走其下所有 KB / 文档 / 向量引用。
    # （D42 的软删除是给"删工作区"这个操作用户显式调用时走的；
    #   注销账号是硬删除，两者语义不同。）
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )
    # D42：工作区软删除，与 KB/文档同一套 30 天机制
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # D4：暂不做 RBAC，owner/member 两档足够表达"管理权 vs 访问权"
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # SET NULL：邀请人注销后保留成员关系，只丢"谁邀请的"这个信息
    invited_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    joined_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("role IN ('owner','member')", name="ck_wsm_role"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")


class WorkspaceInvite(Base):
    """工作区邀请码（D4 · M1）。

    🔑 为什么需要它：`POST /workspaces/{id}/members` 只能加**已注册**的邮箱，
    而 D4 明确**不接邮件服务** —— 于是"邀请一个还没注册的人"这条路本来就不通。
    邀请码把它变成自服务：owner 生成一段码 → 对方注册后自己输码加入。

    ## 为什么只存哈希 + 前缀（与 API Key 同一纪律，D64）

    数据库泄露时，存明文 = 所有工作区不设防。所以 `code_hash` 是
    HMAC-SHA256（`invites.hash_code`），`code_prefix` 只给人认。

    ## 失效的三种方式

    | 方式 | 字段 |
    |---|---|
    | 过期 | `expires_at` |
    | 用完 | `used >= max_uses` |
    | 被撤销 | `revoked_at` |

    ⚠️ **兑换时对三者给同一句话**（`REDEEM_FAIL`）：区分开等于给攻击者
    一个"这个码存在但失效了"的探测信号。
    """

    __tablename__ = "workspace_invites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: HMAC-SHA256（`salt$digest`），**不是明文**
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: 前 4 位，给人认是哪一段（🔴 绝不存/显示完整明文）
    code_prefix: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    #: 被邀请人的角色。`owner` 不允许 —— 邀请码是给成员用的，
    #: 否则"转发一个码"就能把整个工作区卖出去
    role: Mapped[str] = mapped_column(Text, nullable=False, default="member")
    max_uses: Mapped[int] = mapped_column(default=1, nullable=False)
    used: Mapped[int] = mapped_column(default=0, nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP,
        default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    #: 谁兑换了（一码一人时就是唯一那位）
    redeemed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    redeemed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)

    __table_args__ = (
        # role 只允许 member：邀请码不能授予 owner（D4 的 RBAC 只有两档）
        CheckConstraint("role = 'member'", name="ck_invite_role"),
        # 🔑 used 不能超过 max_uses。**数据库层**兜住"并发兑换两次" ——
        #   应用层的 `used >= max_uses` 检查在两个请求同时进来时会双双通过
        #   （都是先读 used=0 再写 1）。
        CheckConstraint("used <= max_uses", name="ck_invite_used"),
        CheckConstraint("max_uses BETWEEN 1 AND 50", name="ck_invite_max_uses"),
        # 🔑 这个唯一约束**库里已经有**（D120 的迁移建了它），但模型漏了声明
        #   —— 于是 `alembic check` 报「要**删**掉 uq_invite_ws_prefix」
        #   （D128 修的就是这个）。
        #   ⚠️ **方向很重要**：照 autogenerate 的删除走，同一个工作区就能有
        #   两个同前缀的邀请码，而兑换是"按前缀找那条"—— 会拿错那条，
        #   症状是"输入我自己的码提示已被使用"。
        #   `code_prefix` 只有 4 位（生成时保证工作区内唯一），所以这个
        #   约束是那条保证的**兜底**，删掉它兜底就没了。
        UniqueConstraint("workspace_id", "code_prefix", name="uq_invite_ws_prefix"),
    )


# ============================================================
# 2.2 知识库
# ============================================================


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        Text, default="private", nullable=False
    )
    source_kind: Mapped[str] = mapped_column(Text, default="file", nullable=False)
    embed_model: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    chunk_overlap: Mapped[int] = mapped_column(
        Integer, default=50, nullable=False
    )
    # D164：切分策略。**默认 recursive = 现行为**，见 `ingest/strategies.py`。
    # ⚠️ CHECK 值变更 alembic autogenerate **不检测**（PROGRESS §5.26），
    #   加/改值要手写迁移。这里的取值与 `strategies.STRATEGY_NAMES` 必须一致
    #   （由 `tests/test_strategies.py` 的守卫钉住）。
    chunk_strategy: Mapped[str] = mapped_column(
        Text, default="recursive", nullable=False
    )
    # D31：图片内容描述可选开关
    # 阶段 3：per-KB 的 Reranker。⚠️ NULL = 不启用（**不是**空串）——
    # 空串会与"全局默认模型"混淆，而这两件事的业务含义完全不同。
    rerank_model: Mapped[str | None] = mapped_column(Text)
    generate_image_desc: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # D121：运行时 PII 掩码开关，🔴 **默认关闭**。
    # 个人知识库的核心用途就是"搜我自己的手机号/身份证号"，
    # 默认掩码等于把这个用途关掉（而且不可逆：原文只在 blob 里，
    # 重新摄取才能拿回可搜的形式）。
    # 开关是**按库**的：自己的日记库保持原文，要发给别人的证件库才开。
    # ⚠️ 只作用于**索引侧**（chunk 文本），原件/预览/下载仍是原文。
    pii_mask: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 可空 + SET NULL：库里可能放着别人建的公共库（D43）。
    # 那个创建者注销后，KB 必须留下、仍可访问，不能被外键挡住。
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, onupdate=utcnow, nullable=False
    )
    # D26：软删除 30 天
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)

    documents: Mapped[list[Document]] = relationship(
        back_populates="kb", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('private','workspace','global')", name="ck_kb_vis"
        ),
        CheckConstraint(
            "source_kind IN ('file','chat','photo')", name="ck_kb_kind"
        ),
        CheckConstraint(
            "chunk_strategy IN ('fixed','recursive','semantic',"
            "'parent_child','late','contextual')",
            name="ck_kb_chunk_strategy",
        ),
        Index("ix_kb_ws_alive", "workspace_id", postgresql_where="deleted_at IS NULL"),
    )


# ============================================================
# 2.3 文档
# ============================================================


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 聊天 KB 专用，如 chat-2025-02-14
    source_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    blob_sha: Mapped[str] = mapped_column(
        ForeignKey("blobs.sha256"), nullable=False
    )
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    doc_source: Mapped[str] = mapped_column(Text, default="upload", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    # D38：降级失败时记录原因
    status_msg: Mapped[str | None] = mapped_column(Text)
    # D28：覆盖式更新。同一 (kb_id, file_name) 可有多个版本，
    # 只有 is_current=TRUE 参与检索
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: D192：同一 `(kb_id, file_name)` 的**版本号**，从 1 起、每次覆盖式更新 +1。
    #: `is_current` 回答"哪一版在检索里"，`version_id` 回答"这是第几版" ——
    #: 两个问题不同：审计 / 历史追踪 / 将来判"事件新旧"都需要后者
    #: （文章「版本控制」一条：版本号配合 content_hash 追踪变更历史，也方便回滚）。
    version_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 是否扫描件（无文本层）。决定要不要走 colSmol 视觉路径（D37/D60）——
    #: 这个判断在解析阶段做一次就够，之后每次看文档状态都不必重新开 PDF。
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 🔑 **要走视觉路径的页码**（D124）。`[]` = 纯文本，`[1]` = 单张图片，
    #: `[3, 7, 11]` = 混合 PDF 里那三页没有文本层。
    #:
    #: ⚠️ 为什么必须落库（`is_scanned: bool` 不够）：它是**页级**索引
    #:   （`kb_visual`）在 PG 侧唯一的真相来源。没有它就无法对账 ——
    #:   `/admin/consistency` 只能比 chunk 级点，视觉腿于是长期没人管，
    #:   "0 个点"看起来像"库里没照片"，而真实原因视觉腿一直是坏的（D123）。
    #:   `is_scanned` 只说"这份要编码"，说不出"该有几个点"。
    #:
    #: ⚠️ NULL = 这份文档是加这一列之前摄取的，页码无从得知 ——
    #:   对账必须**跳过**它（而不是当成"应该有 0 个点"），否则会报一堆假残留。
    scan_pages: Mapped[list[int] | None] = mapped_column(JSONB)

    #: 🔑 **事件日期**（D20 时间线/照片墙/纪念日的数据源）。
    #:
    #: ⚠️ 不能用 `created_at`：那是"导入时间"。23 张照片一次导入完，
    #:   `created_at` 全是同一时刻 → 时间线退化成"某一天 23 张"，
    #:   而真实情况是横跨两年。导入时从 EXIF / 文件名抽，抽不到留空
    #:   （**不猜** —— 猜错的时间线比没有时间线更坏）。
    # D20：EXIF → 文件名 → **留空不猜**（时间线的日期来源）
    taken_at: Mapped[dt.date | None] = mapped_column(Date)
    # D121：摄取时**掩码前**检出的 PII 计数 `{"total": n, "by_type": {...}}`。
    # 🔑 存"原始内容里有几处"而不是"索引里还剩几处"：开掩码后索引里
    #   当然是 0，但用户真正想知道的是"我的原文里有多少敏感信息"。
    #   NULL = 没跑过检测（未摄取/纯扫描件）—— 与 `{}`（跑了、零命中）
    #   是两回事，报告里要能区分。
    pii_hits: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # D166：分层校验报告（格式/解析/Chunking 三层）+ 切分策略的降级 note。
    # NULL = 加这一列之前摄取的（未体检）；`{}` = 跑了、无问题。
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # SET NULL：上传者注销后文档不能被卡住删不掉（他可能在别人的公共库里传过文件）
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, onupdate=utcnow, nullable=False
    )
    # D26：回收站，30 天后物理清理
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="doc", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','parsing','chunking','embedding','ready','failed','deleted')",
            name="ck_doc_status",
        ),
        CheckConstraint(
            "doc_source IN ('upload','chat_import','photo_dir')",
            name="ck_doc_source",
        ),
        # 只索引活跃文档：列表页按 kb_id 查 is_current=TRUE
        Index(
            "ix_doc_kb_current",
            "kb_id",
            postgresql_where="is_current AND deleted_at IS NULL",
        ),
        Index(
            "ix_doc_status_alive",
            "status",
            postgresql_where="deleted_at IS NULL",
        ),
    )


# ============================================================
# 2.4 Blob（内容寻址存储）
# ============================================================


class Blob(Base):
    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 引用计数：同一文件可能被多个文档版本引用（D28 覆盖式更新），
    # 归零才物理删除
    ref_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )


# ============================================================
# 2.5 Chunk 元数据
# ============================================================


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # D35：归一化文本（嵌入用）
    content_norm: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    # D33 L3：坐标矩形数组。一个 chunk 含多个文本块 → 多框高亮
    # 形状 [{"page": 1, "bbox": [x0, y0, x1, y1]}]，坐标已归一化到 0~1
    locations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    token_count: Mapped[int | None] = mapped_column(Integer)

    # ── D58（L3 预研后新增）──
    # 正文 chunk 是单框；表格 chunk 是多框且带 row/col。
    # 前端靠这两个字段把多框排成网格，否则表格的 40 个框会糊成一片。
    kind: Mapped[str] = mapped_column(Text, default="text", nullable=False)
    # text(正文段落) | table(整张表) | page(扫描件，无坐标只高亮整页) | chat
    table_index: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    col_count: Mapped[int | None] = mapped_column(Integer)
    #: D203：表格的**结构化**网格（`{"header": [...], "rows": [[...]]}`）。
    #: `content_norm` 仍是 Markdown（人读），这一份是给前端渲染真表格 / 程序化读取。
    table_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # ── D165 Parent-Child / 层级元数据 ──
    #: 父块上下文（章节 / PDF 整页）。**不进向量库**，只随 payload 回传给生成端。
    parent_text: Mapped[str | None] = mapped_column(Text)
    #: 章节路径 `H1 > H2 > H3`（无标题的文档为空）。
    section_path: Mapped[str | None] = mapped_column(Text)

    doc: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index", name="uq_chunk_doc_idx"),
        CheckConstraint(
            "kind IN ('text','table','page','chat')", name="ck_chunk_kind"
        ),
        Index("ix_chunk_doc", "doc_id"),
        # D17/D40：trgm 子串索引。Postgres 内置分词对中文无效，
        # 所以这个索引**只**负责子串/模糊匹配（LIKE '%1381234%'、similarity()），
        # 词元级 BM25 在 Qdrant 的 kb_sparse 里。
        # gin_trgm_ops 支持 LIKE 前缀通配，不能用普通 btree。
        Index(
            "ix_chunk_trgm",
            "content_norm",
            postgresql_using="gin",
            postgresql_ops={"content_norm": "gin_trgm_ops"},
        ),
    )


# ============================================================
# 2.7 任务队列（D14）
# ============================================================


class ChatDayStat(Base):
    """`(kb_id, day)` → 当天的聊天聚合（D20 时间线的数据源）。

    🔑 为什么要有这张表：聊天消息的结构化字段（`date`/`name`/`type`）
    **只存在于导入那一刻的 jsonl 行里**。导入后文档是渲染出来的 markdown，
    只剩人类可读的 `[2024-07-21 20:45:02] 名字：内容`。
    查询时用正则从 markdown 里抽日期的话，改一次渲染格式时间线就整体坏掉，
    而且**不报错**（只是数字悄悄变少）。

    ⚠️ 也不能读 `data/timeline_daily.json`：那是单机离线产物的全局文件，
    里面是某一个人的全部聊天 —— 多租户服务读它等于把一个人的数据发给所有人。

    复合主键 `(kb_id, day)`：重复导入走 UPSERT **累加**，
    而不是插第二行（否则时间线上"同一天两行"）。
    """

    __tablename__ = "chat_day_stats"
    __table_args__ = (
        # 🔑 **不要**在这里再声明 `UniqueConstraint("kb_id", "day")`（D128）：
        #   `(kb_id, day)` 已经是**复合主键**（见下面的 `primary_key=True`），
        #   主键本身就有唯一性 —— 再声明一个同列的 UniqueConstraint
        #   不会给数据库加第二道保险，只会让 `alembic check` 报
        #   「要加一个 uq_chat_day_stat」。
        #   症状是**迁移与模型漂移**，而它红着的时候没人说得清
        #   "是该加约束还是该改模型"：照 autogenerate 生成的迁移
        #   会**多加一个重复索引**。
        #
        # 时间线只查"一个库 + 一个时间区间"，这个复合索引就是为它建的。
        # ⚠️ 它的列与主键完全相同，**不是**重复：主键服务的是 UPSERT 的
        #   冲突键，而这条索引服务的是"按 kb_id 过滤 + day 区间扫描"
        #   （时间线不指定区间时按月查）。留着它，意图才写在 schema 里。
        Index("ix_chat_day_stat_kb_day", "kb_id", "day"),
    )

    # 🔑 复合主键要在这里标出来（SQLAlchemy 靠 `primary_key=True` 认主键，
    #   光有 UniqueConstraint 它不认 → "could not assemble any primary key"）。
    #   这也是 UPSERT 累加的键：同一天重复导入时合并而不是插第二行。
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: `{消息类型: 条数}`（text/recall/voice/image/…）
    types: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: `{说话人: 条数}`
    speakers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    #: 当天最长的几条消息摘要（**截断**，全量存会把行撑大）
    highlights: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    #: 当天落了几份文档（切 50 条一个文档时，一天可能跨多个文档）
    docs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ⚠️ 与其它表一致用 Python 侧的 `utcnow` 默认值（模型顶部 `utcnow`），
    #   而不是 `server_default=func.now()` —— 后者在这套模型里没被用过，
    #   两套写法混着会让"这行的 created_at 到底谁给的"没人说得清。
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP, default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    kb_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # 如 embed_visual 的 {"page_from": 1, "page_to": 5}
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # worker 租约：locked_at 超时（5 分钟）视为持有者已死，可被重新领取
    locked_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    locked_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('ingest','embed_visual','thumbnail','pdf_preview',"
            "'cleanup','import_chat','import_dir','reindex')",
            name="ck_job_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','done','failed','cancelled')",
            name="ck_job_status",
        ),
        # 轮询热路径：只索引待领取的
        Index(
            "ix_jobs_poll",
            "status",
            "created_at",
            postgresql_where="status = 'pending'",
        ),
        Index("ix_jobs_doc", "doc_id"),
    )


# ============================================================
# 2.8 认证
# ============================================================


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 只存 sha256(明文)，明文仅创建时返回一次
    key_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: ["kb:read", "chat:read"], nullable=False
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )


# ============================================================
# 2.9 审计（M8）
# ============================================================


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ⚠️ 这两个外键是 SET NULL 而不是 CASCADE，理由很重要：
    # 审计的意义是"主体消失后仍可追溯"。若跟随级联删除，
    # `DELETE FROM users` 会被外键挡住（api_keys 是级联删的，
    # audit_log 引用它 → 违反外键约束，整个删除失败），
    # 就算不挡，删用户就等于抹掉他的痕迹，审计形同虚设。
    # 留空后 detail 里仍有 query 摘要、耗时、kb_ids。
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # D8：审计三元组 key → user → kb
    kb_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, nullable=False
    )
    # query 摘要、结果数、耗时
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )

    __table_args__ = (
        # M8：审计表会持续增长（每次检索一条），
        # 建完索引后要定期归档，见 PROGRESS.md
        Index("ix_audit_user_time", "user_id", created_at.desc()),
        Index("ix_audit_time", created_at.desc()),
    )


# ============================================================
# 2.11 BM25 词表（增量入库用）
# ============================================================
#
# 为什么需要这张表：旧的 BM25 是**全量重建**的（跑一遍全语料生成词表 + idf，
# 落盘成 data/bm25_model.json）。但产品化后文档是**增量**入库的 ——
# 传一份新文件就重建全库词表显然不可行。
#
# 这张表把词表搬进 PG：
#   terms.id       稳定的 term_id，直接当 Qdrant 稀疏向量的分量下标
#   terms.df       文档频率，算 idf 用
#   corpus_stats   全局文档数与平均长度（BM25 的 b 参数需要）
#
# idf 会随入库漂移（新词让旧词的 idf 偏高）。这是 BM25 增量化的固有折中，
# 修法是定期跑 `reindex` 任务重算 —— jobs 表已有 KIND_REINDEX。
#
# ⚠️ 词表是**全局**的（不分租户），因为三个 KB 共用 `kb_sparse` 一个 collection，
# 稀疏向量的下标空间必须全局一致。副作用：一个罕见词在别的 KB 出现过，
# 这边的 idf 会被拉低 —— 只泄漏统计量，不泄漏内容。


class Term(Base):
    __tablename__ = "terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    #: 文档频率（多少个 chunk 包含过这个词）
    df: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, default=utcnow, nullable=False)


class CorpusStat(Base):
    """BM25 的全局统计量，单行（`id=1`）。

    为什么用行存而不是 KV 字符串：需要 `SELECT ... FOR UPDATE` 做原子自增，
    行锁比应用层加锁可靠（多 worker 场景）。
    """

    __tablename__ = "corpus_stats"

    #: 固定为 1，单行
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: 总 chunk 数（BM25 的 N）
    n_docs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 全部 chunk 的 token 总数（算 avgdl）
    total_len: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, default=utcnow, nullable=False)


class IndexModel(Base):
    """🔑 每条向量腿**实际用于建索引**的嵌入模型记录（D191）。

    ## 为什么需要它（文章「为什么 Embedding 模型必须保持一致」）

    索引时用的 Embedding 模型必须与查询时一致，否则等于在两个不同的向量
    空间里算相似度 —— 排序基础整个失效，而且**不报错**（最坏的失败模式）。

    本项目全局只有一个文本模型（D22：`BAAI/bge-small-zh-v1.5`），但它是
    **可被配置改的**（`.env` 的 `text_embed_model` / `text_embed_dim`）。
    一旦改了配置却没重建索引，dense 腿就会拿"新模型的查询向量"去比
    "旧模型建的库向量"。所以把"谁建的索引"落成元数据：

    | 时机 | 动作 |
    |---|---|
    | 入库 / 重建 | `index_models.record_model()` 写下当前配置的模型 |
    | 查询（dense 腿） | 与当前配置比对，不一致就 `skipped`（降级，不返回垃圾）|
    | `/health` · 巡检 | 如实汇报"建索引用的模型 vs 当前配置" |

    ## 为什么三个 collection 各一行

    文本腿是 dense（有模型）+ sparse（BM25，词法、无模型，**不记录**）；
    视觉腿是 colSmol（有模型）—— 换 colSmol 同样要被校验，所以一并记录。
    主键就是 collection 名，行数恒为个位数。

    ⚠️ `model_version` 可空：SentenceTransformer 的本地快照没有可读的 revision，
    留空表示"不知道"，**不要**拿模型名冒充版本（那会让"换了版本"永远测不出来）。
    """

    __tablename__ = "index_models"

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    #: 建索引时用的模型名（如 `BAAI/bge-small-zh-v1.5`）
    model: Mapped[str] = mapped_column(Text, nullable=False)
    #: 向量维度。维度不同时向量库**根本无法比对**，所以单独记一列。
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 模型版本（可选）。NULL = 不知道。
    model_version: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP, default=utcnow, onupdate=utcnow, nullable=False
    )


# ============================================================
# 2.10 会话（D7 多轮问答）
# ============================================================


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: D163：多库问答后 `kb_id` 变成**可空**（多库会话没有唯一 kb）。
    #: 单库会话仍然填它（向后兼容），多库会话填 `kb_ids`。
    kb_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True
    )
    #: D163：这次会话的检索范围（库 id 列表）。`None` = 全部有权限的库。
    kb_ids: Mapped[list[int] | None] = mapped_column(JSONB)
    # 问答记录是个人私产，用户没了就跟着没
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_session_kb", "kb_id"),)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 引用的 chunk id 列表，便于回看当时依据了什么
    citations: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    created_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP, 
        default=utcnow, nullable=False
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="ck_msg_role"),
        Index("ix_msg_session", "session_id", "id"),
    )


# ============================================================
# 外部 LLM 配置（D170）—— 全局单例，页面可配
# ============================================================

class LlmSettings(Base):
    """外部 LLM（OpenAI 兼容网关）的**全局单例**配置。

    ## 为什么落库、为什么不写死

    生成层原来只读环境变量（`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`）——
    改一次要重启进程、改 `.env`，用户没法自己配。D170 把它搬进库：
    **页面上配，立即生效**，env 只作为"库里没有行时的引导默认"。

    ## 🔴 隐私前提没有变

    `provider='extractive'`（默认）仍是**零外传**的抽取式。只有显式切到
    `openai_compat` 且填齐 base_url/model/api_key，检索出的原文才会离开本机。
    所以本表**默认没有行**（= 抽取式），要外部 LLM 必须由人来配。

    ## ⚠️ 单例（`id` 恒为 1）

    这是**实例级**配置，不是按用户/按库：一个实例一个外部模型。
    好处是 `/ask` 与摄取的 contextual 切分共用同一份，行为一致；
    代价是**多用户实例上一个人配的 key 所有人都在用** ——
    本项目定位是个人/小团队单机实例（D8/D23），已知并接受。

    ## ⚠️ `api_key` 是**明文**

    它必须能原样发给上游，所以不能哈希。因此：
    - 只有管理端点能读写它，**读时一律返回掩码**（`LlmConfigOut`）；
    - 它**绝不出现在审计 detail、日志或响应正文**里。

    ## `extra_headers`：自定义请求头

    有些网关要求特殊头（如 `HTTP-Referer`、`X-Title`、厂商自定义头）。
    这里存 `{header: value}`，发请求时与 `Authorization` 合并 ——
    这样"接入一个新的 OpenAI 兼容网关"**不需要改代码**。
    """

    __tablename__ = "llm_settings"

    #: 单例：永远 1。用主键约束保证"最多一行"。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: extractive（默认，不外传）| openai_compat（OpenAI 协议网关）
    provider: Mapped[str] = mapped_column(
        Text, default="extractive", nullable=False
    )
    base_url: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    #: 🔴 明文（见 docstring）；读端点只回掩码
    api_key: Mapped[str | None] = mapped_column(Text)
    #: 自定义请求头（与 Authorization 合并）
    extra_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    temperature: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    #: 显式开关：即使 provider=openai_compat 也要它为真才真正外传
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 🔑 D171：**哪些功能**用外部 LLM（细粒度开关）。
    #: `usable()` 只回答"凭据齐不齐"；"这个功能要不要外传"由这两列决定。
    #: 默认都开（用户切到 openai_compat 通常就是想要它生成/增强）；
    #: 想"问答仍抽取式、只用它做 Contextual"就把 `use_for_ask` 关掉。
    use_for_ask: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    use_for_contextual: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: D196：查询改写（文章 §Query Rewrite：Multi-Query / HyDE）要不要走外部 LLM。
    #: 默认开（切到 openai_compat 通常就想要它增强检索）；想"问答用 LLM、检索
    #: 仍不额外外传检索 query"就把它关掉。
    use_for_rewrite: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('extractive','openai_compat')",
            name="ck_llm_provider",
        ),
    )
