# 数据模型与存储

> 本文只写**现在是什么**。设计理由见 [项目深度拆解](./deep-dive.md) 与 [架构总览](./architecture.md)。

---

## 1. 存储拓扑

```
PostgreSQL 16 (pgvector/pgvector:pg16)
  真相源：用户 / 工作区 / 知识库 / 文档 / chunk 元数据
          任务队列 / 审计日志 / 全文子串索引(trgm) / 会话与消息

Qdrant 1.15.1（standalone 二进制）
  kb_dense   dense 512d      bge-small-zh-v1.5
  kb_sparse  sparse         jieba + BM25
  kb_visual  multi 128d     colSmol-500M（max_sim）

本地文件系统
  data/blobs/{sha[:2]}/{sha}/original|preview.pdf|thumbs/
```

**为什么装了 pgvector 却不用**：全局统一一个嵌入模型 + Qdrant 是既有选型；pgvector 不支持 late-interaction 多向量。装 pgvector 只是为了镜像自带 `pg_trgm`。

---

## 2. PostgreSQL 表结构

### 2.1 身份与工作区

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         CITEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,              -- argon2
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user','superadmin')),  -- 全局角色
    nickname      TEXT UNIQUE,                -- 唯一昵称，可作登录名
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
    id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    owner_id BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), deleted_at TIMESTAMPTZ
);

CREATE TABLE workspace_members (
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner','member')),
    invited_by   BIGINT REFERENCES users(id),
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
```

> **role 只有两档**：暂不做完整 RBAC。⚠️ `users.role`（全局角色）与 `workspace_members.role`（owner/member）**不是一回事**。

邀请码表 `workspace_invites` 只存 **HMAC-SHA256 哈希 + 前 4 位**，不存明文；默认 7 天 / 1 次，`used <= max_uses` 由数据库 CHECK 约束。

### 2.2 知识库

```sql
CREATE TYPE kb_visibility AS ENUM ('private', 'workspace', 'global');
CREATE TYPE kb_source_kind AS ENUM ('file', 'chat', 'photo');

CREATE TABLE knowledge_bases (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL, description TEXT,
    visibility   kb_visibility  NOT NULL DEFAULT 'private',
    source_kind  kb_source_kind NOT NULL DEFAULT 'file',
    embed_model  TEXT NOT NULL DEFAULT 'BAAI/bge-small-zh-v1.5',  -- 全局统一
    chunk_size   INT  NOT NULL DEFAULT 500,
    chunk_overlap INT NOT NULL DEFAULT 50,
    chunk_strategy TEXT NOT NULL DEFAULT 'recursive',
    generate_image_desc BOOLEAN NOT NULL DEFAULT FALSE,
    rerank_model TEXT,                    -- 空 = 不启用精排
    pii_mask     BOOLEAN NOT NULL DEFAULT FALSE,
    created_by   BIGINT NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);
CREATE INDEX ON knowledge_bases (workspace_id) WHERE deleted_at IS NULL;
```

### 2.3 文档

```sql
CREATE TYPE doc_status AS ENUM
  ('pending','parsing','chunking','embedding','ready','failed','deleted');
CREATE TYPE doc_source AS ENUM ('upload','chat_import','photo_dir');

CREATE TABLE documents (
    id BIGSERIAL PRIMARY KEY,
    kb_id BIGINT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    file_name    TEXT NOT NULL,           -- 覆盖式更新，同一 (kb_id, file_name) 可有多个版本
    source_path  TEXT,                    -- 聊天 KB 专用：chat-2025-02-14
    content_hash TEXT NOT NULL,           -- sha256，判重
    blob_sha     TEXT NOT NULL REFERENCES blobs(sha256),
    mime_type    TEXT, size_bytes BIGINT,
    doc_source   doc_source NOT NULL DEFAULT 'upload',
    status       doc_status NOT NULL DEFAULT 'pending',
    status_msg   TEXT,                    -- 失败原因
    quality      JSONB,                   -- 三层校验报告
    is_current   BOOLEAN NOT NULL DEFAULT TRUE,
    version_id   INT NOT NULL DEFAULT 1,
    page_count   INT, chunk_count INT DEFAULT 0,
    scan_pages   INT[],                   -- 页级索引的 PG 真相来源（NULL=未知）
    pii_hits     JSONB,                   -- 摄取时掩码前记下
    taken_at     TIMESTAMPTZ,             -- 照片拍摄日期（EXIF/文件名，不猜）
    uploaded_by  BIGINT NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);
CREATE INDEX ON documents (kb_id) WHERE is_current AND deleted_at IS NULL;
CREATE INDEX ON documents (status) WHERE deleted_at IS NULL;
```

几个字段的语义容易记错：

| 字段 | 含义 | 常见误解 |
|---|---|---|
| `id` | 文档主键 | 不是 `doc_id`。`DocumentOut` 上有个 `doc_id` property，但 pydantic 不序列化普通 property |
| `version_id` | 同一 `(kb_id, file_name)` 的版本号 | 覆盖式更新 +1，不做完整版本历史 |
| `is_current` | 是否当前版本 | 检索只返回 `is_current` |
| `quality` | 三层校验报告 + `strategy_notes` | 扫描件的 `parse` 是 `skipped`，不是 `passed` |
| `scan_pages` | 哪些页走视觉腿 | 视觉腿对账的 PG 真相来源 |
| `pii_hits` | 掩码**前**的检出数 | 开了掩码后仍能回答“原文里有多少” |

### 2.4 Blob（内容寻址存储）

```sql
CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY, size_bytes BIGINT NOT NULL, mime_type TEXT,
    storage_path TEXT NOT NULL, ref_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**为什么不用 `ON DELETE CASCADE` 直接删文件**：同一份文件可能被多个文档版本引用（覆盖式更新后旧版本仍在回收站）。引用计数归零才物理删除。

> ⚠️ `storage_path` 这一列**不是存储抽象**：写入时是 `str(blob_path(sha256))`，没有任何代码读它做分发。要上对象存储时先做只读远端副本。

### 2.5 Chunk 元数据

```sql
CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content_norm TEXT NOT NULL,          -- 归一化文本（用于嵌入）
    char_start INT, char_end INT,        -- 在原文中的偏移
    locations JSONB,                     -- L3 坐标矩形数组，归一化 0~1
    token_count INT,
    kind TEXT NOT NULL DEFAULT 'text',   -- text | table | page | chat
    table_index INT, row_count INT, col_count INT,
    table_json  JSONB,                   -- 表格结构化网格 {header,rows,row_count,col_count}
    parent_text  TEXT,                   -- 父块上下文，**不进向量库**
    section_path TEXT,                   -- 章节路径 H1 > H2 > H3
    CONSTRAINT ck_chunk_kind CHECK (kind IN ('text','table','page','chat')),
    UNIQUE (doc_id, chunk_index)
);
CREATE INDEX ON chunks (doc_id);
CREATE INDEX chunks_trgm ON chunks USING gin (content_norm gin_trgm_ops);
```

实测依据：bbox 归一化后往返误差 **0.00e+00 point**，与 DPI 无关；正文 chunk 恰好 **1 个框**；表格 chunk 每非空单元格 1 个框（实测最多 **40**，带 `row`/`col`）；成本 8~9 ms/页。

> ⚠️ 画框时必须先把归一化坐标乘回 PDF point；表格框取自 `tbl.rows[i].cells`，不是扁平的 `tbl.cells`。

### 2.6 任务队列

```sql
CREATE TABLE jobs (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,                  -- ingest / embed_visual / thumbnail / pdf_preview
                                         -- cleanup / import_chat / import_dir / reindex
    doc_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    kb_id  BIGINT REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK (status IN ('pending','running','done','failed','cancelled')),
    progress REAL NOT NULL DEFAULT 0,
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    error TEXT, payload JSONB,
    locked_at TIMESTAMPTZ, locked_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX jobs_poll ON jobs (status, created_at) WHERE status = 'pending';
CREATE INDEX jobs_doc ON jobs (doc_id);
```

worker 领取协议（防重复消费）：

```sql
UPDATE jobs SET status='running', ...
WHERE id = (
  SELECT id FROM jobs
  WHERE status='pending'
    AND (locked_at IS NULL OR locked_at < now() - interval '5 min')
  ORDER BY created_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED
) RETURNING *;
```

> ⚠️ 这里**不用 ENUM**：autogenerate 不检测 ENUM / CHECK 的值变更，用 TEXT + CHECK 至少能让人看见差异。改任何 kind 都要在三处登记：`common/jobs.py` 的 `ALL_KINDS`、`worker/handlers/__init__.py` 的 `HANDLERS`、`models.py` 的 CHECK 约束 + 一条手写迁移。

### 2.7 认证与审计

```sql
CREATE TABLE api_keys (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,        -- 完整 sha256(明文)，不存明文
    key_prefix TEXT NOT NULL,             -- 前若干位，用于 UI 展示
    scopes TEXT[] NOT NULL DEFAULT '{kb:read,chat:read}',
    last_used_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    api_key_id BIGINT REFERENCES api_keys(id), user_id BIGINT REFERENCES users(id),
    action TEXT NOT NULL,       -- search | ask | mcp_call | a2a_call | upload ...
    kb_ids BIGINT[],            -- 审计三元组：key → user → kb
    detail JSONB,               -- query 摘要、结果数、耗时
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_user_time ON audit_log (user_id, created_at DESC);
CREATE INDEX audit_time ON audit_log (created_at DESC);
```

明文 API Key **只在创建时返回一次**。吊销是**删行**，没有 `revoked_at` 列。

### 2.8 外部 LLM 配置

```sql
CREATE TABLE llm_settings (
    id INT PRIMARY KEY DEFAULT 1,        -- 全局单例
    provider TEXT NOT NULL DEFAULT 'extractive'
             CHECK (provider IN ('extractive','openai_compat')),
    base_url TEXT, model TEXT,
    api_key TEXT,                        -- 明文（必须能发给上游）；读端点只回掩码
    extra_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    temperature REAL NOT NULL DEFAULT 0.3,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    use_for_ask BOOLEAN NOT NULL DEFAULT TRUE,
    use_for_contextual BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> **没有行 = 抽取式**（零外传），这正是隐私默认。迁移不插默认行。只有 `provider='openai_compat'` **且** `enabled` **且** base_url/model/api_key 齐全，生成层才真的外传。
>
> `extra_headers` 是接特殊网关的硬需求（例如某些 OpenAI 兼容网关要求自定义请求头，缺它 400）。

### 2.9 索引模型记录

```sql
CREATE TABLE index_models (
    collection    TEXT PRIMARY KEY,     -- kb_dense / kb_visual
    model         TEXT NOT NULL,
    dim           INT  NOT NULL,
    model_version TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> **为什么需要它**：索引与查询必须同模型，否则在两个向量空间里算相似度，排序基础整个失效且**不报错**。入库 / 重建时写、dense 腿查询前校验（不一致就 `skipped`）、`/health` 的 `index_models` 如实汇报。稀疏腿（BM25）是词法算法，不记录。

### 2.10 会话、聊天与语料统计

| 表 | 用途 | 关键点 |
|---|---|---|
| `chat_sessions` | 多轮会话 | 新增 `kb_ids`（检索范围，`null` = 全部有权限的库），`kb_id` 可空 |
| `chat_messages` | 消息历史 | 只存引用编号，不存来源身份 ⇒ 历史 `[n]` 点不出原文 |
| `chat_day_stats` | 聊天时间线的按天汇总 | 聊天 KB 的时间线视图 |
| `terms` / `corpus_stats` | BM25 词表与语料统计 | **全局量**；入库时加、删除时减，必须有统一减法入口 |

---

## 3. Qdrant 索引布局

### 3.1 三个 collection 而不是一个

Qdrant 的单个 collection **不支持混合 unnamed + named multi-vector**，而 `kb_visual` 用的是命名向量 `text` + `multivector_config`。

| collection | 向量类型 | 模型 | 承载 |
|---|---|---|---|
| `kb_dense` | dense 512d | bge-small-zh-v1.5 | 所有文本 chunk |
| `kb_sparse` | sparse | jieba BM25 | 同上，词元级精确 |
| `kb_visual` | multi 128d max_sim | colSmol-500M | 扫描件页 + 照片 |

三者的**点 ID 完全一致**（同一 chunk 同时有 dense 和 sparse），检索时并行查再 RRF 融合。一致性靠的就是“点 ID 由业务键算出来”；若退化成全局连续序号，三条腿就对不上了。

### 3.2 稳定点 ID

```python
def point_id(kb_id: int, doc_id: int, chunk_index: int) -> int:
    """blake2b-64 哈希，稳定可复算，删后可重建设不冲突"""
    h = hashlib.blake2b(f"{kb_id}\x00{doc_id}\x00{chunk_index}".encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big") >> 1   # 留出符号位
```

页级另有 `point_id_for_page(kb_id, doc_id, page_no)`（`kb_visual` 用的就是它，`chunk_index = -(page_no+1)`）。实测 10 万 chunk **0 冲突**。

**推论**：重建索引后同文档同 chunk 的 ID 不变 ⇒ 支持“临时删掉索引再用同一脚本重建”。

### 3.3 Payload schema

三个 collection **统一 payload**（便于 RRF 融合时对齐）：

`kb_id` / `doc_id` / `chunk_index` / `source_kind` / `owner_id` / `workspace_id` / `visibility` / `kind` / `page` / `embed_model` / `text_norm` / `text_raw` / `file_name` / `mime` / `locations`（+ `table_index` / `parent_text` / `section_path` / `updated_at`）。

必须建的 payload 索引（各 collection 共 6 个）：

- `kb_id`（keyword，最高优先级）
- `workspace_id`（keyword）
- `doc_id`（integer）
- `source_kind`（keyword）
- `visibility`（keyword）
- `updated_at`（datetime，时间过滤）

> ⚠️ `payload_schema` 在 GET 响应的**顶层**；建索引要 `?wait=true`。

### 3.4 别名模式与蓝绿重建

逻辑名（`kb_dense` / `kb_sparse` / `kb_visual`）在**别名模式**下是别名，真正存数据的是带代次后缀的物理集合。

| 模式 | 开关 | `kb_dense` 是 | 蓝绿重建 |
|---|---|---|---|
| 默认 | `QDRANT_ALIASES=false` | 真实集合（历史行为逐字不变） | ❌ |
| 别名 | `QDRANT_ALIASES=true` | 别名 → `kb_dense__<gen>` | ✅ |

```bash
# 一次性迁移（把现有真实集合复制成物理集合 + 别名）
uv run python deploy/bootstrap_index_aliases.py --yes

# 蓝绿重建（超管；建影子 → 验证 → 原子切别名 → 留旧回滚）
curl -XPOST '/api/v1/admin/reindex?swap=true' -H "Authorization: Bearer <超管 key>"
```

> ⚠️ 别名**不出现在 `GET /collections`**（实测）⇒ 巡检 / `/health` 列的是物理名；对账用逻辑名经别名查询，照常。
>
> ⚠️ 迁移与切换都有毫秒级窗口（删原集合 → 建别名），期间该腿读 404 ⇒ 检索**降级 `skipped`**，不 500。
>
> ⚠️ 视觉腿**不重建**：它要重读原件 + colSmol 前向，那是“重新摄取”。影子重建只覆盖 dense + sparse。

---

## 4. 级联删除与一致性

**一致性原则**：PostgreSQL 是真相源，Qdrant 是可重建派生索引。

```
删除文档：
  1. PG: documents.deleted_at = now(), is_current = FALSE   ← 真相源，此刻生效
  2. 删 blob（仅当 ref_count 归零）
  3. 入队 cleanup 任务 → 删 Qdrant 三 collection 的对应点
  4. 30 天后（cleanup）物理清理文档行 + chunk 行
```

**允许 Qdrant 短暂不一致**（检索时以 PG 的 `is_current` 为准二次过滤）。**绝不允许 PostgreSQL 有而 Qdrant 无**（那才是真丢失）。

索引对账任务在 worker 空闲时定期跑（`common/consistency.py`，只报告不自动修），另有：

- `GET /admin/consistency`（按需）；
- `deploy/monitor.py`（要人跑）。

对账按腿分别报告：

| 报告项 | 含义 | 该做什么 |
|---|---|---|
| `legs.*.missing` | PG 有、这条腿没有 | 漏索引 → 文本腿 `KIND_REINDEX` **能**修；视觉腿要 `reparse` |
| `legs.*.dangling` | 这条腿有、PG 没有 | 残留点 → reindex **修不了**，要重跑摄取或软删 |

> 顶层汇总口径会掩盖“只有一条腿坏了”——**不要只看顶层**。

---

## 5. 迁移与真实数据

迁移原则：**保留原始数据源，向量全部重建**。原始文件在 `data/`，可以重跑：

- `data/chat_messages_clean.jsonl` → KB「聊天记录」（`source_kind=chat`）；
- `data/docs_light/*.md` → KB「我的文档」（需重跑 privacy 判定）；
- `photos_original/*.jpg` → KB「我的照片」。

**隐私复检**：迁移时必须重跑 `owner_classifier`，不能信任旧结果——判定规则被后续修复改动过多次。

> `data/` 不进 git；已跟踪文件里不许有真实信息。守卫在 `tests/test_no_real_data.py`：真实数据只从**不被追踪**的文件读（`eval/_qset.py` / `eval/_local_data.py` / 本地 denylist / `reports/`）。

---

## 6. 相关文档

- 各表如何被使用：[文档处理与切分](./ingestion.md) · [任务队列与进程模型](./jobs.md)
- 过滤条件与审计：[多租户与权限](./tenancy.md)
- 备份与恢复：[部署与运维](./operations.md)
