# 多租户与权限

Knowledge Hub 没有做完整的 RBAC，只有四层足够用的机制：用户与 API Key、工作区与成员、知识库三档可见性、全局的 `user` / `superadmin` 角色。所有判定收在 `services/common/auth.py`，别在路由里另写一份。

## API Key 怎么变成租户范围？

请求只带 `Authorization: Bearer <key>`。服务端用完整 sha256 找到 `api_keys` 行，再连到 `users`，推导出一个冻结的 `UserContext`：

| 字段 | 含义 |
|---|---|
| `user_id` | 当前用户 |
| `workspace_ids` | 我加入的工作区 |
| `readable_kb_ids` | 三档可见性折叠后的可读库全集 |
| `is_superadmin` | 全局超管，仍从用户行推导 |
| `scopes` | 粗粒度门禁，当前只做 `kb:read` / `chat:read` |

请求体里**不接受** `user_id`、`workspace_id` 之外的租户参数，也不接受角色。`workspace_id` 出现时必须校验成员资格；`kb_ids` 只用于求交，不能扩大范围。

Key 的格式是 `sk-pkh_` 加 32 字节随机串。库里只存完整 sha256 的 64 位 hex，明文只在创建时返回一次。用 SHA-256 而不是 bcrypt/argon2，是因为 Key 本身是 256 位随机数，没有低熵可爆破，而且每次请求都要算；用户密码才用 argon2id。

早期只存 sha256 的前 12 个 hex（48 bit），这是在线爆破的漏洞：攻击者猜的是哈希前缀，不是原文，平均 2^48 次哈希用一块 GPU 约 8 小时。改成完整长度后，旧 Key 全部失效一次，表现是“被登出一次”，前端在 401 时会重新登录换 Key。

`POST /auth/login` 每次都会**新铸一把** `web-login` key。因为库里只有哈希，返回不了任何旧 key 的明文，所以登录的语义只能是“校验密码 → 发新 key”。代价是 `api_keys` 表会增长，用户可以吊销，运维脚本可以清理长期未用的。

## 三档可见性为什么不能写成一个 IN？

三档各有各的限定条件：

```text
global     → 所有用户可见
workspace  → visibility == 'workspace' AND workspace_id IN (我的工作区)
private    → created_by == 我
```

写成 `visibility.in_(("workspace","global")) OR created_by == 我` 会同时制造两个漏洞：所有 workspace 库对任何用户可见（少了“我是成员”），以及同工作区成员能看到别人的 private 库（private 被混进同一个 IN）。

修的时候还有第二种错法：保留裸的 `workspace` 分支，再把带成员限定的分支**追加**成另一条 `or`。这样限定条件完全无效，等于没改。两个条件必须写在同一个 `and_` 里：

```python
and_(KnowledgeBase.visibility == "workspace",
     KnowledgeBase.workspace_id.in_(ctx.workspace_ids))
```

推论很通用：任何“附加的限定条件”都要先问一句——它是 `or` 的一项，还是 `and` 的一项？这个项目唯一一次真实越权，就是两处复刻可见性逻辑造成的。

工作区被软删后，它的库也必须不可见。早期只标记 `workspaces.deleted_at`，结果是删掉工作区对保密毫无作用：列表仍列出、搜索仍能命中正文。修法不是级联删除（那会把“恢复一行”变成“恢复一串”），而是在可见性里要求工作区还活着：

```python
exists(select(literal(1)).where(Workspace.id == KnowledgeBase.workspace_id,
                                Workspace.deleted_at.is_(None)).correlate(KnowledgeBase))
```

这个 `EXISTS` 必须 **correlate** 到当前 KB 行，否则会退化成“库里只要存在任意一个活工作区，每一行都通过”。而且它必须用 `and_` 包住整个 `or_`，不能当成第四条 `or` 分支。

## 四个过滤构造器分别用在哪？

| 函数 | 服务对象 | 用在哪 |
|---|---|---|
| `build_qdrant_filter` | Qdrant | dense / sparse / visual 三条腿 |
| `build_kb_scope_sql` | PostgreSQL | trgm 子串腿 |
| `visible_kb_clause` | PostgreSQL | KB 列表、可读范围推导 |
| `writable_kb_clause` | PostgreSQL | KB 回收站、恢复 |

前两者同源：都只读 `ctx.readable_kb_ids`，不各自重算三档可见性。分成四个函数是因为服务对象不同：trgm 腿跑在 PostgreSQL 里，拿不到 Qdrant 的过滤字典；列表与回收站需要 SQL 条件。新增任何检索存储时，往这张表里加一行，不要在调用方拼条件。

读写判定差一格，别混用：

| | global | workspace | private |
|---|---|---|---|
| 读 | 所有人 | 成员**或创建者** | 创建者 |
| 写 | 所有人 | **只有成员** | 创建者 |

差在“workspace 可见性但创建者已不是成员”这一格：那种库读得到、改不了。回收站列表必须用写判据，否则会出现“列表里有、点恢复 403”。

跨三个存储时，授权只判定一次：

| 存储 | 隔离手段 | 失败时 |
|---|---|---|
| PostgreSQL | 三个 SQL 构造器 + `assert_kb_access` / `assert_kb_write` | 恒假条件 / `KB_NOT_FOUND` |
| Qdrant | 每条查询带 `build_qdrant_filter`；payload 里也有 `kb_id` | `{"kb_id": -1}` 恒空 filter |
| Blob | 鉴权在元数据层：先取文档、校验 KB，再读 `blob_sha` | 403 / 404，不按路径放行 |

blob 去重不等于权限共享。同一份文件被两个租户上传只存一份，但 sha256 不是 capability——知道哈希也读不到，因为读端点先校验文档所属 KB。文件路径从来不是权限。

BM25 的 `terms` 与 `corpus_stats` 是全局量，因为三个库共用一个稀疏集合，下标空间必须一致。一个罕见词在别的库出现过会拉低这边的 idf，这只泄漏统计量，不泄漏内容，是明确记录下来的取舍。

## fail-closed 具体怎么做？

无权限与不存在返回同一个错误码：`KB_NOT_FOUND` / 403。内部日志可以记原因，对外不能区分。文档用 `DOC_NOT_FOUND`，工作区也是同码。MCP 工具在库不存在或无权限时返回 `isError: true` 加 `KB_NOT_FOUND`，同样不暴露存在性。

跨库搜索有两个边界：`kb_ids=[]` 返回 400，因为空数组是请求错误；`kb_ids` 求交后为空则返回空结果而不是报错——报错会泄漏“这些 kb_id 里至少有一个存在”。

当没有任何可读库时，Qdrant 过滤器与 SQL 条件都返回恒假，**绝不退化成不过滤**。`{"kb_id": -1}` 看起来像限制，实际是“什么都不要”；忘了这个分支就是全库可读。

## 工作区、邀请码与超管

工作区只有 `owner` 与 `member` 两档。`POST /workspaces/{id}/members` 只能加已注册邮箱；没注册的人走邀请码，因为项目不接邮件服务。

邀请码有四条约束：

1. 库里只存 HMAC-SHA256 哈希与前 4 位前缀，明文只返回一次；
2. 口令 8 位，去掉 `0`、`O`、`1`、`I`、`L`，规范化接受空格、连字符与小写，但**不会**把 `0` 猜成 `O`——那会让抄错的码通过；
3. 默认 7 天、1 次，`max_uses` 上限 50，`used <= max_uses` 由数据库 CHECK 兜住并发；
4. 兑换失败统一一句话，不存在、过期、用完、撤销都一样，区分开等于给攻击者探测信号。

兑换的校验顺序是先验哈希、再看是不是已经是成员、最后验状态。已经是成员不算失败，返回 200 与 `already_member: true`。请求体没有 `role`，邀请码只能授予 member。

超管是 `users.role = superadmin`，与工作区的 owner 不是一回事。它越过三档可见性，做法是把 `readable_kb_ids` 展开成全集，而不是在过滤器里加分支；普通用户的 SQL 一字未改。`GET/POST/PATCH /admin/users` 仅超管可用，并且有两条防自锁：不能降级或停用最后一个超管，不能停用自己。

任何用户都可以用 `PATCH /auth/me` 改自己的显示名、昵称、邮箱与密码；改邮箱或密码必须带 `current_password`，否则一次借用的设备就能把账号锁走。

注册与登录都有失败限流。注册按 IP 分桶，公网每小时 20 次，本机 500 次；登录只数失败，IP 桶 5 分钟 10 次，账号桶 15 分钟 20 次。账号桶才是保护某个具体用户密码的那一个，换 IP 无效。`allow_register=false` 时本机仍然放行，否则三个 eval 与端到端测试都会红；公网路径永远不是 `local`。

## 审计记录什么？

审计记录三元组：API Key → 用户 → 知识库，外加 query 摘要、结果数、耗时。写入跟业务操作用同一个 session，业务回滚时审计也回滚——审计记录的是“发生过的事”，不该记录没提交的操作。

| action | 端点 |
|---|---|
| `search` | 单库/跨库检索、`search/debug`、`ask/preview` |
| `ask` | `/kb/{id}/ask`、`/ask`（SSE） |
| `upload` | 上传、聊天导入、目录导入 |
| `mcp_call` | `POST /mcp` 的每次 `tools/call` |
| `a2a_call` | `POST /a2a/tasks` |

`ask_preview` 记 `search` 不记 `ask`，因为它没有生成任何东西。`ask` 的 detail 里另记 `retrieval_query` 与 `query_expanded`：用户问了什么与真正送去检索的可能不同（指代扩展），只记一个的话，“为什么这次召回了奇怪的东西”就没法查。

刻意不记审计的读端点：chunk 列表、页图、预览、缩略图、PII 报告。新增检索端点时必须接上审计，`tests/test_audit_coverage.py` 是接线守卫。`GET /audit` 只能查自己有权管理的工作区，且 `workspace_id` 必填。

## 权限之外的隐私边界

权限解决“谁能看”，隐私解决“数据去哪”，两者不要混。

| 机制 | 解决什么 | 默认 |
|---|---|---|
| 三档可见性 | 用户之间的隔离 | `private` |
| `KB_NOT_FOUND` | 不泄露资源存在性 | 恒开 |
| PII 掩码 | 索引文本里的敏感信息 | 按库关闭 |
| 抽取式生成 | 原文不出本机 | 恒开 |
| 外部 LLM 开关 | 是否允许原文外传 | 关 |
| 视觉服务无鉴权但只绑 `127.0.0.1` | 重模型不暴露到网络 | 本机 |

视觉服务没有鉴权，它只绑回环地址。别把它指到 `0.0.0.0`；用 `deploy/visual.sh` 单独起停。

## 常见错误怎么排查？

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 回收站里有、点恢复 403 | 列表用了读判据 | 改用 `writable_kb_clause`；真值表在 `tests/test_container_trash.py` |
| 删了工作区还能搜到 | 可见性没要求工作区活着 | 加 `EXISTS`，并确认 correlate 到当前 KB 行 |
| 同工作区看到别人的 private 库 | 三档写进一个 `IN` | 改三个独立分支 |
| 搜索返回了没选的库 | `kb_ids` 被当成全集或过滤未传 | 子集只做求交；检查腿是否传了统一 filter |
| 401 后前端被登出 | Key 哈希改成完整长度 | 前端 401 时重新登录换 Key |
| 邀请码重复兑换提示无效 | 校验顺序错了 | 先验哈希 → 再看是否已是成员 → 最后验状态 |

## 相关文档

- 过滤条件如何被四条腿消费：[检索与问答](retrieval.md)
- 错误码、分页与 SSE 字段：[API 契约](DESIGN-API.md)
- MCP 与 A2A 的权限裁剪：[对外接口](api-mcp-a2a.md)
