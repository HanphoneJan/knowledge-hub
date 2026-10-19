# deploy/ — 部署、运维与验证脚本

> 本文件讲**通用做法**：任何一台机器 + 一个反向代理/隧道都能照此部署。

## 目录里有什么

| 文件 | 干什么 | 在基线里吗 |
|---|---|---|
| `dev.sh` | 起/停三个进程（`start` / `restart` / `stop`；**只有这三个子命令**）| ✅ |
| `visual.sh` | 视觉查询服务**单独**起停（`start` / `stop` / `restart` / `status` / `logs`）；`dev.sh` 委托它 | ✅ |
| `ci.sh` | 不需要外部服务与模型的门禁（CI 与本地同一份命令）| ✅ |
| `monitor.py` | 十四项巡检（队列、索引对账、磁盘、卡住的文档…）；**"跳过"不算健康** | ✅ |
| `backup.sh` / `restore.sh` | 备份 PG + blobs（Qdrant 不备份，靠重建）；恢复到**临时库**并逐表对账 | 手工 |
| `seed_photos.py` | 造持久照片库（幂等）；账号与口令从 `.env` 读 | 手工 |
| `measure_visual_leg.py` | 量视觉腿的成本与收益（含并发那一维）| 手工 |
| `verify_*.py` | 端到端手工验证（上传/检索/视觉降级…）| 手工（**不在基线**）|
| `docker-compose.yml` | 只起基础设施：`postgres`(pgvector) + `qdrant` | 手工 |

## 起服务

```bash
# 1) 基础设施（只有这两个容器；应用不进容器）
docker compose -f deploy/docker-compose.yml up -d

# 2) 应用三个进程：API(8010) · worker · 视觉查询服务(8020)
bash deploy/dev.sh start
bash deploy/dev.sh restart    # 改了后端代码之后必须用这个

# 2b) 视觉服务也可单独管理（脚本化部署，不用 systemd）
bash deploy/visual.sh start|stop|restart|status|logs

# 3) 前端构建产物由 API 托管（dev.sh 不会替你构建）
cd web && npm install && npm run build
```

| 端口 | 谁 |
|---|---|
| `8010` | API（同时托管前端构建产物与 `/mcp`、`/a2a/tasks`）|
| `8020` | 视觉查询服务：**只做编码**，常驻 colSmol（约 2.65GB）|
| `5173` | 只有本地开发才需要（`cd web && npm run dev`）|
| `5432` / `6333` | Postgres(pgvector) / Qdrant |

⚠️ **三个进程必须用 `dev.sh` / `visual.sh` 管**：`kill -- -PID` 杀不干净 `uv run` 派生的子进程，
残留的旧 worker 会继续用**旧代码**领任务，症状是"改了没生效"。
视觉服务的生命周期**只有一份实现**（`visual.sh`），`dev.sh` 委托它 —— 单独起停与整套编排不会漂移。

## 备份与恢复（唯一一条"没验证过就等于没有"的纪律）

```bash
bash deploy/backup.sh                        # 备份 + 自检（生成 manifest）
bash deploy/restore.sh --scratch data/backups/<时间戳>   # 恢复到临时库 → 逐表对账 → 逐个 sha256
```

- Qdrant **不参与备份**：向量可以从 PG 里的 chunk 重建（`POST /admin/reindex`），
  所以备份只要保证 **PG + blobs** 完整。
- 「恢复到临时库再对账」是**演练**，不是可选步骤：只跑 `backup.sh` 看它"成功"证明不了能恢复。
- 生产恢复（覆盖正式库）必须先停 API 与 worker —— 脚本会拒绝在服务运行时覆盖。

## 巡检

```bash
uv run python deploy/monitor.py    # 十四项；退出码 1 = 有项不健康
```

- **"跳过"不算健康**：跳过意味着"这项没验"，把它算成通过等于自欺。
- 索引对账**分腿报**（dense / sparse / visual 各一条），只看顶层会因为并集口径而漏掉"只有一条腿坏了"。

## 蓝绿重建（可选：别名模式）

想让**全量重建时旧索引仍可读**（零停机），开别名模式：

```bash
uv run python deploy/bootstrap_index_aliases.py --yes   # 一次性：真实集合 → 物理集合 + 别名
# 在 .env 设 QDRANT_ALIASES=true 并重启 API / worker
# 之后重建：POST /api/v1/admin/reindex?swap=true（需超管）
```

- 建影子集合 → 灌数据 → 验点数 → **原子切别名** → 旧集合保留（回滚用）。
- 默认**不开**：逻辑名就是真实集合，行为与以往完全一致；不做破坏性迁移是默认选择。
- 别名**不出现在 `GET /collections`**，所以巡检里列的是物理集合名 —— 正常。

## 对外访问（通用形态与加固清单）

对外只需要**一个端口**：把 `8010` 交给反向代理或隧道即可（API 已托管前端产物；
`/mcp` 与 `/a2a/tasks` 在根路径，不在 `/api/v1` 下）。

| 该做 | 为什么 |
|---|---|
| 边缘再加一层访问控制（隧道侧的 Access / 反代 basic auth / 只允许局域网）| 应用内的鉴权只有 **API Key** 一道，它保护的是数据，不保护登录页与限流 |
| `ALLOW_REGISTER=false` | 否则陌生人可以自助建号（`.env` 里默认按部署场景决定）|
| 登录失败限流保持开启 | 这是唯一不需要凭证就能到达的数据入口 |
| **不要**暴露 `8020`（视觉服务）| 它没有鉴权，只绑 `127.0.0.1`；指到 `0.0.0.0` 等于对外开放一个模型服务 |
| **不要**暴露 `5432` / `6333` | 数据库与向量库没有应用层租户概念 |
| 定期跑 `monitor.py` + 每月做一次恢复演练 | 见上 |

⚠️ 视觉服务是**可选**的：`VISUAL_SERVICE_URL=`（留空）就关掉视觉腿，检索照常返回
（那一条腿如实标 `skipped`）。小内存机器上这是推荐的默认。
