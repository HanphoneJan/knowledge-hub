# 测试与验收体系

> 本项目的验收分成五层，**互不替代**。CI 只跑其中不需要基础设施与模型的那部分，全量基线留在本机。

---

## 1. 分层总览

```
+-----------------------------------------------------------------+
|  第5层  恢复演练  deploy/restore.sh                               |
|  业务视角：备份真的能恢复吗                                        |
+-----------------------------------------------------------------+
|  第4层  巡检  deploy/monitor.py（十四项，跳过不算健康）              |
|  运维视角：这个部署现在健康吗                                      |
+-----------------------------------------------------------------+
|  第3层  验收脚本  eval/（契约 81/82 + 1b 138 + 1c 218 + 1d 199）  |
|  真实链路视角：接口与端到端行为对不对                               |
+-----------------------------------------------------------------+
|  第2层  前端  vitest 316 + typecheck + build                       |
|  前端纯逻辑与构建                                                  |
+-----------------------------------------------------------------+
|  第1层  pytest 1335 + CI 门禁 deploy/ci.sh                         |
|  代码质量视角：单元、集成与结构守卫                                 |
+-----------------------------------------------------------------+
```

| 层 | 跑什么 | 需要什么 | 谁跑 |
|---|---|---|---|
| 1 | `uv run pytest`（1335） | Postgres + Qdrant + 本地模型 | 本机 |
| 1 | `bash deploy/ci.sh` | 什么都不需要 | CI + 本机 |
| 2 | `npm test` / `typecheck` / `build` | Node | 本机 + CI |
| 3 | `eval/*.py` + `contract_check.py` | 真库 + 模型 + 在跑的服务 | 本机 |
| 4 | `deploy/monitor.py` | 真库 + 真服务 | 本机 / cron |
| 5 | `deploy/restore.sh --scratch` | 一份备份 | 本机，建议每月 |

---

## 2. 第 1 层：pytest 与 CI 门禁

### 2.1 全量 pytest（本机）

```bash
uv run pytest                  # 1335 项
```

- 需要真实 PostgreSQL(pgvector) + Qdrant + **本地缓存的模型**（`bge-small` ~100MB；视觉腿 colSmol 2.65GB）；
- **离线加载模型**是一条硬约束：`tests/conftest.py` 与 `eval/*` 都 `setdefault` 了 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`；
- 不设的话每个模型加载会联网重试 5 次 ≈23 秒，整套 pytest 从 50 秒涨到 ~28 分钟；
- 三个 eval 脚本都会**清数据**（走 `eval/_reset.py`），**串行跑，别并发**。

### 2.2 CI 门禁（`deploy/ci.sh`）

```bash
bash deploy/ci.sh            # 全部
bash deploy/ci.sh web        # 只跑前端
bash deploy/ci.sh py         # 只跑后端免基础设施那部分
```

CI 只跑**不需要 DB / 模型 / 一个在跑的服务**的部分：

- 前端 `typecheck + vitest + build`；
- 契约覆盖率 `eval/contract_check.py`；
- **443 项免基础设施单测**（`PURE_TESTS` 白名单，30 来个文件）。

**为什么不把全量塞进 CI：** 模型要么进 CI 缓存、要么每次下载几百 MB，而离线纪律是硬约束；视觉腿在 CI 里必然 `skipped`，于是“跑过了”反而是假绿灯。

判定为“免基础设施”前要验三个维度（前两个各踩过一次）：

1. **数据库不可达**：把 `DATABASE_URL` 指向不存在的库，仍应全绿；
2. **服务不可达**：确认该文件不发真 HTTP——“本机正好起着 API”的写法定档在这里会红，并连坐同模块的纯单测；
3. **locale**：CI 是 `LANG=C`，本机常是 `zh_CN` ⇒ 任何 `localeCompare` / `toLocaleString` 的排序或格式断言都可能只在本机绿。验法：`LANG=C LC_ALL=C npm test`。

`ci.sh` 与本地跑的是**同一份命令**，workflow 只是调它；改“哪些测试进 CI”时先在本地按上表验一遍。

---

## 3. 第 2 层：前端

```bash
cd web && npm run typecheck && npm test && npm run build
```

- `vitest` **316** 项：核心纯逻辑（`src/lib/*` 各自带单测）+ 主题首绘守卫 + 切分策略 + locale 守卫；
- `typecheck` / `build` 必须过；
- 接口对齐：`src/types/api.ts` 与契约一致，后端 `eval/contract_check.py` 守路径/方法/参数面；
- 逐页浅/深**目测**（`web/scripts/screenshot.mjs`）+ `web/scripts/smoke.mjs` 无 console error / 无 4xx-5xx。这些是**开发工具，不是 e2e 契约**——别往 smoke 里加选择器断言。

---

## 4. 第 3 层：eval 验收脚本

| 脚本 | 项数 | 覆盖 |
|---|---|---|
| `eval/contract_check.py` | **81/82** | 端点路径 / 方法 / **参数面** |
| `eval/api_1b_check.py` | **138** | 认证 / 权限 / 邀请码 / PII 掩码 / 回收站 / 统计 / 限流 / 对外加固 |
| `eval/api_1c_check.py` | **218** | 摄取全链路 / 预览 / 时间线 / 导入 / 坐标 |
| `eval/api_1d_check.py` | **199** | 检索 / 问答 SSE / MCP / A2A / 审计 / 运维审计 |

```bash
uv run python eval/contract_check.py
uv run python eval/api_1b_check.py   # 串行
uv run python eval/api_1c_check.py   # 串行
uv run python eval/api_1d_check.py   # 串行
```

- 三者都走 `eval/_reset.py` 清场，**串行跑，别并发**；
- 项数可用 `grep -cP '^\x1b\[32m✓'` 复核；
- `_fixtures.py` 负责造数据，照片 keeper 库就靠它；
- **真实数据不进仓库**：`_qset.py`（问题集）、`_local_data.py`（探针 PDF / 问题 / 标记）、`reports/` 都只留本地；仓库里只有 `*.example.py` 模板。守卫 `tests/test_no_real_data.py` 按本地 deny-list 扫已跟踪文件。

### 4.1 检索质量回放

`eval/retrieval_eval.py` 与 `retrieval_metrics.py` 是改检索时的核心工具：

- 它第一次跑就抓到“hybrid 静默降级成 dense 单腿”（三条 DB 腿共用 `AsyncSession` 并发导致）；
- 路由 `date` 权重的方向性错误也是它发现的。

> 后续改检索**先跑回放**，别只看“调用返回 200”。

---

## 5. 第 4 层：巡检

```bash
uv run python deploy/monitor.py          # 十四项（人看）
uv run python deploy/monitor.py --json   # 机读
uv run python deploy/monitor.py --quiet  # 只输出问题
uv run python deploy/monitor.py --only df index
```

退出码 `0` = 一切正常，`1` = 有问题（**跳过也算**）。详细检查项见 [部署与运维](./operations.md)。

---

## 6. 第 5 层：备份与恢复演练

```bash
bash deploy/backup.sh                              # 备份（含自检）
bash deploy/backup.sh --list                       # 列出（含自检结果）
bash deploy/backup.sh --verify DIR                 # 只校验
bash deploy/restore.sh --scratch data/backups/<ts> # 演练（推荐，每月一次）
```

演练会：校验备份完整性 → 建临时库并 `pg_restore` → **逐表行数对账** → **逐个 blob sha256 校验** → 删临时库，并提示“重建索引后按 `qdrant_expect` 对账”。

已实测过一次：**18 张表行数全对 · 126 个 blob sha256 全对 · 临时库自动删除**。

> 没恢复过的备份只是一堆文件。

---

## 7. 结构守卫（不靠人记）

| 守卫 | 钉住什么 |
|---|---|
| `test_retrieval_filters.py` | 租户过滤只有统一入口；检索实现只有一份；运行时只有一棵依赖树 |
| `test_permissions.py` | 三档可见性三个独立分支，SQL 里不出现 `visibility IN` |
| `test_container_trash.py` | 回收站用写判据，用真值表逐格比对 |
| `test_audit_coverage.py` | 新增检索端点必须接审计 |
| `test_unawaited_coroutines.py` | 调了协程函数但没 await |
| `test_visual_service.py` | 视觉服务不得碰 Qdrant 或 auth |
| `test_strategies.py` | 切分策略注册表与对外 schema 枚举相等 |
| `test_index_models.py` | Embedding 模型一致性守卫 |
| `test_no_real_data.py` | 已跟踪文件里没有真实数据 |
| `test_docs_pointers.py` | 入口文档的路径 / 命令 / 端口 / 基线数字对得上 |
| `test_contract_parser.py` | 钉住契约解析器本身 |

### 7.1 写这类断言的纪律

- **AST 类断言必须做变异验证**：故意改坏一处，确认测试真的会红（并单独判定 pytest `exit 5`——那是“没收集到测试”，不是通过）；
- **判据要指向性质，不是中间状态**：判了某个中间状态或某行实现字面量，改实现就会误红；
- **抽了 helper 必须接线**：不接线的 helper 等于没有；
- **“仍然全绿”要分清**是判据弱、变异无效，还是 `-k` 指错。

---

## 8. 基线数字怎么复核

```bash
uv run pytest                                  # 1335
uv run python eval/contract_check.py           # 81/82
uv run python eval/api_1b_check.py             # 138
uv run python eval/api_1c_check.py             # 218
uv run python eval/api_1d_check.py             # 199
cd web && npm test                             # 316
uv run python deploy/monitor.py                # 十四项
uv run alembic check                           # No new upgrade operations
```

> 数字**只能来自实跑**。改完任何一块都要重跑全量；改后端代码后先 `bash deploy/dev.sh restart`（见 [任务队列与进程模型](./jobs.md) 第 9 节）。

---

## 9. 相关文档

- 巡检、备份、对外访问：[部署与运维](./operations.md)
- 检索质量相关脚本：[检索与融合](./retrieval.md)
- 契约与端点：[API 契约](./DESIGN-API.md)
