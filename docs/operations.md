# 部署与运维

> 部署形态：**单机自部署**。三个进程 + 两个存储，全部只绑本机。
>
> **本文定位**：部署与运维的读者视图。脚本级细节（各脚本参数、蓝绿重建、对外加固清单）
> 以 [`deploy/README.md`](../deploy/README.md) 为准。

---

## 1. 基础设施

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d
curl -s http://127.0.0.1:6333/collections          # Qdrant
docker exec pkh-postgres pg_isready -U pkh         # Postgres
```

| 服务 | 端口 | 绑定 |
|---|---|---|
| PostgreSQL | 5432 | 127.0.0.1 |
| Qdrant REST | 6333 | 127.0.0.1 |
| Qdrant gRPC | 6334 | 127.0.0.1 |

版本说明：

- **Qdrant 锁定 `v1.15.1`**：命名向量创建、多向量检索端点格式、稀疏向量字段名都踩过版本相关的坑，换版本会重踩；
- **PostgreSQL 用 `pgvector/pgvector:pg16`**：装 pgvector 只是为了顺带拿到 `pg_trgm`。

> 拉 Docker 镜像需要代理时，独立配置 docker daemon（daemon 不读 shell 的 `HTTP_PROXY`）：
>
> ```bash
> sudo mkdir -p /etc/systemd/system/docker.service.d
> sudo tee /etc/systemd/system/docker.service.d/proxy.conf <<'EOF'
> [Service]
> Environment="HTTP_PROXY=http://127.0.0.1:7890"
> Environment="HTTPS_PROXY=http://127.0.0.1:7890"
> Environment="NO_PROXY=localhost,127.0.0.1,10.0.0.0/8"
> EOF
> sudo systemctl daemon-reload && sudo systemctl restart docker
> ```

---

## 2. 三个进程

```bash
bash deploy/dev.sh start      # 起 API(8010) + worker + 视觉查询服务(8020)
bash deploy/dev.sh restart    # 改了后端代码之后用这个
bash deploy/dev.sh stop

bash deploy/visual.sh {start|stop|restart|status|logs}   # 视觉服务单独管
PKH_WORKERS=4 bash deploy/dev.sh start                   # 多 worker（需 VISUAL_WRITE_VIA_SERVICE=true）
```

⚠️ `dev.sh` **只有** start / restart / stop 三个子命令（没有 status / logs）。

```bash
curl -s --noproxy '*' http://127.0.0.1:8010/health
tail -20 /tmp/pkh_api.log /tmp/pkh_worker.log /tmp/pkh_visual.log
```

- 视觉查询服务**首次启动要 13 秒**（加载 colSmol，约 2.65 GB）；`dev.sh` 等不到它也会正常退出——那段时间视觉腿降级成 `skipped`，检索照常；
- 不想装：`.env` 里 `VISUAL_SERVICE_URL=`（留空）关掉读侧，写入侧不受影响；
- 改过前端要 `cd web && npm run build`（`dev.sh` 不会替你构建）。

### 2.1 首次准备

```bash
uv run python -m services.api.scripts.create_user   # 建账号（自助注册默认关）
uv run python deploy/seed_photos.py                 # 持久照片库（幂等）
```

### 2.2 改代码之后

`uv run X` 会产生 2 个进程（wrapper + 真正的 python）。只 kill 进程组 leader 杀不干净子进程，旧 worker 会继续用旧代码处理任务，症状是“改了没生效”。

```bash
bash deploy/dev.sh restart
```

---

## 3. 关键环境变量

| 变量 | 作用 |
|---|---|
| `ALLOW_REGISTER` | 自助注册开关（默认开；关掉后新建账号走本机 CLI） |
| `EXPOSE_DOCS` | `/docs` / `/redoc` / `/openapi.json` 开关（关掉后路由不存在） |
| `VISUAL_SERVICE_URL` | 视觉查询服务地址；留空 = 关掉读侧视觉腿 |
| `VISUAL_WRITE_VIA_SERVICE` | 写侧也走视觉服务（默认关） |
| `PKH_WORKERS` | worker 数量 |
| `PKH_API_PORT` | API 端口（默认 8010） |
| `QDRANT_ALIASES` | 别名模式与蓝绿重建（默认 false） |
| `IMPORT_ROOTS` | 导入路径白名单 |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | 模型离线加载（dev.sh / conftest / eval 都设） |
| `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` | 每个进程的 PostgreSQL 连接池（默认 5 + 5）|

⚠️ 环境变量**没有前缀**（`Settings` 没有 `env_prefix`）。两个安全开关**默认都是开**：本机开发、pytest、三套 eval 都靠自助注册；默认关会让基线全红。

### 3.1 连接池：并发检索的隐性预算

这里的"连接"是 **API / worker 进程到 PostgreSQL 的连接池连接**（asyncpg TCP 连接），
不是浏览器到 API 的 HTTP 连接，也不是 Qdrant 连接。连接池是**进程级**的：每个
uvicorn worker、每个 worker 进程、CLI 各有一份。

`pg_pool_size`（默认 5）+ `pg_max_overflow`（默认 5）= 每进程最多 **10 条**。
一次 `hybrid` 检索最多占 **4 条**（请求会话 1 + dense/sparse/trgm 各 1）；
`semantic` / `exact` 只占 **2 条**（请求会话 + 一条腿）。

⇒ 池大小至少按 **并发检索数 × 4 + 余量** 估。⚠️ 池不够时症状是**排队变慢**
（`pool_timeout`，默认 30s），不是算错；极端情况下连接都被请求会话占着、每个请求
又在等腿的连接，会卡到超时。高并发部署先调这两个值，或给多 worker 留足每进程的池。

---

## 4. 内存预算

| 进程 | 常驻 | 峰值 |
|---|---|---|
| api | ~400 MB | ~600 MB |
| worker | ~200 MB（未编码） | ~2.8 GB（加载视觉模型后） |
| visual_service | 2.71 GB | ~2.8 GB |
| postgres | ~200 MB | ~500 MB |
| qdrant | ~300 MB | ~800 MB |

worker 与视觉服务**各自**要 ~2.65 GB ⇒ 同机峰值约 6 GB。15 GB 够；小机器上二选一：要么关视觉腿，要么 `VISUAL_WRITE_VIA_SERVICE=true` 让 worker 不再加载模型（实测 RSS ~991 MB），代价是摄取依赖视觉服务。

---

## 5. 十四项巡检

```bash
uv run python deploy/monitor.py            # 人看
uv run python deploy/monitor.py --json     # 机读（cron / CI）
uv run python deploy/monitor.py --quiet    # 只在有问题时输出
uv run python deploy/monitor.py --only df index
```

退出码 `0` = 正常，`1` = 有问题（**跳过也算**）。

| 检查 | 抓的是 |
|---|---|
| PostgreSQL 可连 | 库挂了 → 全站 500 |
| Qdrant `/healthz` + 可读 | 索引挂了 → 检索降级、摄取全失败 |
| API `/health` | 进程活着但依赖坏了 |
| **BM25 df 一致**（`n_docs` vs 活文档 chunk 数） | **不是报错，是检索质量悄悄变差** |
| **索引对账**（`/admin/consistency` 的分腿 `legs`） | 半坏状态（dense 对、sparse 缺） |
| 磁盘占用 | 🔴 写满会让摄取**静默失败**（上传仍返 202） |
| 卡住的任务（`running` 超过 30 分钟） | worker 卡了没人知道 |
| 卡住的文档（停在中间态却无待办任务） | 终态判定的兜底 |
| 视觉腿对账 | `VDR_ENABLED=false` 时**跳过**而不是报红 |
| 最近有备份（≤3 天） | 事实上没有备份 |
| 其它若干项 | 队列、模型、配置一致性等 |

⚠️ **三态：通过 / 失败 / 跳过。** “跳过” = 这项没法验（最常见是 `.env` 里没有注册过的 `PKH_MONITOR_KEY`）。跳过**不算健康**（`ok=false` 且 `counts.skipped>0`）——巡检一半没跑成却报“一切正常”，是监控工具最坏的失败。

cron 示例：

```bash
0 8 * * * cd /path/to/knowledge-hub && \
  uv run python deploy/monitor.py --quiet || mail -s '[PKH] 巡检异常' you@your.domain
```

> 退出码有意义才有“报警”可言——`--quiet` 只影响输出，退出码照常。

---

## 6. 备份与恢复

```bash
bash deploy/backup.sh                    # 做一份（写完立刻自检）
bash deploy/backup.sh --keep 7           # 只留最近 7 份（默认 14）
bash deploy/backup.sh --list             # 列出已有备份（含自检结果）
bash deploy/backup.sh --verify DIR       # 只校验
```

| 内容 | 备份 | 理由 |
|---|---|---|
| `pg_dump -Fc` | ✅ | 唯一的权威数据源 |
| `data/blobs/` | ✅ | 内容寻址的原件；缺了就是“文档还在、文件没了” |
| Qdrant | ❌ | **索引可重建**——但**期望点数记进 manifest**，重建后按它对账 |

每份备份目录里有 `manifest.json`：各表精确行数、每个产物的 sha256、Qdrant 各 collection 的点数。校验会查文件哈希、tar 能否解开、**dump 里有没有 `TABLE DATA` 条目**（空的 `-Fc` dump 也会成功退出）。

```bash
# ① 演练（推荐，每月一次）
bash deploy/restore.sh --scratch data/backups/<时间戳>

# ② 真恢复
bash deploy/dev.sh stop
bash deploy/restore.sh --force --into pkh data/backups/<时间戳>
```

演练会：校验备份完整性 → 建临时库并 `pg_restore` → **逐表行数对账** → **逐个 sha256 校验原件** → 删临时库，并提示“重建索引后按 `qdrant_expect` 对账”。已实测过一次：18 张表行数全对、126 个 blob sha256 全对。

---

## 7. 对外访问

部署形态：`<你的域名>` → Cloudflare Tunnel → 本机 `127.0.0.1:8010`。

| 事实 | 怎么验的 |
|---|---|
| 走 Cloudflare 边缘 | `curl -sI https://<域名>/api/v1/health` → `server: cloudflare`、`cf-ray` |
| 请求确实到应用 | 同一响应里有 `x-request-id` 与 `x-elapsed-ms` |
| tunnel agent 在本机手起 | `pgrep -a cloudflared` → `--token-file /etc/cloudflared/token` |
| 🔴 **没有 Access** | 不带凭证的请求被转达到应用（没有 302 到 `*.cloudflareaccess.com`） |

### 7.1 现在起访问界面

`web/dist` 由 FastAPI 托管：`cd web && npm run build` 后 `bash deploy/dev.sh restart` 即挂载（不存在时不挂，API 照常）。

- `https://<域名>/` → 界面（深链 `/kb/123`、`/settings` 刷新也正常）；
- `/api/v1/*`、`/mcp`、`/a2a`、`/health` → 真端点（打错仍是 404 JSON）；
- ⚠️ CORS 仍只允许 `http://localhost:5173` / `127.0.0.1:5173`。

### 7.2 无凭证姿态

| 探测 | 状态 |
|---|---|
| `/api/v1/kb`、`/audit`、`/admin/*`、`/mcp`、所有写端点 | **401** |
| `/`（登录 / 注册界面） | 200——你要用它，必须公开 |
| `POST /auth/register` | **403 `REGISTRATION_CLOSED`**（公网关闭自助注册） |
| `POST /auth/login` | 允许，但失败 10 次/300s（按 IP）或 20 次/900s（按账号）→ 429 |
| `/docs`、`/redoc`、`/openapi.json` | **404**（已关闭） |
| `GET /health` | 200（公开版不含队列细节） |

### 7.3 为什么不挂 Cloudflare Access（已决议）

1. **登录限流与 Access 无关**——通过 Access 的账号照样没有次数上限；
2. 本项目是**个人单用户**实例，API Key 鉴权已在应用层，Access 多引入一条外部配置；
3. `/mcp` 在**根路径**且不在 `/api/v1` 下，Access 的 cookie 对程序无效，客户端得再带 Service Token。

⚠️ 如果以后要上，清单在云隧道的 Access 文档里；生效后本机 `curl` 探测也会被挡，验收要在浏览器里做。

---

## 8. 常见故障

| 症状 | 真正原因 | 处理 |
|---|---|---|
| 改了代码但行为没变 | 旧 worker 子进程还在 | `bash deploy/dev.sh restart` |
| 检索返回 200 但只有 dense 的结果 | 三条 DB 腿**共用**会话，并发 `execute` 失败 | 看 `/search/debug` 的 `skipped`；确认引擎为每条 DB 腿各建会话（`tests/test_leg_concurrency.py`）|
| 视觉腿一直 `skipped` | 服务还没起来 / 不可达 / `VISUAL_SERVICE_URL` 为空 | 等服务预热（13 s）或检查配置 |
| 上传返回 202 但文档不动 | 磁盘写满导致摄取静默失败 | 巡检“磁盘占用”；清理 `/tmp` 与备份 |
| 文档停在中间态 | 无待办任务却未终态 | worker 启动补扫 + 巡检“卡住的文档” |
| 恢复演练提示 sha 不符 | 备份不完整 / 被改动 | 换一份备份，检查 `manifest.json` |
| 前端页面 401 | 页图端点需要 Bearer，却用了 `<img src>` | 改成 `fetch` → blob → objectURL |
| 磁盘莫名被占 | headless Chrome 的临时 profile 泄漏（每轮约 150 MB） | 清理临时目录 |
| 请求走错出口 | 代理设置 | 用 `--noproxy '*'` 或检查代理环境变量 |

---

## 9. 相关文档

- 巡检与备份在验收体系里的位置：[测试与验收体系](./testing.md)
- 任务与进程：[任务队列与进程模型](./jobs.md)
- 安全开关与权限：[多租户与权限](./tenancy.md)
