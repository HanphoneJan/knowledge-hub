# 测试与验收

Knowledge Hub 的验收分五层，互不替代。CI 只跑不需要基础设施与模型的那部分，全量基线留在本机。

| 层 | 跑什么 | 需要什么 | 谁跑 |
|---|---|---|---|
| 1 | `uv run pytest`，1335 项 | PostgreSQL + Qdrant + 本地模型 | 本机 |
| 1 | `bash deploy/ci.sh` | 什么都不需要 | CI 与本机 |
| 2 | `npm test` / `typecheck` / `build`，316 项 | Node | 本机与 CI |
| 3 | `eval/*.py`，契约 81/82、1b 138、1c 218、1d 199 | 真库 + 模型 + 在跑的服务 | 本机 |
| 4 | `deploy/monitor.py`，14 项 | 真库 + 真服务 | 本机或 cron |
| 5 | `deploy/restore.sh --scratch` | 一份备份 | 本机，建议每月 |

## 全量 pytest 为什么必须在本机？

```bash
uv run pytest                  # 1335 项
```

全量需要真实 PostgreSQL(pgvector)、Qdrant 和本地缓存的模型：`bge-small` 约 100 MB，视觉腿 colSmol 2.65 GB。模型一律离线加载，`tests/conftest.py` 与 `eval/*` 都设了 `HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1`。不设的话每次模型加载会联网重试约 23 秒，整套 pytest 会从 50 秒涨到接近半小时。

三个 eval 脚本都会清数据（走 `eval/_reset.py`），必须串行跑，不能并发。改完后端代码先 `bash deploy/dev.sh restart`，否则旧 worker 还会用旧代码领任务。

## CI 门禁为什么只跑一部分？

```bash
bash deploy/ci.sh            # 全部
bash deploy/ci.sh web        # 只跑前端
bash deploy/ci.sh py         # 只跑后端免基础设施那部分
```

CI 跑三件事：前端 `typecheck + vitest + build`；契约覆盖率 `eval/contract_check.py`；27 个免基础设施 pytest 文件，约 443 项。

不把全量塞进 CI 的理由很实际：模型要么进 CI 缓存，要么每次下载几百 MB，而离线加载是硬约束；视觉腿在 CI 里必然 `skipped`，于是“跑过了”反而是假绿灯。CI 与本机跑的是同一份命令，workflow 只是调 `deploy/ci.sh`。

判定一个测试文件“免基础设施”要验三个维度，前两个各踩过一次：

1. **数据库不可达**：把 `DATABASE_URL` 指向不存在的库，仍应全绿；
2. **服务不可达**：确认该文件不发真 HTTP；“本机正好起着 API”的写法在这里会红，还会连坐同模块的纯单测；
3. **locale**：CI 是 `LANG=C`，本机常是 `zh_CN`，任何 `localeCompare` 或 `toLocaleString` 的排序断言都可能只在本机绿。验法是 `LANG=C LC_ALL=C npm test`。

## 前端怎么验收？

```bash
cd web
npm run typecheck && npm test && npm run build
```

`vitest` 316 项，覆盖 `src/lib/*` 的纯逻辑、主题首绘守卫、切分策略投影与 locale 守卫。前端不维护 DOM 或 class 契约，唯一的自动化验收线是接口对齐：`src/types/api.ts` 与 `docs/DESIGN-API.md` 一致，后端 `eval/contract_check.py` 守路径、方法与参数面。

目测用 `web/scripts/screenshot.mjs` 出浅色与深色两张；`web/scripts/smoke.mjs` 是开发期安全网，抓白屏、未捕获异常与静默 401，不是 e2e，别往里面加选择器断言。

## eval 脚本覆盖什么？

| 脚本 | 项数 | 覆盖 |
|---|---|---|
| `eval/contract_check.py` | 81/82 | 端点路径、方法、参数面 |
| `eval/api_1b_check.py` | 138 | 认证、权限、邀请码、PII、回收站、统计、限流、对外加固 |
| `eval/api_1c_check.py` | 218 | 摄取全链路、预览、时间线、导入、坐标 |
| `eval/api_1d_check.py` | 199 | 检索、问答 SSE、MCP、A2A、审计、运维审计 |

```bash
uv run python eval/contract_check.py
uv run python eval/api_1b_check.py
uv run python eval/api_1c_check.py
uv run python eval/api_1d_check.py
```

项数可以用 `grep -cP '^\x1b\[32m✓'` 复核。`eval/_fixtures.py` 负责造数据，照片 keeper 库就靠它。

真实数据不进仓库：`eval/_qset.py`（问题集）、`eval/_local_data.py`（探针 PDF、问题、标记）、`reports/` 都只留本地，仓库里只有 `*.example.py` 模板。守卫是 `tests/test_no_real_data.py`，按本地 deny-list 扫已跟踪文件。

## 检索质量怎么回放？

`eval/retrieval_eval.py` 与 `eval/retrieval_metrics.py` 是改检索时的核心工具。它按固定的问题集重跑，比较 Hit@k、MRR、Recall，回答“这次改动是变好了，还是换了一种错法”。

它第一次跑就抓到“hybrid 静默降级成 dense 单腿”：三条 DB 腿共用一个 `AsyncSession`，并发 `execute` 失败，sparse 与 trgm 被记成 `skipped`，接口仍然 200。路由 `date` 权重的方向性错误也是它发现的。后续改检索先跑回放，别只看“调用返回 200”。

## 巡检与恢复演练

```bash
uv run python deploy/monitor.py          # 14 项
uv run python deploy/monitor.py --json   # 机读
uv run python deploy/monitor.py --quiet  # 只输出问题
```

退出码 0 表示正常，1 表示有问题，**跳过也算**。巡检“跳过”意味着这项没验，把它算成通过等于自欺。检查项与 cron 用法见[任务、进程与运维](jobs-operations.md)。

备份与恢复演练：

```bash
bash deploy/backup.sh                              # 备份，含自检
bash deploy/backup.sh --list                       # 列出，含自检结果
bash deploy/backup.sh --verify DIR                 # 只校验
bash deploy/restore.sh --scratch data/backups/<ts> # 演练
```

演练会校验备份完整性，建临时库并 `pg_restore`，逐表对行数、逐个比 blob 的 sha256，最后删临时库，并提示重建索引后按 `qdrant_expect` 对账。实测过一次：18 张表行数全对、126 个 blob 的 sha256 全对、临时库自动删除。没恢复过的备份只是一堆文件。

## 结构守卫清单

这些测试不是测功能，是钉住架构约束。

| 守卫 | 钉住什么 |
|---|---|
| `test_retrieval_filters.py` | 租户过滤只有统一入口；检索实现只有一份；运行时只有一棵依赖树 |
| `test_permissions.py` | 三档可见性写成三个独立分支，SQL 里不出现 `visibility IN` |
| `test_container_trash.py` | 回收站用写判据，真值表逐格比对 |
| `test_audit_coverage.py` | 新增检索端点必须接审计 |
| `test_unawaited_coroutines.py` | 调了协程函数但没 await |
| `test_visual_service.py` | 视觉服务不得碰 Qdrant 或 auth |
| `test_strategies.py` | 切分策略注册表与对外 schema 枚举相等，默认路径逐字不变 |
| `test_parser_registry.py` | 格式派发表是唯一清单，白名单由它派生 |
| `test_index_models.py` | Embedding 模型一致性 |
| `test_no_real_data.py` | 已跟踪文件里没有真实数据 |
| `test_docs_pointers.py` | 入口文档的路径、命令、端口、基线数字对得上 |
| `test_contract_parser.py` | 契约解析器本身 |
| `test_leg_concurrency.py` | 三条 DB 腿各拿一个会话 |
| `test_schema_drift.py` | `alembic check` 没有待生成迁移 |
| `test_ask_llm_degrade.py` | 外部生成器失败降级抽取式，不 500 |
| `test_metadata_filter.py` | 时间过滤与 payload 字段 |
| `test_quality.py` / `test_rewrite.py` | 证据质量与查询改写的性质 |

写这类断言的纪律：

- **AST 类断言必须做变异验证**：故意改坏一处，确认测试真的会红；还要单独判定 pytest 的 `exit 5`，那是“没收集到测试”，不是通过。
- **判据指向性质，不指向中间状态**：判了某行实现字面量，改实现就会误红。
- **抽了 helper 必须接线**：不接线的 helper 等于没有。
- **“仍然全绿”要分清**是判据弱、变异无效，还是 `-k` 指错。

## 基线数字怎么复核？

```bash
uv run pytest                                  # 1335
uv run python eval/contract_check.py           # 81/82
uv run python eval/api_1b_check.py             # 138
uv run python eval/api_1c_check.py             # 218
uv run python eval/api_1d_check.py             # 199
cd web && npm test                             # 316
uv run python deploy/monitor.py                # 14 项
uv run alembic check                           # No new upgrade operations
```

数字只能来自实跑。改完任何一块都要重跑全量；只改文档也要跑 `tests/test_docs_pointers.py` 与 `eval/contract_check.py`，因为契约与路径守卫会读它们。

## 相关文档

- 巡检、备份与恢复演练：[任务、进程与运维](jobs-operations.md)
- 检索质量回放的对象：[检索与问答](retrieval.md)
- 契约与端点：[API 契约](DESIGN-API.md)
