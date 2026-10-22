# 测试与验收

这个项目的测试分两半：一半能在托管 CI 上必然绿灯，另一半必须在有真实 Postgres、Qdrant 和本地模型的机器上跑。这条分界不是"快的放 CI、慢的放本地"，而是由一条硬约束决定的——模型离线加载。CI 里没有缓存好的 bge-small 与 colSmol，硬跑全量只会得到"视觉腿静默 skipped"的假绿灯。

## 分层

![测试分层与各自需要的基础设施](diagrams/test-layers.png)

| 层 | 规模（2026-10-22 实测） | 需要什么 | 抓什么 |
|---|---|---|---|
| 后端全量 pytest | 1335 项，80 个 `.py`（78 个 `test_*.py`），约 22.8k 行 | 真 Postgres(pgvector) + Qdrant + 本地模型；无 DB/无 Qdrant 的用例会 `skipif` | 端到端、权限、状态机、索引对账 |
| CI 免基础设施单测 | 449 项，27 个文件 | 不需要 DB、模型、服务、特定 locale | 纯函数、契约解析、文档指向、结构守卫 |
| 三套 HTTP 验收（1b/1c/1d） | README 记录 138 / 218 / 199 项 | 起 API + worker；1c/1d 还要一份本地多页 PDF | 权限边界、上传→检索、SSE、MCP/A2A |
| 前端 vitest | 316 项，18 个文件 | Node | 组件、纯函数、状态 |
| 前端构建门禁 | `typecheck + vitest + build` | Node | 类型、产物 |
| 运维巡检 | 12 个检查函数，报告通常 14 项 | 真服务 + PG/Qdrant | 队列、对账、磁盘、卡住文档、备份 |
| 契约检查 | 声明 82 / 实现 81 / 明确不做 1 | 无 | 路径、方法、query 参数面 |

CI 的 27 个文件是显式列出来的，不是按目录猜。加文件前要验三个维度：把数据库指向不存在的库仍全绿；把服务地址指向没人听的端口仍全绿；`LANG=C LC_ALL=C` 下前端测试仍全绿。第三个维度专门防 `localeCompare` / `toLocaleString` 这类只在本机绿的断言。

## 全量 pytest 与 CI

```bash
uv run pytest                 # 1335 项，需要真实依赖
bash deploy/ci.sh             # 前端 typecheck/vitest/build + 契约 + 449 项纯单测
```

全量里的集成用例会用 `pytest.mark.skipif` 探测 Postgres/Qdrant 是否在线。跳过只说明"这个 checkout 没有依赖"，不代表通过；CI 不跑这些，所以 CI 绿不等于全量绿。

测试共用一个 session 级 event loop。`services/common/db.py` 的异步引擎是模块级单例，asyncpg 连接池绑定创建它的 loop；每个用例各调一次 `asyncio.run()` 会报 `attached to a different loop`。这不是测试洁癖，是运行时的真实约束。

清理测试账号必须走 `eval/_reset.py::reset_users_and_bm25`，不能裸 `DELETE FROM users`。硬删会级联删掉 `chunks`，而 BM25 的 `df` 只在入库与重建里维护，删完 `corpus_stats.n_docs` 虚高。后果不是报错，是检索质量悄悄变差，且下一次跑 pytest 会假红在"n_docs 对不上"——症状与被测代码毫无关系。

## 三套 HTTP 验收

1b/1c/1d 不是 pytest，是起真实服务后用 httpx 打请求：

- **1b**：权限边界、fail-closed、上传校验、审计、限流、对外开关。它关心"无权限是不是真的看不到"，不是"接口能不能 200"。
- **1c**：上传 → worker 摄取 → Qdrant 可检索。用一份真实多页表单 PDF，验收标准是"用问题能检索到原文标记段落"。
- **1d**：检索 + 问答 SSE。同一份 PDF，测召回质量、SSE 事件顺序、MCP/A2A。

1c/1d 的探针 PDF、问题、原文标记都从**不被 git 跟踪**的 `eval/_local_data.py` 读；没有它时退回合成示例。仓库里不许出现真实值，`tests/test_no_real_data.py` 会按本地 deny-list 扫已跟踪文件。

三套脚本的项数会随断言增删变化。README 记录的 138/218/199 是 2026-10-19 那一轮的数字；本次没有重跑 1c/1d，因为工作区里没有那份探针 PDF。写文档时把它当"上一轮实跑记录"，不要当代码里的常量。

## 前端

```bash
cd web && npm run typecheck && npm test && npm run build
```

18 个测试文件、316 项。组件测试与纯函数测试混在一起，重点是状态与契约映射：SSE 帧怎么归并、引用角标怎么点开、四路召回怎么画、深色主题的对比度。构建由 API 托管，产物在 `web/dist`。

## 结构守卫

这些测试不验行为，验"某个约定没有被绕过去"：

| 守卫 | 守什么 |
|---|---|
| `test_docs_pointers.py` | README/入口文档里的每个路径存在、`dev.sh` 子命令存在、端口与脚本一致、pytest 数字等于实测收集数 |
| `test_contract_parser.py` | 契约解析器不能被改坏；声明数、实现数、参数面共享同一个路径解析 |
| `test_no_real_data.py` | 真实语料路径不被跟踪；已跟踪文件不含 deny-list 真实词 |
| `test_qdrant_filter_scope.py` | Qdrant 过滤只允许 `kb_id`；`services/` 下没有用 ACL 字段做 key 的过滤字典 |
| `test_audit_coverage.py` | 每个检索/问答端点都接审计，且 `write_audit` 真的被 await |
| `test_parser_registry.py` | 上传白名单与图片后缀从派发表派生，不是手写第二份 |
| `test_visual_leg.py` | colSmol 用 `MultiVectorEncoder` 加载、维度取 `VDR_DIM`、视觉 payload 与文本腿同字段 |
| `test_container_trash.py` | 回收站列表的 SQL 判据与恢复的行判据逐格等价 |
| `test_doc_state.py` | 文档终态判定只动中间态、只认最后一条 ingest、thumbnail 不挡终态 |
| `test_consistency_periodic.py` | 对账不产生笛卡尔积、分页 scroll、HTTP 错误不当成 0 个点、绝不删任何东西 |

这些守卫的价值在于"变异会被抓住"。比如把 `parsers.py` 写成 `parsers/`，`test_docs_pointers` 会红；把契约正则在 `?` 处截断，`test_contract_parser` 会红；在任一服务文件里加 `{"key": "visibility"}`，`test_qdrant_filter_scope` 会红。

## 契约检查

```bash
uv run python eval/contract_check.py --strict
```

输出：

```
契约声明 82 个端点 · 实现 81 个 operation（其中 1 个已决议不做）
覆盖率 81/82（实现里有 0 个契约没写）
```

它读 `docs/DESIGN-API*.md` 里所有 `METHOD /path?query`，与 `app.openapi()` 对账。只查路径、方法与 query 参数面；字段名、返回类型这类形状漂移查不出来，那些在契约里就地标"以代码为准"。

三个数字都要看懂：声明 82、实现 81、明确不做 1。`POST /api/v1/jobs/{id}/retry` 是决议不做，不算缺口；如果它被实现了，检查会报"与决议相反地实现了"。

## 运维巡检作为验收

巡检不是只看进程活着。它把"索引是否对得上、文档是否卡住、队列是否堆积、备份是否太旧"都变成退出码。`--quiet` + 退出码给 cron 用：

```bash
0 8 * * * cd /path/to/knowledge-hub && uv run python deploy/monitor.py --quiet
```

"跳过不算健康"这条很关键：巡检没配好 API Key 时，报告不能写"一切正常"，否则 cron 每天绿着，而索引一致性从来没被查过。

## 已知盲区

- **CI 不跑视觉腿**：没有 colSmol，视觉相关用例要么跳过，要么只验源码形状。CI 绿不能证明视觉检索可用。
- **全量依赖本机模型缓存**：`HF_HUB_OFFLINE=1` 是硬约束；换机器要先准备模型，否则整套会慢到几十分钟。
- **三套 eval 依赖真实 PDF 与测试账号**：探针缺失时 1c/1d 跑不了；1b 会造/清测试账号，不要在共享库上跑。
- **契约检查只到参数面**：返回字段、SSE 帧字段靠 pytest 与前端测试守，不靠契约脚本。
- **前端测试不覆盖真实浏览器交互**：拖拽上传、SSE 逐字、引用抽屉的滚动是手工验收或端到端脚本，不在 vitest 里。
- **数字会漂**：1335、449、316、138/218/199 都是"某个时点的收集数"，测试增删后要同步；`test_docs_pointers` 只盯 pytest 这一项，其余靠人。
