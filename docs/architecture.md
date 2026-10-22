# 架构：三个进程、两个存储

Knowledge Hub 把办公文档、聊天导出和照片收进不同的知识库，解析、切分、嵌入之后提供带引用的检索与问答。它面向一台机器自部署，不追求多节点，也没有把每个模块拆成独立服务。

系统里只有三个应用进程，加两个存储：

![三个应用进程与两个存储：API 负责请求与检索编排，worker 轮询任务，visual_service 只做视觉编码；PostgreSQL 是真相源，Qdrant 是派生索引](diagrams/architecture-processes.svg)

| 进程 | 入口 | 监听 | 职责 |
|---|---|---|---|
| `api` | `services/api/main.py` | `8010` | REST 与 SSE、MCP `/mcp`、A2A `/a2a/tasks`、托管前端构建产物、检索编排 |
| `worker` | `services/worker/main.py` | 无 | 轮询 `jobs` 表，执行摄取、视觉编码、缩略图、预览转换、清理、导入与重建 |
| `visual_service` | `services/visual_service/main.py` | `8020`，只绑 `127.0.0.1` | 把查询文本或一页图编码成 colSmol 多向量 |

PostgreSQL 带 pgvector 与 pg_trgm，跑在 `pgvector/pgvector:pg16` 容器里，端口 `5432`；Qdrant 是 `qdrant/qdrant:v1.15.1`，端口 `6333`（REST）与 `6334`（gRPC）。两个存储都只监听回环地址。前端开发服务器另占 `5173`，生产构建产物由 API 进程直接托管。

## 为什么视觉编码要单独一个进程？

`colSmol-500M` 在 CPU 上的成本决定了这条进程边界：

| 指标 | 实测值 |
|---|---|
| 模型加载 | 12.8 ~ 15.0 s，常驻内存 +2.64 GB |
| 单张图编码 | 约 15 s，且与图片分辨率无关 |
| 查询文本编码 | 119 ~ 128 ms/条，p95 157 ms |
| 6 路并发 | 墙钟 0.96 s，消耗 CPU 8.36 s，并行度约 8.7 倍 |

如果它和 API 同进程，用户在文档处理期间提问，编码会把核占满，API 长时间没有响应；2C4G 的机器还会直接 OOM。因此有一条硬约束：**API 进程不得加载视觉编码模型**。`services/retrieval/pool.py` 里的 `get_visual_model()` 在未显式调用 `enable_visual(True)` 的进程里会主动抛错，而不是悄悄退化成空向量。

代价是 API 自己用不了视觉腿。解决办法不是放宽约束，而是增加第三个进程承担**读侧**编码：API 通过 HTTP 调 `visual_service` 拿查询向量。

这条线是刻意断开的。`visual_service` 只做编码，**不连 Qdrant，也不做租户判定**。租户过滤的入口只有 `services/common/auth.py`，复制进新进程就多了一份迟早漂移的实现，而漂移的方向是越权。视觉腿的查询因此分成三步：

| 步骤 | 在哪跑 | 理由 |
|---|---|---|
| 查询文本 → 多向量 | 本进程或远端 `visual_service` | 只有这步需要重模型 |
| 构造租户过滤条件 | 始终在 API 进程 | 过滤唯一入口，不能复制 |
| Qdrant MaxSim 查询 | 始终在 API 进程 | 同上 |

守卫在 `tests/test_visual_service.py`：视觉服务里不得出现 Qdrant 或 auth 相关代码。

写侧默认仍由 worker 本地编码。多 worker 部署可以打开 `VISUAL_WRITE_VIA_SERVICE=true`，让 worker 调同一个常驻服务，这样每个 worker 不再各占 2.65 GB；代价是摄取从此依赖视觉服务可用。

单机 15 GB 内存下的实测预算大致是这样：

| 进程 | 常驻 | 峰值 |
|---|---|---|
| `api` | 约 400 MB | 约 600 MB |
| `worker` | 约 200 MB（未编码） | 约 2.8 GB（本地加载视觉模型后） |
| `visual_service` | 2.71 GB | 约 2.8 GB |
| PostgreSQL | 约 200 MB | 约 500 MB |
| Qdrant | 约 300 MB | 约 800 MB |

worker 与 `visual_service` 各自要 2.65 GB，同机峰值约 6 GB。15 GB 够用，但 2C4G 的小机器上必须二选一。走远端写侧时 worker 实测回落到约 991 MB。

## 一次上传要经过哪些步骤？

![上传与摄取链路：同步校验后落 blob 与 documents 行，worker 解析、切分、校验、嵌入，再派生视觉与预览任务](diagrams/ingest-pipeline.svg)

上传端点先做能在请求里判定的检查：扩展名白名单、单文件不超过 50 MB、单请求不超过 20 个文件、文件头与扩展名是否明显不符。通过后按内容哈希落盘，登记 blob 引用计数，写一条 `documents` 行（`status=pending`），再入队一个 `ingest` 任务。

worker 用 `FOR UPDATE SKIP LOCKED` 领取任务，然后在 `services/ingest/pipeline.py` 里走完解析、切分、校验、嵌入和写索引。有文本层的文档在文本腿写完后置 `ready`；扫描页和照片会派生 `embed_visual` 任务，每页一个。纯扫描件与照片没有文本腿，会停在 `pending`，等这些任务全部到达终态后，再由 `services/worker/doc_state.py` 按页级点数决定 `ready` 还是 `failed`；有文本层的混合 PDF 不会因为视觉页还没齐而回退状态。细节见[文档摄取](ingestion.md)。

## 一次检索要经过哪些步骤？

![检索链路：四条腿各自召回，RRF 只按排名融合，再经过软删过滤、查询路由偏好与可选精排，最后组装证据](diagrams/retrieval-flow.svg)

请求先经过 API Key 鉴权，得到 `UserContext.readable_kb_ids`。查询路由判断意图（照片、扫描件、表格、聊天、日期），置信度低于 0.35 就不路由。随后四条腿并发召回：

- `dense`：`bge-small-zh-v1.5` 编码查询，查 `kb_dense`；
- `sparse`：jieba 分词后在 PostgreSQL 词表里算 BM25 权重，查 `kb_sparse`；
- `trgm`：PostgreSQL 的 `word_similarity`，补编号、错误码这类非词字符串；
- `visual`：查询文本编码后查 `kb_visual`，扫描页与照片靠它。

融合用 RRF，只使用每条腿里的排名，不使用分数。融合后先挡掉 PG 里已软删或非当前版的文档，再按路由做软偏好过滤，最后按知识库配置决定是否精排。问答则在检索之后组装证据、流式生成并校验引用。细节见[检索与问答](retrieval.md)。

并发这里要说得准确：四条腿是真并发。三条 DB 腿各自从会话工厂拿一个 `AsyncSession`，各自占一条 PostgreSQL 连接；视觉腿不碰 DB。早期版本让三条腿共用调用方的 `AsyncSession`，SQLAlchemy 不允许并发 `execute`，sparse 与 trgm 会被记成 `skipped`，`hybrid` 静默退化成 dense 单腿。改成独立会话后并发回来了，代价从延迟换成连接数：一次 `hybrid` 最多占“请求会话 + 3 条腿 = 4 条连接”，连接池要按“并发检索数 × 4”估。

## 数据到底以哪份为准？

**PostgreSQL 是唯一真相源，Qdrant 是可重建的派生索引。** 由此得到一条贯穿全系统的原则：

> 允许 Qdrant 与 PostgreSQL 短暂不一致；绝不允许“PostgreSQL 里有而 Qdrant 里没有”。

删除文档时先改 PG（`deleted_at` 与 `is_current=false`），此刻对检索立即生效；Qdrant 里的点由后台任务清理。检索在融合后还会用 PG 再过滤一次，所以即使清理任务没跑完，被删内容也不会被召回。30 天后才做物理删除，删除链有六步，漏掉任何一步都不会报错。

原件放在内容寻址目录 `data/blobs/{sha 前两位}/{sha}/original`，同一份文件只存一次。预览 PDF 与缩略图也放在同一个目录下。Qdrant 不参与备份，因为它可以从 PG 的 chunk 重建；备份只需要保证 PG 与 blobs 完整。表结构与删除顺序见[数据模型与一致性](data-model.md)。

## Qdrant 为什么是三个集合？

Qdrant 的单个集合不支持“匿名向量 + 命名多向量”混用，而三种表示形态本来就不同：

| 集合 | 向量形态 | 模型或算法 | 承载 |
|---|---|---|---|
| `kb_dense` | 匿名稠密 512 维，Cosine | `bge-small-zh-v1.5` | 所有文本 chunk |
| `kb_sparse` | 顶层 `sparse_vectors` 里的 `bm25` | jieba + 自实现 BM25 | 文本 chunk 的词元级匹配 |
| `kb_visual` | 命名向量 `text`，128 维，`max_sim` 多向量 | `colSmol-500M` | 扫描页与照片 |

三个集合的 chunk 级点 ID 完全一致，由 `(kb_id, doc_id, chunk_index)` 经 blake2b-64 哈希算出；页级点把 `chunk_index` 换成负数来避开同一空间。用哈希而不是自增序号，是为了能从业务键直接算出点 ID，不必先查库——这是“删掉索引再重建，结果仍然一致”的前提。三集合的 payload 形状保持一致，融合时不需要知道一条命中来自哪条腿。

镜像里装了 pgvector，但向量没有放进 PostgreSQL：全局只用一个文本嵌入模型，Qdrant 是既有选型；而且 pgvector 不支持 late-interaction 多向量，与视觉腿冲突。PostgreSQL 这边承担真相源、子串检索、任务队列与审计。另一个实测结论是 PostgreSQL 内置全文分词对中文无效（整行被当成一个 token），镜像里也没有中文分词扩展，所以 BM25 留在 Qdrant，PostgreSQL 只做字符级子串搜索。

## 为什么检索只有一套实现？

对外有三个入口：HTTP、MCP 和 A2A。三者的检索与问答都调用 `services/retrieval/` 的同一个入口，各自只做协议适配。

理由不是抽象的代码复用。三套检索逻辑一旦各自演化，半年后就会出现“API 搜得到、MCP 搜不到”这类不一致，而且极难排查：两边看起来都正常。MCP 的 `ask_kb` 与 A2A 的 `answer` 都复用 `services/retrieval/ask.py` 的 `prepare` 与 `stream_answer`，只是最后把 SSE 帧解析回各自的协议形状。

## 哪些边界是刻意留下的？

| 边界 | 现状 | 取舍 |
|---|---|---|
| 文件存储 | 只有本地磁盘与内容寻址 blob | 单机自部署是目标形态；同一份文件只存一次 |
| 原件永不被掩码 | PII 掩码只作用于索引侧文本 | 用户仍可下载、预览原文 |
| 生成层默认抽取式 | 答案句子全部摘自原文 | 零外传，引用 100% 可核验 |
| 精排默认关闭 | 按知识库开启 | 只能重排候选，不能扩大召回；默认 v2-m3 下 20 个候选约 2.3 s |
| 不做 OCR | 扫描件交给视觉召回 | 不引入 tesseract 这条独立链路 |
| 不引入 pdf.js | 底图用后端渲染的页图 | 框位与服务端同源；代价是页面不能选中文字 |
| 不做多节点 | 没有分片与副本 | 这个规模上复杂度换不来收益 |

五条硬约束（依赖树、租户过滤入口、检索实现、fail-closed、API Key 是唯一租户来源）见[踩坑与硬约束](pitfalls.md)。

## 相关文档

- 文件如何变成可检索内容：[文档摄取](ingestion.md)
- 四条腿与问答：[检索与问答](retrieval.md)
- 表结构、删除与对账：[数据模型与一致性](data-model.md)
- 三档可见性与审计：[多租户与权限](tenancy.md)
- 队列、进程、巡检与备份：[任务、进程与运维](jobs-operations.md)
