# 任务、进程与运维

Knowledge Hub 的后台只有一张 `jobs` 表和一个轮询 worker。任务量不大，单机部署，没必要引入 Celery 或独立消息队列；而进度条本来就需要一份任务状态表，把待办任务放进去是零成本。

## 为什么是 Postgres 任务表？

需求很具体：任务与业务数据同库，`ON DELETE CASCADE` 能自动清掉文档的待处理任务；单机任务峰值只有几百条；多 worker 要用 `FOR UPDATE SKIP LOCKED` 保证不抢同一行。于是得到 `jobs` 表加轮询 worker：API 只负责入队与查询进度，worker 负责执行。

八种任务类型：

| kind | 成本 | 说明 |
|---|---|---|
| `ingest` | 约 50 ms/页 | 解析、切分、嵌入、写文本索引，一次做完 |
| `embed_visual` | 约 15 s/页 | colSmol 编码，一页一个任务 |
| `thumbnail` | 约 30 ms/页 | PDF 前 20 页，宽 400 px |
| `pdf_preview` | 变量 | Office 转 PDF，并在转换版上补坐标 |
| `cleanup` | 变量 | 回收站到期、库与工作区物理清理 |
| `import_chat` | 变量 | 读 JSONL，每 50 条消息切一个文档 |
| `import_dir` | 变量 | 从服务器目录导入图片 |
| `reindex` | 分钟级 | 重建索引、修 BM25 词表漂移 |

早期 `parse` / `chunk` / `embed` 是三个 kind，后来合并成 `ingest`：三步加起来不到 1 秒，拆开只是多三次事务，进度回传还要跨三个任务拼装。

## 任务怎么领取、重试、结算？

![任务状态机与文档终态判定：pending → running → done / failed / pending 重试；文档由三个守卫决定 ready 还是 failed](diagrams/job-state.svg)

worker 用一条 `SELECT ... FOR UPDATE SKIP LOCKED` 领取最早的任务，把它置 `running`，写 `locked_at` 与 `locked_by`，`attempts` 加一。租约是 15 分钟：超过这个时间没续租，别的 worker 可以接管。长任务（视觉编码）会周期性 `heartbeat` 续租，否则会被抢走，两个 worker 同时编码同一页。

结算分三种：

- 成功：`status=done`，`progress=1.0`；
- 可重试失败：`attempts < max_attempts` 时回到 `pending`，清空租约；`max_attempts` 默认 3；
- 永久失败：直接 `failed`，不再占用重试次数。

`IngestError.retryable` 区分瞬时与永久错误。blob 缺失、格式不支持、超过页数上限是永久错误；网络抖动、视觉服务暂时不可用是瞬时错误。未注册的 kind 永远不会成功，直接终态失败。

进度有一个反直觉的约定：`progress=1.0` 只归 `finish_job` 所有，handler 上报的进度封顶 0.999。原因是 handler 结尾会调 `progress(1.0)`，而它走独立事务；`finish_job` 在之后另一个事务里才把状态翻成 `done`。两次提交之间存在几毫秒窗口，读到的样本会是“`running` + 100%”。不封顶的话，这个窗口会让端到端测试偶发红，而“偶发红”比“稳定红”更贵——下一个人会去查一个不存在的问题。封顶而不是丢弃，是为了让进度单调不减。

空闲时 worker 的轮询间隔按 1 秒、2 秒、5 秒递增，有任务立刻回到 1 秒，不空转烧 CPU。

## 文档终态怎么判？

`jobs.status` 与 `documents.status` 是两份状态。只看任务，部分失败会被报成 `ready`；只看索引点数，全失败时文档会永远停在 `pending`。所以判定要同时看三件事：

1. 文档还在中间态吗？`ready` 与 `failed` 都不许动，尤其 `failed` 可能是用户已经看过并决定不重试的结果。
2. 还有未完成的 `ingest` 或 `embed_visual` 吗？有就还没到终态。
3. 该有的视觉页级点齐了吗？`len(scan_pages)` 是期望值，Qdrant 里该文档的点数是实际值。

这套判定只作用于中间态。纯扫描件与照片没有文本腿，会停在 `pending` 等页级点齐；有文本层的混合 PDF 在文本腿完成时已经 `ready`，视觉页任务随后补齐，状态不会因为视觉页还没齐而回退。

挡住判定的 kind 只有 `ingest` 与 `embed_visual`。`thumbnail` 与 `pdf_preview` 不挡：把它们算进去，会让“缩略图还没生成”把一份页级点已经齐了的文档永远按在 `pending`。

结算必须在 `finish_job` 的事务之外，而且失败不抛异常。任务终态已经落库，如果和文档状态绑在同一事务里，“改文档状态失败”会把 15 秒的模型前向一起回滚；而抛出去会被 worker 当成“任务失败”再走一遍 `finish_job(error=...)`，把一个 `done` 的任务改回 `pending`，比原问题严重得多。代价是“任务成功但文档还是中间态”的窗口，由启动补扫与巡检兜底。

`services/worker/doc_state.py` 是唯一判定处。worker 启动时补扫“中间态却没有任何未完成任务”的文档：正常情况下这里什么都没有，它存在是为了让已经卡住的那些能自愈。巡检里也有“卡住的文档”一项。

## 三个进程怎么起停？

```bash
bash deploy/dev.sh start      # API(8010) + worker + 视觉服务(8020)
bash deploy/dev.sh restart    # 改了后端代码之后用这个
bash deploy/dev.sh stop

bash deploy/visual.sh start|stop|restart|status|logs   # 视觉服务单独管
PKH_WORKERS=4 bash deploy/dev.sh start                 # 多 worker
```

`dev.sh` 只有 `start` / `restart` / `stop` 三个子命令，没有 `status` 与 `logs`。看状态用：

```bash
curl -s --noproxy '*' http://127.0.0.1:8010/health
tail -20 /tmp/pkh_api.log /tmp/pkh_worker.log /tmp/pkh_visual.log
```

视觉服务首次启动要 13 秒加载 colSmol。`dev.sh` 等不到它也会正常退出——那段时间视觉腿降级成 `skipped`，检索照常。不想装视觉腿就把 `VISUAL_SERVICE_URL` 留空，关掉读侧；写入侧不受影响。

改过前端要 `cd web && npm run build`，`dev.sh` 不会替你构建。首次准备还要建账号（对外部署通常关掉自助注册，用本机 CLI 建）与可选的持久照片库：

```bash
uv run python -m services.api.scripts.create_user
uv run python deploy/seed_photos.py
```

改后端代码后必须用 `restart`。`uv run X` 会产生两个进程（wrapper 加真正的 python），只 kill 进程组 leader 杀不干净子进程，旧 worker 会继续抢任务、用旧代码处理，症状是“改了没生效”。

关键环境变量：

| 变量 | 作用 |
|---|---|
| `ALLOW_REGISTER` | 自助注册开关，默认开；关掉后新建账号走本机 CLI |
| `EXPOSE_DOCS` | `/docs`、`/redoc`、`/openapi.json` 开关，关掉后路由不存在 |
| `VISUAL_SERVICE_URL` | 视觉查询服务地址；留空关掉读侧 |
| `VISUAL_WRITE_VIA_SERVICE` | 写侧也走视觉服务，默认关 |
| `PKH_WORKERS` | worker 数量 |
| `QDRANT_ALIASES` | 别名模式与蓝绿重建，默认 false |
| `IMPORT_ROOTS` | 导入路径白名单 |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | 模型离线加载，脚本与测试都设 |
| `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` | 每进程 PostgreSQL 连接池，默认 5 + 5 |

环境变量没有前缀。两个安全开关默认都是开，因为本机开发、pytest 与三套 eval 都靠自助注册；默认关会让基线全红，对外部署时再显式关掉。

连接池是并发检索的隐性预算。这里的连接指 API 或 worker 到 PostgreSQL 的连接池连接，不是浏览器到 API 的 HTTP 连接。每个进程最多 10 条（5 + 5）；一次 `hybrid` 最多占 4 条（请求会话 1 + 三条 DB 腿各 1），`semantic` / `exact` 占 2 条。池大小至少按“并发检索数 × 4 + 余量”估。池不够时表现为排队变慢，不是算错。

## 多 worker 与内存预算

默认形态下 worker 自己加载 colSmol。同机跑 N 个 worker 会把 2.65 GB 乘以 N。可选形态是让写侧也走视觉服务：

```bash
VISUAL_WRITE_VIA_SERVICE=true
PKH_WORKERS=4 bash deploy/dev.sh start
```

此时 worker 不加载模型，实测 RSS 从峰值约 2.8 GB 降到约 991 MB，同机能跑多个 worker。代价是摄取从此依赖视觉服务可用，所以默认关闭。

内存预算大致是：

| 进程 | 常驻 | 峰值 |
|---|---|---|
| `api` | 约 400 MB | 约 600 MB |
| `worker` | 约 200 MB（未编码） | 约 2.8 GB（本地视觉） |
| `visual_service` | 2.71 GB | 约 2.8 GB |
| PostgreSQL | 约 200 MB | 约 500 MB |
| Qdrant | 约 300 MB | 约 800 MB |

worker 与视觉服务各自要 2.65 GB，同机峰值约 6 GB。15 GB 够用，2C4G 的小机器上必须二选一：关视觉腿，或让写侧走服务。

视觉模型在 CPU 上没有 bf16 加速（i5-13420H 无 `avx512_bf16`），bf16 会慢 4.6 倍（68 s/图 对 15 s/图），代码已钉 float32。换机器要重测。视觉页渲染的长边封顶 2048 px，这不是画质参数：colSmol 内部恒定把长边重采样到 2048 px 再切块，渲染更大一个 token 都多不出来；不封顶时超大页面会把 pymupdf 顶死（`Overly large image`），那一页的视觉任务永久失败。

## 巡检看什么？

```bash
uv run python deploy/monitor.py            # 人看
uv run python deploy/monitor.py --json     # 机读
uv run python deploy/monitor.py --quiet    # 只在有问题时输出
uv run python deploy/monitor.py --only df index
```

退出码 0 表示正常，1 表示有问题。巡检是三态：通过、失败、跳过。**跳过不算健康**——最常见的是 `.env` 里没有注册过的 `PKH_MONITOR_KEY`，跳过意味着这项没验；把它算成通过，巡检一半没跑成却报“一切正常”，是监控工具最坏的失败。

十四项检查覆盖：

| 检查 | 抓什么 |
|---|---|
| PostgreSQL 可连 | 库挂了全站 500 |
| Qdrant `/healthz` | 索引服务活着 |
| Qdrant 可列集合 | 不只活着，还能读 |
| API `/health` | 进程活着但依赖坏了 |
| BM25 df 一致 | 词表漂移，检索质量悄悄变差 |
| Embedding 模型一致 | 索引与查询用了不同模型，排序基础失效 |
| 索引对账（分腿） | dense 对、sparse 缺这类半坏状态 |
| 视觉腿对账 | `VDR_ENABLED=false` 时跳过而不是报红 |
| 卡住的文档 | 中间态却无待办任务 |
| 磁盘占用 | 写满会让摄取静默失败，上传仍返 202 |
| 卡住的任务 | `running` 超过 30 分钟 |
| 近 24 小时失败任务 | 有没有需要人看的失败 |
| 队列滞后 | 最老 pending 等太久 |
| 最近备份 | 超过 3 天等于事实上没有备份 |

cron 可以直接用退出码报警：

```bash
0 8 * * * cd /path/to/knowledge-hub && \
  uv run python deploy/monitor.py --quiet || mail -s '[PKH] 巡检异常' you@your.domain
```

## 备份恢复与蓝绿重建

```bash
bash deploy/backup.sh                    # 做一份，写完立刻自检
bash deploy/backup.sh --keep 7           # 只留最近 7 份，默认 14
bash deploy/backup.sh --list             # 列出备份与自检结果
bash deploy/backup.sh --verify DIR       # 只校验
```

备份 PostgreSQL（`pg_dump -Fc`）与 `data/blobs/`，不备份 Qdrant。索引可以从 PG 的 chunk 重建，但 manifest 里要记下每个 collection 的期望点数，重建后按它对账。每份备份目录里有 `manifest.json`：各表精确行数、每个产物的 sha256、Qdrant 各集合点数。校验会查文件哈希、tar 能否解开、dump 里有没有 `TABLE DATA` 条目——空的 `-Fc` dump 也会成功退出。

```bash
# 演练，推荐每月一次
bash deploy/restore.sh --scratch data/backups/<时间戳>

# 真恢复
bash deploy/dev.sh stop
bash deploy/restore.sh --force --into pkh data/backups/<时间戳>
```

演练会校验备份完整性，建临时库并 `pg_restore`，逐表对行数、逐个比 blob 的 sha256，最后删临时库，并提示重建索引后按 `qdrant_expect` 对账。实测过一次：18 张表行数全对、126 个 blob 的 sha256 全对。

蓝绿重建可选。默认逻辑名就是真实集合，重建时按库先删后建，正在重建的那个库短暂搜不到。打开 `QDRANT_ALIASES=true` 并跑过一次 `deploy/bootstrap_index_aliases.py` 之后，逻辑名变成别名，`POST /admin/reindex?swap=true`（需超管）会建影子集合、灌数据、验点数、原子切别名，旧集合保留回滚。视觉腿不参与重建，它要重读原件并跑 colSmol 前向，那是重新摄取。

## 对外访问与常见故障

对外只需要一个端口：把 `8010` 交给反向代理或隧道。API 已托管前端产物，`/mcp` 与 `/a2a/tasks` 在根路径，不在 `/api/v1` 下。加固清单：

| 该做 | 为什么 |
|---|---|
| 边缘再加一层访问控制 | 应用内只有 API Key 一道，它保护数据，不保护登录页与限流 |
| `ALLOW_REGISTER=false` | 否则陌生人可以自助建号 |
| 保持登录失败限流 | 这是唯一不需要凭证就能到达数据的入口 |
| 不暴露 `8020` | 视觉服务没有鉴权，只绑回环 |
| 不暴露 `5432` / `6333` | 数据库与向量库没有应用层租户概念 |
| 定期巡检与恢复演练 | 见上 |

无凭证时，`/api/v1/kb`、`/audit`、`/admin/*`、`/mcp` 与所有写端点返回 401；`/` 返回登录界面 200；`POST /auth/register` 在公网返回 403 `REGISTRATION_CLOSED`；`/docs`、`/redoc`、`/openapi.json` 返回 404；`GET /health` 返回 200，但不含队列与模型细节。

| 症状 | 真正原因 | 处理 |
|---|---|---|
| 改了代码但行为没变 | 旧 worker 子进程还在 | `bash deploy/dev.sh restart` |
| 检索只有 dense 的结果 | 三条 DB 腿共用了会话 | 看 `/search/debug` 的 `skipped`；确认引擎为每条腿各建会话 |
| 视觉腿一直 `skipped` | 服务没起、不可达，或 URL 为空 | 等 13 秒预热，或检查配置 |
| 上传返回 202 但文档不动 | 磁盘写满导致摄取静默失败 | 巡检磁盘项；清理 `/tmp` 与备份 |
| 文档停在中间态 | 无待办任务却未终态 | worker 启动补扫，或对这几份 `reparse` |
| 恢复演练提示 sha 不符 | 备份不完整或被改动 | 换一份备份，检查 `manifest.json` |
| 前端页图 401 | 用了 `<img src>` 而不是 fetch | 改成 `fetch` → blob → objectURL |
| 请求走错出口 | 代理设置 | 用 `--noproxy '*'`，或检查代理环境变量 |

## 相关文档

- 摄取与派生任务：[文档摄取](ingestion.md)
- 任务与文档状态涉及的表：[数据模型与一致性](data-model.md)
- 巡检与恢复在验收体系里的位置：[测试与验收](testing.md)
- 安全开关与对外加固：[多租户与权限](tenancy.md)
