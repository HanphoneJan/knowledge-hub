# 工程约定与踩坑

> 本文是**约定**，不是建议。带 ⚠️ 的条目都是踩过坑之后立的规矩。

---

## 1. 五条硬约束（违反即错）

1. **运行时只有一棵依赖树**

   ```
   web/ → services/api → services/common → services/retrieval → services/ingest
   ```

   没有第二套实现。判定“只有一份实现”的方式是**扫全仓库 AST 找定义点**（能抓住“有人在第三个地方又写了一份”）。顶层目录有白名单，被钉死在测试里。

2. **租户过滤没有第二个地方**：入口全在 `services/common/auth.py`——`build_qdrant_filter`（Qdrant）、`build_kb_scope_sql`（PG，trgm 腿）、`visible_kb_clause` / `writable_kb_clause`（PG，KB 列表与回收站）。前两个**同源**。

3. **检索唯一实现在 `services/retrieval/`**，API / MCP / A2A 三方共用。

4. **无权限 ≡ 不存在**，返回同一个错误码（`KB_NOT_FOUND` / 403）。

5. **API Key 是唯一租户来源**，请求体不接受 `user_id`；全局角色（超管）一样从用户行经 API Key 推导。

---

## 2. 三档可见性：永远不要写进一个 `IN`

这是**已经真实发生过一次的越权漏洞**。

```python
# ❌ 两个独立漏洞
or_(visibility.in_(("workspace", "global")), created_by == me)
#   1. 所有 workspace 库对任何用户可见
#   2. 同工作区成员能看到别人的 private 库
```

`IN` 表达“任一条件成立即可”，而可见性要表达“不同档位有各自不同的限定条件”，语义不匹配。

修复时最容易犯的二次错误：保留裸的 `workspace` 分支，把带成员资格限定的分支追加成另一个 `or`，等于没改。两个条件必须在**同一个 `and_`** 里。

**推论（又踩过一次同款）**：任何“附加的限定条件”都要先问——**它是 `or` 的一项，还是 `and` 的一项？** 拿不准就写成 `and_(or_(可见性…), 额外限定)`，让结构自己说话。

> 给工作区可见性加“工作区还活着”时，先写成第四条 `or`（→ 只要库里存在任意一个活工作区，每一行都通过），再踩了一次 `EXISTS` 不 correlate。两次 pytest 全绿，都是 e2e + 真库探测抓住的。

还有一条：**软删的行必须先按行取、再判权限**。用 `readable_kb_ids` 过滤回收站列表会恒为空（软删行按定义不在集合里）；而且要用**写**判据，不是读判据，否则会出现“列表里有、点恢复 403”。

---

## 3. 检索侧

### 3.1 `skipped` 是字符串，不是布尔

调试面板里的 `legs[].skipped` 必须携带**可操作的原因**。一条什么都没有的 `skipped: false` 会让人查错方向。

**“查询侧没跑”与“写入侧没数据”在数据面上长得一模一样**——都是“这一腿返回 0 条”。所以能力检测只回答“本进程能不能跑”，不看集合里有没有点。

### 3.2 三条 DB 腿不能**共用**一个会话（真并发要各拿一个）

`dense` / `sparse` / `trgm` 都要做 DB 操作。如果把它们交给**同一个** `AsyncSession` 并发 `execute`，SQLAlchemy 会抛：

```
This session is provisioning a new connection; concurrent operations are not permitted
```

后果极其隐蔽：sparse / trgm 被记成 `skipped`，`hybrid` **静默降级成 dense 单腿**，接口照样 200、`/search/debug` 也不报错。

修法：**每条 DB 腿从会话工厂各拿一个会话**（各自的连接），四条腿真并发。⚠️ 不要退回"共用会话 + 加锁串行"——那能跑对，但把并发收益丢了。代价是连接数：一次 `hybrid` 最多占 请求会话 + 3 = 4 条连接，池要按 `并发检索数 × 4` 估。守卫：`tests/test_leg_concurrency.py`（真并发 + 独立会话）+ `tests/test_retrieval_e2e.py` 的冷会话用例。

### 3.3 RRF 的两个工程细节

- **key 必须带命名空间**（`text:` / `visual:`）：两个集合的点 ID 空间独立，直接拿点 ID 做 key 会让两者互相抵消；
- **排序要有确定性 tiebreaker**：RRF 分是浮点，并列时顺序会抖动，前端引用角标会在刷新后对调。

### 3.4 日期归一化：检索质量问题可能出在分词

`2025-02-14` 与“2025年2月14日”两边没有任何共同 token，而 BM25 无法表达“多词共现”。加一个归一化 token `d20250214` 后，该类查询 R@10 从 **0.20 → 1.00**。

### 3.5 trgm 只能用 `word_similarity`

`ILIKE '%<整句提问>%'` 与整段 `similarity()` 都会**静默漏召回**（各返回 0 条），因为两者都要求 query 与整段文本可比。把两者 `OR` 起来也漏——放宽的是同一个错误判据。代价是全表扫描，但“慢能被发现，漏召回不能”。

### 3.6 Embedding 模型必须一致

不同模型的向量空间不通用，混用等于在两个空间里算相似度——**排序基础整个坏了，而且不报错**。所以：

- 建索引时写 `index_models`；
- dense 腿查询前校验，不一致就 `skipped` 并说清“索引哪个、现在哪个、怎么修”；
- `index_models` **空表 = 未知 = 放行**（还没记录 ≠ 不一致）。

### 3.7 BM25 的统计量必须有统一减法入口

`terms` / `corpus_stats` 在入库时加、删除时减。**任何绕开统一删除入口的路径都会让它虚高** → idf 被压平 → 罕见词不再罕见。表现是质量下降、不是报错，所以对账里专门有一条：`chunks` 行数应等于语料统计里的文档数。

---

## 4. 摄取与坐标

### 4.1 扩展名在数据库行里

内容寻址 blob **没有扩展名**。从路径取后缀会让解析器与页图端点各炸一次。解析入口强制要求传 `file_name`。

### 4.2 画框乘回 point，不是像素

```python
shape.draw_rect(Rect(nx0 * pix.width, ny0 * pix.height, ...))        # ❌ 框整体下移约半行
shape.draw_rect(Rect(nx0 * page.rect.width, ny0 * page.rect.height, ...))  # ✅
```

### 4.3 形态按 `locations` 的形状分派，不按 `kind`

对外引用类型只有 `text` / `page` / `image`，而块形态有四种，所以表格块会以 `kind="text"` + 带 `row`/`col` 的框出现。判据：有 row/col → 网格；`kind==="page"` → 整页；其余单框。反过来“从画了几个框反推形态”是错的。

### 4.4 坐标与被渲染页面必须同源

Office 没有原生坐标，走“转 PDF → 在转换版上算坐标”。画框用哪个文件由调用方决定，底图渲染函数不许自己去找原件。定位覆盖率低于 60% 时不给框，而不是给一个错位的框。

### 4.5 表格坐标取自 `rows[i].cells`，不是扁平的 `cells`

`len(tbl.cells) != row_count * col_count`（合并单元格会让长度对不上）。文本用 `extract()`，坐标用 `rows[i].cells`，按行号对齐。

### 4.6 页图端点要 Bearer ⇒ 前端不能用 `<img src>`

`<img>` 带不了自定义请求头，直接写会**静默 401**，只在控制台看得到。要用 `fetch` 拿 blob 再 `createObjectURL`，并在卸载时释放。

---

## 5. 任务与状态

### 5.1 进度 100% 只归 `finish_job`

handler 结尾的 `progress(1.0)` 与 `finish_job` 是两次提交，中间有一个几毫秒的窗口，会读到 `running + progress=1.0`。偶发红比稳定红更贵。所以非终态进度封顶 **0.999**，且必须单调不减。

### 5.2 文档终态要“两件事都算”

只看任务状态 → 部分失败被报成 `ready`；只看索引点数 → 全失败时永远 `pending`。判定要同时看“任务是否全部终态”和“索引里实际有多少点”，并且结算在 `finish_job` 事务之外、且不抛异常。

### 5.3 挡终态的 kind 白名单

只有 `ingest` 与 `embed_visual` 挡终态。`thumbnail` / `pdf_preview` 不挡——否则“缩略图还没生成”会把一份页级点已经齐了的文档永远按在 `pending`。

### 5.4 “没有待办任务却还在中间态”是不可能的状态

它要么是终态判定漏了，要么是任务被删了。worker 启动补扫 + 巡检兜底。

### 5.5 kind 变更要登记三处

`common/jobs.py` 的 `ALL_KINDS`、`worker/handlers/__init__.py` 的 `HANDLERS`、`models.py` 的 CHECK 约束，外加一条手写迁移。第三处最容易漏。

---

## 6. 数据库与代码

### 6.1 时间列一律带时区

裸 `mapped_column(default=utcnow)` 会被 autogenerate 生成 `TIMESTAMP WITHOUT TIME ZONE`。统一用 `TIMESTAMP`（`DateTime(timezone=True)`）。

### 6.2 同步 / 异步 session 工具必须分成两个名字

```python
def register_blob(session, ...): ...            # ✅ worker（同步 session）
async def register_blob_async(session, ...): ... # ✅ API（AsyncSession）
# ❌ 只写一个，然后两边都调
```

在 `AsyncSession` 上调同步的 `session.execute()` **不报错**，只是返回一个从未被 await 的 coroutine。守卫 `tests/test_unawaited_coroutines.py` 用 AST 扫全 `services/`。

### 6.3 SSE 生成器里不许碰 DB session

FastAPI 在 `yield` 依赖上把退出码放在“发送响应之前”执行，所以 `DbSession` 依赖在响应体开始流式之前就已经 commit + 关闭了。铁律：

1. 检索 + 取生成器**全部在开流之前**用请求级 session 完成；
2. SSE 生成器里**只允许**做 LLM 的 HTTP 往返；
3. 问答记录的落库在流结束后**新开** `session_scope()`，两条一起写。

违反第 2 条的话，`session.execute()` 的 coroutine 永远不会被 await，消息静默丢失——与“忘了 await 的 enqueue”完全同一种“不报错、不告警、功能就是不工作”。

### 6.4 `ON CONFLICT` / 覆盖式更新

上传同名文件时旧文档进回收站、新文档成为当前版本。旧向量由周期清理任务清掉，**不留 30 天**。

---

## 7. 文档与仓库纪律

- 入口文档（README / MEMORY / HANDOFF）里的路径、命令、端口、基线数字必须对得上，守卫 `tests/test_docs_pointers.py`；
- `docs/` 里不放脚本，只放文档；
- 决策写决策记录（带“为什么”与实测数字），坑写踩坑记录，基线数字同步；
- **代码与文档冲突时以代码为准**，并把文档改过来；
- 已跟踪文件里不许有真实信息，守卫 `tests/test_no_real_data.py`；
- 备份与截图都别拿真数据。

---

## 8. 本地环境类坑

| 症状 | 真正原因 | 处理 |
|---|---|---|
| 改了代码没生效 | 旧 worker 子进程 | `dev.sh restart` |
| 请求走错出口 / 代理报错 | 代理环境变量 | `--noproxy '*'` 或检查代理 |
| 磁盘满、无明确报错 | 临时目录里 Chrome profile 泄漏 | 清理临时目录 |
| `got Future … attached to a different loop` | 事件循环混用 | 检查 async 测试与共享 loop |
| 模型加载 ~23 秒一次 | 没设离线环境变量 | 设 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` |
| 检索质量悄悄变差 | BM25 统计量漂移 / 某条腿 skipped | 对账分腿看；跑检索回放 |

### 8.1 async 测试的配置节名

`pytest.ini` 的节名写错会让整份配置**都不生效**。写 async 测试时按同步函数 + 模块级共享 loop 的约定办。

---

## 9. 判据与测试纪律

- **判据要指向性质，不是中间状态**。判了某个中间状态或某行实现字面量，改实现就会误红；
- **AST 类断言必须做变异验证**：故意改坏一处，确认测试真的会红；并单独判定 pytest `exit 5`（没收集到测试，不是通过）；
- **抽了 helper 必须接线**，不接线的 helper 等于没有；
- **“仍然全绿”要分清**是判据弱、变异无效，还是 `-k` 指错。

---

## 10. 相关文档

- 架构约束的来龙去脉：[架构总览](./architecture.md) · [项目深度拆解](./deep-dive.md)
- 权限踩坑：[多租户与权限](./tenancy.md)
- 检索与坐标细节：[检索与融合](./retrieval.md) · [文档处理与切分](./ingestion.md)
