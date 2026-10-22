# Knowledge Hub · 项目文档

单机自部署的多租户知识库与 RAG 检索服务。办公文档、聊天导出和照片进不同的知识库，解析、切分、嵌入之后用四条召回腿找到原文，带坐标级引用回答问题，并通过 HTTP、MCP、A2A 对外提供同一套内核。

它要解决的是三类散落资料：PDF、DOCX、XLSX、PPTX 与 Markdown；几万条聊天导出；没有文本层的照片与扫描件。对应的三条底线是可核验（引用能点回原文那一页那一句）、不外传（默认抽取式，零外传）、可运维（队列、进度、对账、备份与恢复演练）。

这套文档面向读代码的人，讲“为什么这样设计、代价是什么、什么时候会降级”。使用方式看仓库根 [`README.md`](../README.md)，逐个端点的请求与响应看 [`DESIGN-API.md`](DESIGN-API.md)。

文中的人物、文件名、查询内容都是合成示例；代码路径、表名、环境变量与实测数字保留原名。图在 `docs/diagrams/`，源文件是 `.drawio`，正文引用 `.svg`。

## 文档地图

| 文档 | 回答的问题 | 什么时候读 |
|---|---|---|
| [architecture.md](architecture.md) | 有哪几个进程？一次上传、一次提问分别经过什么？真相存在哪？ | 第一次接手，建立全局图景 |
| [ingestion.md](ingestion.md) | 支持哪些格式？六种切分策略怎么选？校验分几层？坐标从哪来？ | 加解析器、切分策略，查“入库了但搜不到” |
| [retrieval.md](retrieval.md) | 四条腿各擅长什么？为什么用 RRF？路由、改写、精排、拒答各做什么？ | 改检索质量、调参数、排“搜不到” |
| [data-model.md](data-model.md) | 18 张表怎么分组？Qdrant 为什么三个集合？删除与对账的顺序？ | 改表结构、加索引、查数据不一致 |
| [tenancy.md](tenancy.md) | API Key 如何推导租户？三档可见性为什么不能写进一个 IN？审计记什么？ | 改权限、加资源、做安全评审 |
| [jobs-operations.md](jobs-operations.md) | 任务怎么领取与重试？文档状态何时翻 ready？巡检、备份、蓝绿重建怎么做？ | 改队列、部署、排障、例行维护 |
| [api-mcp-a2a.md](api-mcp-a2a.md) | HTTP 错误与分页约定？MCP 四个工具？A2A 两个 skill？权限怎么裁？ | 对接客户端或改端点 |
| [DESIGN-API.md](DESIGN-API.md) | 82 个端点的请求与响应形状、错误码、SSE 事件、分页与预览协议 | 对接接口或改端点，**改端点先改它** |
| [frontend.md](frontend.md) | 外壳与路由怎么组织？SSE 与引用画框有哪些坑？设置与知识库有哪些分区？ | 改前端或理解交互 |
| [testing.md](testing.md) | 测试分几层？CI 跑什么、本机跑什么？哪些结构守卫钉住架构？ | 提交前、改判定标准时 |
| [pitfalls.md](pitfalls.md) | 五条硬约束是什么？哪些 bug 不报错但结果是错的？ | 动手改代码前 |

## 按目标挑读法

第一次系统读，建议按这条线走：架构 → 文档摄取 → 检索与问答 → 数据模型 → 多租户 → 任务与运维 → 对外接口。前端与测试可以最后看，踩坑放在动手改代码前。

- **先建立全局图景**：读 [architecture.md](architecture.md)，再看一遍那张进程与存储图。
- **关心检索算法**：直接读 [retrieval.md](retrieval.md)，里面的实测表都标了口径。
- **关心文档处理**：读 [ingestion.md](ingestion.md) 的六种切分策略与分层校验两张表。
- **要改代码**：先读 [architecture.md](architecture.md) 的五条硬约束与 [pitfalls.md](pitfalls.md)。
- **要部署或排障**：读 [jobs-operations.md](jobs-operations.md)，再看 [`deploy/README.md`](../deploy/README.md)。
- **要对接接口**：读 [api-mcp-a2a.md](api-mcp-a2a.md) 与 [DESIGN-API.md](DESIGN-API.md)。
- **只想知道怎么跑起来**：回根 [`README.md`](../README.md)。

## 术语速查

| 词 | 在这套系统里的意思 |
|---|---|
| KB | 知识库，三档可见性，文档的容器 |
| chunk | 切分后的最小索引单位，带可选坐标 |
| 腿（leg） | 一种召回方式：dense、sparse、trgm、visual |
| RRF | 按各腿排名融合，不比较分数 |

## 全局速览

| 维度 | 现状 |
|---|---|
| 进程 | api `8010` · worker 无端口 · visual_service `8020` |
| 检索 | 四条腿 RRF：dense（bge-small-zh-v1.5，512 维）· BM25（jieba，自实现）· trgm（PostgreSQL）· 视觉（colSmol-500M，128 维多向量 MaxSim） |
| 存储 | PostgreSQL 是真相源；Qdrant 三集合是可重建派生索引；原件走内容寻址 blob |
| 租户 | 三档可见性，过滤只在 `services/common/auth.py` 构造，请求体不接受租户字段 |
| 生成 | 默认抽取式，答案句子全部摘自原文；外部 LLM 需显式配置 |
| 任务 | PostgreSQL 任务表 + 轮询 worker；`jobs.progress` 是进度唯一来源 |
| 规模 | 契约 82 声明 / 81 实现 / 1 明确不做 · pytest 1335 · vitest 316 · 三套验收 138 / 218 / 199 |

性能数字都有测量口径。单个数字脱离口径没有意义，例如把四类语料混在一起算的 overall R@10，不能拿来做出取舍。
