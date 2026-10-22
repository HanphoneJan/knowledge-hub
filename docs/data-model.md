# 数据模型与一致性

Knowledge Hub 的数据分三处存：PostgreSQL、Qdrant 和本地文件系统。它们不是对等的三份副本，而是“一份真相 + 一份可重建索引 + 一份原件”。

| 存储 | 存什么 | 能不能重建 |
|---|---|---|
| PostgreSQL 16（pgvector 镜像） | 用户、工作区、知识库、文档、chunk 文本与坐标、任务、审计、会话、BM25 词表 | 不能，备份的核心 |
| Qdrant 1.15.1 | `kb_dense`、`kb_sparse`、`kb_visual` 三个集合 | 文本两腿能从 PG 重建；视觉腿要重新摄取 |
| 本地磁盘 | `data/blobs/{sha[:2]}/{sha}/original`、`preview.pdf`、`thumbs/` | 原件不能重建 |

镜像里虽然装了 pgvector，但向量没有放进 PostgreSQL：全局只用一个文本嵌入模型，Qdrant 是既有选型，而且 pgvector 不支持 late-interaction 多向量，与视觉腿冲突。装 pgvector 只是因为镜像自带 `pg_trgm`。

## PostgreSQL 的 18 张表怎么分组？

`services/common/models.py` 里定义了 18 张表，可以按职责分成五组：

| 组 | 表 | 关键约束 |
|---|---|---|
| 身份与工作区 | `users`、`workspaces`、`workspace_members`、`workspace_invites` | `users.role` 只有 `user` / `superadmin`；`workspace_members.role` 只有 `owner` / `member`，两者不是一回事 |
| 知识库与文档 | `knowledge_bases`、`documents`、`blobs`、`chunks` | `visibility` 三档；`source_kind` 三档；`chunk_strategy` 六档；`documents.status` 七档 |
| 任务与索引统计 | `jobs`、`terms`、`corpus_stats`、`index_models` | `jobs.kind` 八档；`terms` 与 `corpus_stats` 是**全局量**，不分租户 |
| 认证与审计 | `api_keys`、`audit_log` | 只存 Key 的完整 sha256；审计外键是 `SET NULL`，主体注销后记录仍在 |
| 会话与配置 | `chat_sessions`、`chat_messages`、`chat_day_stats`、`llm_settings` | 会话是个人私产；`llm_settings` 是单例（`id` 恒为 1） |

表结构用 `Mapped[...]` 声明，没有用数据库 ENUM，而是 `TEXT + CHECK`。原因是 alembic autogenerate 不检测 ENUM 或 CHECK 的值变更，用 TEXT 至少能让人看见差异。改任何枚举都要同步三处：模型里的 CHECK、`services/common/jobs.py` 的 `ALL_KINDS` 或对应常量、`services/worker/handlers/__init__.py` 的 `HANDLERS`，再加一条手写迁移。

几个默认值直接决定了检索行为：`knowledge_bases.chunk_size` 默认 500、`chunk_overlap` 默认 50、`chunk_strategy` 默认 `recursive`；`embed_model` 由全局配置写入，不能按库改；`pii_mask` 与 `generate_image_desc` 默认关闭；`rerank_model` 为 `NULL` 表示不启用精排。`llm_settings.temperature` 默认 0.3，`enabled` 默认 `false`。

`workspace_invites` 只存 HMAC-SHA256 哈希与前 4 位前缀，不存明文；`role` 只能是 `member`；`used <= max_uses` 由数据库 CHECK 兜住并发兑换。

## documents 的版本与状态字段怎么读？

同一份 `(kb_id, file_name)` 可以有多行，靠两个字段回答两个不同的问题：

- `is_current` 回答“哪一版参与检索”。检索只返回 `is_current=true AND deleted_at IS NULL`。
- `version_id` 回答“这是第几版”，从 1 起，每次同名覆盖 +1。它不提供完整历史，只提供版本号。

覆盖式更新时，旧版本被 `supersede`：`is_current=false`、`deleted_at` 置为当前时间、30 天后物理清理。新版本沿用同一个 `file_name`，内容哈希不同。

另外几个容易记错的字段：

| 字段 | 含义 | 注意 |
|---|---|---|
| `quality` | 三层校验报告与切分策略 note | `NULL` = 加这一列之前摄取的；`{}` = 跑了、无问题 |
| `scan_pages` | 哪些页要走视觉腿 | 视觉腿对账在 PG 侧的**唯一**真相来源；`NULL` = 未知，对账必须跳过 |
| `pii_hits` | 掩码**前**的 PII 计数 | 开了掩码后仍能回答“原文里有多少” |
| `taken_at` | 照片的事件日期 | 只从 EXIF 或文件名抽，抽不到留空，**不猜** |
| `uploaded_by` / `created_by` | 上传者 / 创建者 | 可空，外键 `SET NULL`，用户注销后资源仍能保留 |

文档状态有 7 档：`pending`、`parsing`、`chunking`、`embedding`、`ready`、`failed`、`deleted`。前四档是中间态，后三档是终态。任务状态与文档状态是两份状态，终态判定见[任务与运维](jobs-operations.md)。

## chunk 上为什么要存坐标和表格网格？

`chunks.content_norm` 是归一化文本，用于嵌入；`locations` 是 L3 引用坐标，形状为 `[{page, bbox, row?, col?}]`，`bbox` 已归一化到 0~1。

实测坐标往返误差为 `0.00e+00` point，与 DPI 无关；正文 chunk 恰好 1 个框；表格 chunk 每个非空单元格 1 个框，实测最多 40 个，带 `row` / `col`；抽取成本 8~9 ms/页。

表格除了 Markdown 文本，还落一份 `table_json`（`{header, rows, row_count, col_count}`）供前端渲染真表格，并在文本前加一句纯规则生成的描述（“N 行 × M 列；表头：…”）让“一览表”类查询能召回。父块上下文存 `parent_text`，章节路径存 `section_path`，两者都不进向量库，只随结果回传。

`chunks.content_norm` 上有一个 `gin_trgm_ops` 索引，但它只服务 `LIKE` / `similarity`；真正召回用的 `word_similarity` 用不上这个索引，走顺序扫描。这是刻意的取舍：换回 `ILIKE` 会静默漏召回。

画框时有两个必须遵守的约定：归一化坐标要乘回**当前渲染页图**的像素尺寸；表格框只能从 `tbl.rows[i].cells` 取，不能从扁平的 `tbl.cells` 取，否则合并单元格会让框错位。

## Qdrant 三集合与稳定点 ID

三个集合的向量形态不同，不能合并：

| 集合 | 向量 | 模型或算法 | 承载 |
|---|---|---|---|
| `kb_dense` | 匿名稠密 512 维，Cosine | `bge-small-zh-v1.5` | 所有文本 chunk |
| `kb_sparse` | 顶层 `sparse_vectors` 里的 `bm25` | jieba + 自实现 BM25 | 文本 chunk 的词元级匹配 |
| `kb_visual` | 命名向量 `text`，128 维，`max_sim` 多向量 | `colSmol-500M` | 扫描页与照片 |

同一 chunk 在 dense 与 sparse 里的点 ID 必须相同，RRF 才能把它们合并。ID 由业务键直接算出来：

```python
def point_id(kb_id: int, doc_id: int, chunk_index: int) -> int:
    raw = f"{kb_id}\x00{doc_id}\x00{chunk_index}".encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") >> 1
```

右移一位是为了避开 uint64 的最高位，兼容 Qdrant。页级点用 `point_id_for_page(kb_id, doc_id, page_no)`，实际是 `chunk_index = -(page_no + 1)`，与 chunk 级点落在不同空间。实测 10 万 chunk 零冲突。

用哈希而不是自增序号有两层收益：删掉索引后可以按同样规则重建出同样的 ID；trgm 腿没有 Qdrant 点，但能从 `(kb_id, doc_id, chunk_index)` 复算出同一个 ID，于是非向量存储的命中也能和向量腿融合。

三集合的 payload 形状一致：`kb_id`、`doc_id`、`chunk_index`、`workspace_id`、`visibility`、`source_kind`、`owner_id`、`file_name`、`mime`、`kind`、`page`、`embed_model`、`text_norm`、`text_raw`，以及可选的 `locations`、`table_index`、`parent_text`、`section_path`、`updated_at`。必须建索引的字段是 `kb_id`、`workspace_id`、`doc_id`、`source_kind`、`visibility`、`updated_at` 六个。

**只有 `kb_id` 参与租户过滤。** 其余字段随结果回传，供审计、展示与时间过滤。尤其 `visibility` 是摄取时写死的，改库可见性不会回写；一旦有人按它过滤，陈旧值会立刻变成越权。守卫在 `tests/test_qdrant_filter_scope.py`。

## 删除一条文档要经过哪六步？

![删除链的六步：减 df、删三条腿的点、删文档行、blob 引用计数减一、归零才删文件，审计保留](diagrams/delete-chain.svg)

软删除只改 PG 的两列：`deleted_at` 与 `is_current=false`，此刻对检索立即生效。物理删除由 30 天后的 `cleanup` 任务或手工清空回收站触发，顺序固定：

1. 把该文档 chunk 的词从 BM25 词表减回去（`terms.df` 与 `corpus_stats`）；
2. 删 Qdrant 三个集合里该文档的点；
3. 删 `documents` 行，`chunks` 与 `jobs` 靠 `ON DELETE CASCADE` 跟着走；
4. `blobs.ref_count` 减一；
5. 归零才物理删 blob 文件；
6. 审计日志**不删**，保留 180 天。

这条链只在 `services/ingest/purge.py` 实现一次，worker 的 `KIND_CLEANUP` 与 `DELETE /api/v1/trash` 共用。漏掉第 1 步的症状不是报错，而是 idf 被压平、检索质量悄悄变差；漏掉第 2 步则已删内容还能被召回。

库与工作区的物理清理也走这里，但只删容器行；它们下面的文档必须先被 `purge_documents` 收走。顺序反了，文档的 df 就永远减不回去。

## 对账与重建：漂移怎么发现、怎么修？

Qdrant 与 PG 允许短暂不一致，但“短暂”要有地方看。对账的判据在 `services/common/consistency.py`，按腿分别比较：

| 报告项 | 含义 | 该做什么 |
|---|---|---|
| `missing` | PG 有、这条腿没有 | 文本腿跑 `KIND_REINDEX` 能补；视觉腿要 `reparse` |
| `dangling` | 这条腿有、PG 没有 | 残留点；`reindex` 会清孤儿库，单文档残留要重跑摄取 |

`GET /admin/consistency` 返回具体 id，不是布尔值；worker 空闲时每 900 秒跑一次并只记日志；`deploy/monitor.py` 分三条腿各报一项。顶层汇总只覆盖文本腿，会掩盖“只有视觉腿坏了”。

重建有两条路：

- 默认：按库先删后建，正在重建的那个库短暂搜不到，其它库照常；
- 蓝绿：`QDRANT_ALIASES=true` 时逻辑名是别名，`POST /admin/reindex?swap=true` 先建影子集合、灌数据、验点数，再原子切别名，旧集合保留回滚。

视觉腿**不参与重建**。它要重读原件并跑 colSmol 前向，那是重新摄取，不是重建索引。重建时把 `kb_visual` 一起删掉会变成“删了建不回来”，比漂移更糟。

## 备份恢复与可重建边界

Qdrant 不备份，因为它可以从 PG 的 chunk 重建；备份只需保证 PostgreSQL 与 blobs 完整。`deploy/backup.sh` 用 `pg_dump -Fc` 导库，打包 blobs，并在 manifest 里记下每张表的行数、每个 blob 的 sha256 以及每个 collection 的期望点数。

`deploy/restore.sh --scratch` 把备份恢复到一个临时库，逐表比行数、逐个比 blob 的 sha256，对完再删掉临时库。恢复后必须重建索引，重建出的点数应等于 manifest 里的 `qdrant_expect`，那才是“索引可重建”的验收方式。

`data/` 不进 git；已跟踪文件里不许出现真实信息。守卫在 `tests/test_no_real_data.py`：真实问题集与探针数据只从被 gitignore 的文件读取。

## 相关文档

- 数据如何写入：[文档摄取](ingestion.md) 与[任务、进程与运维](jobs-operations.md)
- 过滤条件与审计：[多租户与权限](tenancy.md)
- 全局图景：[架构](architecture.md)
