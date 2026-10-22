# 硬约束与静默失败清单

这个项目最贵的 bug 不是报错，而是**不报错、结果却错了**。下面每一条都来自真实故障，改代码前先读。

## 五条硬约束

1. **运行时只有一棵依赖树。** 依赖方向是 `web/ → services/api → services/common → services/retrieval → services/ingest`。没有第二套实现。判定方式是扫全仓库 AST 找定义点，能抓住“有人在第三个地方又写了一份”。
2. **租户过滤没有第二个地方。** 入口全在 `services/common/auth.py`：`build_qdrant_filter`（Qdrant）、`build_kb_scope_sql`（PostgreSQL，trgm 腿）、`visible_kb_clause` 与 `writable_kb_clause`（PostgreSQL，列表与回收站）。前两个同源。
3. **检索唯一实现在 `services/retrieval/`。** API、MCP、A2A 三方共用。
4. **无权限等于不存在。** 返回同一个错误码 `KB_NOT_FOUND` / 403，不能通过错误码差异探测资源存在性。
5. **API Key 是唯一租户来源。** 请求体不接受 `user_id`；全局角色也从用户行经 API Key 推导。

## 三档可见性：不要写进一个 IN

这是已经真实发生过一次的越权。

```python
# ❌ 两个独立漏洞
or_(visibility.in_(("workspace", "global")), created_by == me)
```

一是所有 workspace 库对任何用户可见（少了“我是该工作区成员”），二是同工作区成员能看到别人的 private 库（private 被混进同一个 IN）。`IN` 表达“任一条件成立即可”，而可见性要表达“不同档位有各自不同的限定条件”，语义不匹配。

修复时最容易犯的二次错误：保留裸的 `workspace` 分支，再把带成员限定的分支追加成另一条 `or`。这样限定条件完全无效，等于没改。两个条件必须在同一个 `and_` 里。

通则：任何“附加的限定条件”都要先问——它是 `or` 的一项，还是 `and` 的一项？给工作区可见性加“工作区还活着”时，先写成第四条 `or`，于是只要库里存在任意一个活工作区，每一行都通过；后来又踩了 `EXISTS` 不 correlate。两次 pytest 全绿，都是端到端加真库探测抓住的。

还有一条：软删的行必须先按行取、再判权限。用 `readable_kb_ids` 过滤回收站列表会恒为空，因为软删行按定义不在集合里；而且要用写判据，不是读判据，否则会出现“列表里有、点恢复 403”。

## 检索侧

**`skipped` 是字符串，不是布尔。** 调试面板里的 `legs[].skipped` 必须带可操作的原因。一条空的 `skipped: false` 会让人查错方向。“查询侧没跑”与“写入侧没数据”在数据面上一样，都是这一腿 0 条，所以能力检测只回答“本进程能不能跑”，不看集合里有没有点。

**三条 DB 腿不能共用一个会话。** 把 `dense`、`sparse`、`trgm` 交给同一个 `AsyncSession` 并发 `execute`，SQLAlchemy 会抛 `This session is provisioning a new connection; concurrent operations are not permitted`。后果极其隐蔽：sparse 与 trgm 被记成 `skipped`，`hybrid` 静默降级成 dense 单腿，接口照样 200。修法是每条 DB 腿从会话工厂各拿一个会话；不要退回“共用会话加锁串行”，那能跑对，但把并发收益丢了。代价是一次 `hybrid` 最多占 4 条连接。

**RRF 的 key 必须带命名空间。** 文本点与页级点 ID 空间独立，直接拿点 ID 做 key 会互相抵消。排序还要有确定性 tiebreaker，否则浮点并列时同一个 query 两次请求的顺序会抖动，前端引用角标会在刷新后对调。

**日期问题可能出在分词。** `2025-02-14` 与“2025年2月14日”没有共同 token，而 BM25 无法表达多词共现。加归一化 token `d20250214` 后，该类查询 R@10 从 0.20 升到 1.00。

**trgm 只能用 `word_similarity`。** `ILIKE` 与整段 `similarity()` 都会静默漏召回，各返回 0 条，因为两者都要求 query 与整段文本可比。把两者 OR 起来也漏，放宽的是同一个错误判据。代价是全表扫描；慢能被发现，漏召回不能。

**Embedding 模型必须一致。** 不同模型的向量空间不通用，混用不是“结果随机”，而是排序基础整个坏了，且不报错。建索引时写 `index_models`；dense 腿查询前校验，不一致就 `skipped` 并说清“索引用哪个、现在配的哪个、怎么修”。`index_models` 空表表示未知，放行；未知不等于不一致。

**BM25 的统计量必须有统一减法入口。** `terms` 与 `corpus_stats` 在入库时加、删除时减。任何绕开统一删除入口的路径都会让它虚高，idf 被压平，罕见词不再罕见。表现是质量下降，不是报错，所以对账里专门有一条：`chunks` 行数应等于 `corpus_stats.n_docs`。

## 摄取与坐标

**扩展名在数据库行里。** 内容寻址 blob 没有扩展名。从 `path.suffix` 取后缀会让解析器与页图端点各炸一次。解析入口强制要求传 `file_name`。

**画框乘回 point，不是像素。**

```python
shape.draw_rect(Rect(nx0 * pix.width, ny0 * pix.height, ...))              # ❌ 框整体下移约半行
shape.draw_rect(Rect(nx0 * page.rect.width, ny0 * page.rect.height, ...))  # ✅
```

**形态按 `locations` 的形状分派，不按 `kind`。** 对外引用类型只有 `text` / `page` / `image`，而块形态有四种，所以表格块会以 `kind="text"` 加带 `row` / `col` 的框出现。有 row / col 就是网格，`kind === "page"` 是整页，其余按单框。

**坐标与被渲染页面必须同源。** Office 没有原生坐标，走“转 PDF，在转换版上算坐标”。画框用哪个文件由调用方决定，底图渲染函数不许自己去找原件。定位覆盖率低于 60% 时不给框，而不是给一个错位的框。

**表格坐标取自 `rows[i].cells`，不是扁平的 `cells`。** `len(tbl.cells) != row_count * col_count`，合并单元格会让长度对不上。文本用 `extract()`，坐标用 `rows[i].cells`，按行号对齐。

**页图端点要 Bearer，前端不能用 `<img src>`。** `<img>` 带不了自定义请求头，直接写会静默 401，只在控制台看得到。要用 `fetch` 拿 blob 再 `createObjectURL`，卸载时释放。

## 任务与状态

**进度 100% 只归 `finish_job`。** handler 结尾的 `progress(1.0)` 与 `finish_job` 是两次提交，中间有一个几毫秒窗口，会读到 `running + progress=1.0`。偶发红比稳定红更贵。所以非终态进度封顶 0.999，且必须单调不减。

**文档终态要两件事都算。** 只看任务状态，部分失败会被报成 `ready`；只看索引点数，全失败时永远 `pending`。判定要同时看“任务是否全部终态”和“索引里实际有多少点”，并且结算在 `finish_job` 事务之外、不抛异常。

**挡终态的 kind 只有 `ingest` 与 `embed_visual`。** `thumbnail` 与 `pdf_preview` 不挡，否则“缩略图还没生成”会把一份页级点已经齐了的文档永远按在 `pending`。

**“没有待办任务却还在中间态”是不可能的状态。** 它要么是终态判定漏了，要么是任务被删了。worker 启动补扫，巡检也有这一项。

**kind 变更要登记三处。** `services/common/jobs.py` 的 `ALL_KINDS`、`services/worker/handlers/__init__.py` 的 `HANDLERS`、`services/common/models.py` 的 CHECK 约束，外加一条手写迁移。第三处最容易漏。

## 数据库与异步

**时间列一律带时区。** 裸 `mapped_column(default=utcnow)` 会被 autogenerate 生成 `TIMESTAMP WITHOUT TIME ZONE`。统一用 `DateTime(timezone=True)`。

**同步与异步 session 工具必须分成两个名字。**

```python
def register_blob(session, ...): ...             # worker，同步 Session
async def register_blob_async(session, ...): ... # API，AsyncSession
```

在 `AsyncSession` 上调同步的 `session.execute()` 不报错，只返回一个从未被 await 的 coroutine。守卫 `tests/test_unawaited_coroutines.py` 用 AST 扫全 `services/`。同一个坑还出现在 `enqueue`、`purge`、`unregister_chunks` 上。

**SSE 生成器里不许碰 DB session。** FastAPI 在 `yield` 依赖上把退出码放在发送响应之前执行，所以 `DbSession` 在响应体开始流式之前就已经 commit 并关闭。铁律是：检索与取生成器全部在开流之前用请求级 session 完成；流里只做 LLM 的 HTTP 往返；问答记录的落库在流结束后新开 `session_scope()`，两条一起写。违反第二条，`session.execute()` 的 coroutine 永远不会被 await，消息静默丢失。

**覆盖式更新后旧向量不留 30 天。** 同名新版本顶替旧版本后，旧点由周期清理任务在 5 分钟宽限后删掉。恢复一律重新摄取，旧向量从来不会被复用。

## 文档与仓库

- 入口文档里的路径、命令、端口、基线数字必须对得上，守卫是 `tests/test_docs_pointers.py`。
- `docs/` 里不放脚本，只放文档；真实数据不进已跟踪文件，守卫是 `tests/test_no_real_data.py`。
- 代码与文档冲突时以代码为准，并把文档改过来。
- 备份与截图都别拿真实数据。

## 判据与测试纪律

- **判据要指向性质，不是中间状态。** 判了某行实现字面量，改实现就会误红。
- **AST 类断言必须做变异验证。** 故意改坏一处，确认测试真的会红；还要单独判定 pytest `exit 5`，那是“没收集到测试”，不是通过。
- **抽了 helper 必须接线。** 不接线的 helper 等于没有。
- **“仍然全绿”要分清**是判据弱、变异无效，还是 `-k` 指错。

## 本地环境类坑

| 症状 | 真正原因 | 处理 |
|---|---|---|
| 改了代码没生效 | 旧 worker 子进程 | `bash deploy/dev.sh restart` |
| 请求走错出口或代理报错 | 代理环境变量 | 用 `--noproxy '*'`，或检查代理 |
| 磁盘莫名被占 | 临时目录里浏览器 profile 泄漏 | 清理临时目录 |
| `got Future ... attached to a different loop` | 事件循环混用 | 检查 async 测试与共享 loop |
| 模型加载每次约 23 秒 | 没设离线环境变量 | 设 `HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1` |
| 检索质量悄悄变差 | BM25 统计量漂移，或某条腿 skipped | 对账分腿看；跑检索回放 |

`pytest.ini` 的节名写错会让整份配置都不生效。写 async 测试时按同步函数加模块级共享 loop 的约定办。

## 相关文档

- 进程、存储与边界：[架构](architecture.md)
- 可见性与审计的完整规则：[多租户与权限](tenancy.md)
- 检索与坐标细节：[检索与问答](retrieval.md) 与[文档摄取](ingestion.md)
