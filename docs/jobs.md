# 任务队列与进程模型

> 本文讲“后台怎么把活干完”：任务表、领取协议、终态判定、三个进程与内存取舍。

---

## 1. 为什么是 Postgres 任务表，而不是 Celery / RQ

需求很具体：

- 进度条本来就要一份任务状态表；
- 待办任务放进去零成本；
- 单机、任务量不大，不需要独立消息中间件；
- 要用 `FOR UPDATE SKIP LOCKED` 保证多 worker 不抢同一行。

于是得到 `jobs` 表 + 轮询 worker。API 只负责入队与查询进度，worker 负责执行。

---

## 2. 任务类型

| kind | 谁跑 | 成本 | 说明 |
|---|---|---|---|
| `ingest` | worker | ~50 ms/页 | 解析 → 分块 → 嵌入 → 写 Qdrant 文本腿，**一次性做完** |
| `embed_visual` | worker | **15 s/图** | colSmol 编码，必须独立任务，否则 API 会卡 |
| `thumbnail` | worker | ~30 ms/页 | 预生成缩略图 |
| `pdf_preview` | worker | 变量 | Office 转 PDF，供预览与坐标 |
| `cleanup` | worker | 变量 | 过期数据、回收站物理清理 |
| `import_chat` | worker | 变量 | 服务端读 JSONL，按每 50 条消息切文档 |
| `import_dir` | worker | 变量 | 从服务器目录批量导入照片 |
| `reindex` | worker | 分钟级 | 重建索引 / 修 BM25 词表漂移 |

> 早期 `parse` / `chunk` / `embed` 是三个 kind，后来合并成 `ingest`：三步加起来不到 1 秒，拆开只是多三次事务，而且进度回传要跨三个任务拼装。

### 2.1 领取协议

```sql
UPDATE jobs SET status='running', locked_at=now(), locked_by=:worker_id, ...
WHERE id = (
  SELECT id FROM jobs
  WHERE status='pending'
    AND (locked_at IS NULL OR locked_at < now() - interval '5 min')
  ORDER BY created_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED
) RETURNING *;
```

- `FOR UPDATE SKIP LOCKED`：多个 worker 不会抢同一行；
- `locked_at < now() - 5 min`：worker 崩溃后任务能被重新领取；
- `max_attempts` 默认 3；
- 未注册的 kind **永远**不会成功，直接终态失败，不占重试额度。

### 2.2 空闲退避

worker 轮询间隔随空闲程度递增（1 s → 2 s → 5 s），有任务时立刻回到 1 s。空闲时只轮询 PG，几乎不耗 CPU。

---

## 3. 进度：100% 只归 `finish_job` 所有

```python
PROGRESS_NONTERMINAL_MAX = 0.999
```

每个 handler 结尾都会调 `progress(1.0)`，而它走独立事务；`finish_job` 在之后另一个事务里才把 `status` 翻成 `done`。两次提交之间存在一个窗口：读到的样本是 **`running` + `progress=1.0`**。

这个窗口只有几毫秒，所以 200+ 次运行里偶发一次——**“偶发红”比“稳定红”更贵**：下一个人会以为索引坏了，去查一个根本不存在的问题。

语义上也该这样：**“100%”是“完成了”的意思**，而“完成”由 `finish_job` 判定（含重试判定）。handler 说“我干完了”不等于任务真的完成。封顶 0.999 而不是丢弃这次上报：进度必须**单调不减**，直接丢会看到 0.67 → 1.00 的倒退。

---

## 4. 文档终态判定

`jobs.status` 与 `documents.status` 是**两份**状态。早期版本没有任何代码把前者的终态搬到后者，于是文档永远停在中间态：

| 任务终态 | 文档实际停在 | 用户看到 |
|---|---|---|
| `ingest` 重试耗尽 | `pending` / `parsing` / … | “一直在处理中” |
| `embed_visual` 全部失败 | `pending` | “一直在处理中” |

而下游全都已经准备好了：契约写着失败要写 `failed`、重试端点只接受 `failed`、前端徽章按 `failed` 画——**唯独那个状态从来不会被写进去**。

### 4.1 判定要“两件事都算”

```
1. 文档还在中间态吗？（终态的不许动，包括 failed）
2. 还有未完成的 ingest / embed_visual 吗？（有就还没到终态）
3. 该有的页级点齐了吗？
```

- **只看点数**：全失败时点数永远不够，于是永远 `pending`；
- **只看任务状态**：任务 `done` 只说明“没抛异常”，这一页的点到底在不在索引里才是事实。

所以两个都要看。

### 4.2 kind 白名单

挡住终态判定的只有 `ingest` 和 `embed_visual`：

- `thumbnail` / `pdf_preview` **不挡**：它们不影响“视觉页够不够”，算进去会让“缩略图还没生成”把一份页级点已经齐了的文档永远按在 `pending`；
- `ingest` **必须算**：它会重算 `scan_pages` 并派发视觉任务，它在排队就说明这份文档马上会被重新处理。

### 4.3 结算在事务之外，且不抛异常

```python
def settle_doc_state(kind: str, doc_id: int | None) -> None:
    if kind not in DOC_STATE_KINDS or doc_id is None:
        return
    try:
        with sync_session_scope() as s:
            reconcile_doc_state(s, doc_id)
    except Exception:
        logger.error(...)   # 只记日志，让巡检报出来
```

- **必须在 `finish_job` 的事务之外**：任务终态已经落库，如果绑在同一事务里，“改文档状态失败”会把 15 秒的模型前向一起回滚；
- **失败不抛**：调用点在 worker 主循环正中间，抛出去会被当成“任务失败”再走一遍 `finish_job(error=...)`，把一个 `done` 的任务改回 `pending` / 重试，是比原问题严重得多的副作用。

代价是“任务成功但文档还是中间态”的窗口，由启动补扫（`sweep_stuck_docs`）与巡检兜底。

> **“没有待办任务却还在中间态”是不可能的状态。** worker 启动时会补扫一次；巡检里也有“卡住的文档”这一项。

---

## 5. 三个进程

### 5.1 API

- 监听 8010；
- REST / SSE / MCP / A2A；
- 托管 `web/dist`（存在才挂载）；
- **恒不加载视觉模型**。

### 5.2 Worker

- 无监听端口；
- 轮询任务、执行 handler、结算文档状态；
- 默认本地加载视觉模型做写入编码（峰值 ~2.8 GB）。

### 5.3 视觉查询服务

- 监听 8020，只绑 `127.0.0.1`；
- 常驻 colSmol，启动即预热（约 13 s，RSS 2.71 GB）；
- 提供 `POST /encode`（文本）与 `POST /encode_image`（图片）；
- **不查 Qdrant、不做租户过滤**；
- 只做编码，失败时 API 侧把视觉腿记为 `skipped`，检索照常 200。

`dev.sh` 的起停委托给 `deploy/visual.sh`：一份实现，避免两套 pid / 日志 / 重启策略漂移。

---

## 6. 多 worker 与写侧改造

默认形态下，worker 自己加载 colSmol。同机跑 N 个 worker 会把 2.65 GB 乘以 N，内存吃不消。

可选形态：

```bash
VISUAL_WRITE_VIA_SERVICE=true
PKH_WORKERS=4 bash deploy/dev.sh start
```

此时写侧也走 `visual_service`，worker 不再加载模型（实测 RSS 降到 ~991 MB），于是同机能跑多个 worker。代价是**摄取从此依赖该服务可用**，所以默认关闭，按机器规格决定。

| 形态 | worker RSS | 前提 | 适合 |
|---|---|---|---|
| 默认 | 峰值 ~2.8 GB | 无 | 单 worker、小机器 |
| 写侧走服务 | ~991 MB | 视觉服务必须在线 | 多 worker、内存宽裕 |

---

## 7. 视觉编码的成本特性

| 指标 | 值 | 含义 |
|---|---|---|
| 模型加载 | 12.8 ~ 15.0 s | 启动即预热，就绪约 13 s |
| 单图编码 | **≈15 s/图**（float32） | 与分辨率无关；内部固定切 13 个 512×512 图块 |
| 查询文本编码 | 119 ~ 128 ms/条 | p95 157 ms |
| 6 路并发 | 墙钟 0.96 s / CPU 8.36 s | 并行度 8.7× |
| 常驻内存 | 2.64 GB | 小机器上必须与 worker 二选一 |

本机 CPU 没有 `avx512_bf16`（i5-13420H），colSmol 走 bf16 会慢 4.6 倍（68 s/图 vs fp32 15 s）。代码已钉 float32，换机器要重测。

### 7.1 渲染尺寸的安全阀

视觉页渲染的长边封顶 **2048 px**。这不是画质参数，而是安全阀：

- colSmol 内部恒定把长边重采样到 2048 px 再切图块，渲染更大**一个 token 都多不出来**；
- 不封顶时，工程图纸 / 海报 / CAD 导出这种超大页面会把 pymupdf 顶死（`Overly large image`），这一页的视觉任务永久失败，纯扫描件文档永远停在 `pending`。

---

## 8. 失败与重试

| 情况 | 处理 |
|---|---|
| handler 抛可重试异常 | `attempts + 1`，回到 `pending`；超过 `max_attempts` 置 `failed` |
| 不可重试异常 | 直接 `failed`，不浪费重试额度 |
| 未注册的 kind | 直接 `failed` |
| worker 崩溃 | `locked_at` 超时（5 分钟）后可被重新领取 |
| 文档停在中间态 | worker 启动补扫 + 巡检“卡住的文档” |
| 任务长时间 `running` | 巡检“卡住的任务”（超过 30 分钟） |

---

## 9. 改代码之后：先杀干净旧进程

`uv run X` 会产生 **2 个进程**（wrapper + 真正执行的 python）。只 kill 进程组 leader 杀不干净子进程，旧 worker 会继续抢任务、用旧代码处理，症状是“改了没生效”。

```bash
bash deploy/dev.sh restart
```

`dev.sh` 只有 `start` / `restart` / `stop` 三个子命令（没有 `status` / `logs`）。看状态：

```bash
curl -s --noproxy '*' http://127.0.0.1:8010/health
tail -20 /tmp/pkh_api.log /tmp/pkh_worker.log /tmp/pkh_visual.log
```

---

## 10. 相关文档

- 摄取细节：[文档处理与切分](./ingestion.md)
- 终态相关的表结构：[数据模型与存储](./data-model.md)
- 巡检与备份：[部署与运维](./operations.md)
