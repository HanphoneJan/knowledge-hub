# 多租户与权限

> 本文描述**已经实现并实际生效**的权限体系。结论以 `services/common/auth.py`、各路由与前端守卫为准。

---

## 1. 权限体系总览

平台没有做完整的 RBAC，只有四层足够用的机制：

1. **身份**：用户 + 每用户一把或多把 API Key；
2. **组织**：工作区（Workspace）+ 成员（owner / member）；
3. **资源可见性**：知识库三档（`private` / `workspace` / `global`）；
4. **全局角色**：`user` / `superadmin`，用于运维与用户管理。

权限判定的入口只有一个：`services/common/auth.py`。

---

## 2. API Key：唯一的租户来源

### 2.1 格式与存储

```python
KEY_PREFIX = "sk-pkh"
plaintext = f"{KEY_PREFIX}_{secrets.token_urlsafe(32)}"
key_hash  = sha256(plaintext)          # 完整 64 位 hex，不截断
key_prefix = plaintext[:KEY_PREFIX_LEN + 6]   # 仅用于 UI 展示
```

- 明文**只在创建时返回一次**，库里只存哈希；
- 用 SHA-256 而不是 bcrypt/argon2：key 本身是 256 位随机数，没有低熵可爆破，且每次请求都要算，必须快；用户密码才用 argon2；
- **必须存完整哈希**：早期只存 sha256 前 12 个 hex（48 bit），在线爆破平均 2^48 次哈希 ≈ 一块 GPU 8 小时。改成完整长度后，代价是旧 Key 全部失效一次（前端在 401 时会重新登录换 Key）。

### 2.2 请求体不接受租户字段

```python
# ❌ 攻击者可以传别人的 user_id
{"user_id": 42, "query": "..."}

# ✅ 租户范围从 API Key 推导
ctx = await load_user_context(session, api_key)
```

一切范围都来自 `UserContext`：

| 字段 | 含义 |
|---|---|
| `user_id` | 当前用户 |
| `workspace_ids` | 我加入的工作区 |
| `readable_kb_ids` | 我可读的 KB 全集（三档折叠后的结果） |
| `is_superadmin` | 全局超管（仍从 API Key 推导） |
| `scopes` | 粗粒度门禁（当前只做 `kb:read` / `chat:read`，为未来 RBAC 留位） |

`workspace_id` 可以作为请求参数，但必须校验成员资格。`kb_ids` 只用于**求交**，不能扩大范围。

---

## 3. 三档可见性

### 3.1 三个独立分支，不能写进一个 `IN`

```python
# ❌ 两个独立漏洞
or_(visibility.in_(("workspace", "global")), created_by == me)
#   1. 所有 workspace 库对任何用户可见（少了“我是该工作区成员”）
#   2. 同工作区成员能看到别人的 private 库（private 混在同一个 IN 里）

# ✅ 三个档位各有各的限定条件
global    → 所有用户可见
workspace → visibility == 'workspace' AND workspace_id IN (我的工作区)
private   → created_by == 我
```

**为什么 `IN` 天然是错的**：`IN` 表达“任一条件成立即可”，而可见性要表达“不同档位有各自不同的限定条件”——语义不匹配。

### 3.2 修复时最容易犯的二次错误

保留裸的 `workspace` 分支，把带成员资格限定的分支**追加**成另一条 `or`：

```python
# ❌ 限定条件完全无效，等于没改
conds = [vis == "global", vis == "workspace", created_by == 我]
conds.append((vis == "workspace") & ws_id.in_(ws))    # 白加
```

两个条件必须写在**同一个 `and_`** 里：

```python
# ✅
and_(visibility == "workspace", workspace_id.in_(ws_ids))
```

推论：任何“附加的限定条件”都要先问一句——**它是 `or` 的一项，还是 `and` 的一项？** 拿不准就写成 `and_(or_(可见性...), 额外限定)`，让结构自己说话。

### 3.3 工作区软删之后

早期删工作区只标记 `workspaces.deleted_at`，不级联 KB/文档，结果是“删掉工作区”对保密毫无作用：`GET /kb` 仍列出它的库、搜索仍能命中正文。

修法不是级联（那会把“恢复一行”变成“恢复一串”），而是**在可见性判定里要求它的工作区还活着**：

```python
exists(select(1).where(Workspace.id == KB.workspace_id,
                       Workspace.deleted_at.is_(None)))
```

一处条件，四条路径同时被覆盖。⚠️ 这个 `EXISTS` 必须 **correlate** 到当前 KB 行，否则会退化成“只要库里存在任意一个活工作区，每一行都通过”。

---

## 4. 过滤入口：只有一个真相源

| 函数 | 服务对象 | 用在哪 |
|---|---|---|
| `build_qdrant_filter(ctx, kb_id, kb_ids=)` | Qdrant | dense / sparse / visual 三条腿 |
| `build_kb_scope_sql(ctx, kb_id, column, kb_ids=)` | PostgreSQL | trgm 子串腿 |
| `visible_kb_clause(ctx)` | PostgreSQL | KB 列表（读） |
| `writable_kb_clause(ctx)` | PostgreSQL | KB 回收站 / 恢复（写） |

前两者**同源**——都只读 `ctx.readable_kb_ids`，不各自重算三档可见性。历史上唯一一次真实越权，正是“两处复刻可见性逻辑”造成的。

**为什么不是一个函数？** 因为有一条腿跑在 PostgreSQL 里，拿不到 Qdrant 的过滤字典。新加任何检索存储时，往这张表里加一行，不要在调用方拼条件。

### 4.1 自动化守卫

`tests/test_retrieval_filters.py` 用 AST 扫描全仓库，断言：

- 除白名单外，任何文件不得出现过滤字面量；
- 每条查 Qdrant 的腿都必须传来自统一入口的 `filter`；
- 检索层不得出现可见性**判定**代码；
- 运行时只有一棵依赖树；
- 检索实现只在 `services/retrieval/`。

`tests/test_permissions.py` 还会用 `literal_binds` 编译 SQL，正则找“裸的 workspace 分支”，并断言不出现 `visibility IN`。

### 4.2 跨多个存储：PG / Qdrant / blob 分别靠什么隔离

系统用了三个存储，但**授权只判定一次**：`load_user_context` 从 API Key 推导出
`ctx.readable_kb_ids`；三个存储各自只做"按这个范围过滤 / 校验"，不重算可见性。

| 存储 | 里面有什么 | 隔离手段 | 失败时 |
|---|---|---|---|
| **PostgreSQL**（真相源）| KB / 文档 / chunk / 任务 / 用户 | `build_kb_scope_sql` · `visible_kb_clause` · `writable_kb_clause`；读资源前 `assert_kb_access` | 恒假条件 / `KB_NOT_FOUND` |
| **Qdrant**（派生索引）| dense / sparse / visual 三个 collection | 每条查询都带 `build_qdrant_filter(...)`；payload 里也有 `kb_id` / `workspace_id` / `visibility` | `{"kb_id": -1}` 恒空 filter |
| **Blob**（内容寻址）| 原件 / 预览 / 缩略图，按 `sha256` 去重 | **鉴权在元数据层**：先 `load_document` → `assert_kb_access`，再读 `doc.blob_sha` | 404 / 403，不按路径放行 |

两条容易被忽略的边界：

- **blob 去重 ≠ 权限共享**：同一份文件被两个租户上传只存一份，但 `sha256` **不是
  capability** —— 知道哈希也读不到，因为读端点先校验文档所属 KB。**文件路径从来不是权限**。
- **Qdrant 允许短暂落后**：检索在融合后还要做一次 `_drop_deleted`（批量查 PG）。
  **绝不允许"PG 已删、Qdrant 还能搜到"** —— 那才是真正的数据泄漏。

> ⚠️ **有意的统计量泄漏**：BM25 的 `terms` / `corpus_stats` 是**全局**的（三个 KB 共用
> 一个 sparse collection，下标空间必须一致）。一个罕见词在别的租户语料里出现过，会拉低
> 这边的 idf —— **只泄漏统计量，不泄漏内容**。这是明确记录下来的取舍，不是遗漏。

---

## 5. fail-closed：无权限 ≡ 不存在

```python
# ❌ 攻击者能通过错误码差异探测资源是否存在
if kb.visibility == "global": raise Forbidden()
raise NotFound()

# ✅ 内部日志可记原因，对外一律同一个错误码
raise ResourceNotFound()   # KB_NOT_FOUND / 403
```

同样的纪律适用于：

- 文档：`DOC_NOT_FOUND`；
- 工作区：无权限与不存在同码；
- MCP 工具：库不存在 / 无权限都用 `result.isError = true` + `KB_NOT_FOUND`，不暴露存在性。

跨库搜索的边界：

- `kb_ids=[]` 返回 **400**（空数组是请求错误）；
- `kb_ids` 求交后为空时返回**空结果**而不是报错——报错会泄漏“这些 kb_id 里至少有一个存在”。

---

## 6. 工作区、成员与邀请码

- 工作区有 `owner` / `member` 两档；
- `POST /workspaces/{id}/members` 只能加**已注册**邮箱（没注册返回 400）；
- 不接邮件服务，所以邀请必须**自服务化**：邀请码。

| 端点 | 谁能调 | 说明 |
|---|---|---|
| `POST /workspaces/{id}/invites` | owner/admin | 生成，明文只返回这一次 |
| `GET /workspaces/{id}/invites` | owner/admin | 列表只有前缀 |
| `DELETE /workspaces/{id}/invites/{invite_id}` | owner/admin | 撤销（幂等） |
| `POST /invites/redeem` | 登录用户 | 兑换 → 成为 member |

四条设计约束：

1. 库里只存 HMAC-SHA256 哈希 + 前 4 位（每码随机盐，与 API Key 同一纪律）；
2. 口令 8 位、去掉 `0O1IL`，规范化接受空格/连字符/小写，但**不会**把 `0` 猜成 `O`；
3. 默认 7 天 / 1 次（`max_uses` 上限 50）；
4. **兑换失败统一一句话**（不存在/过期/用完/撤销都是同一句），区分开等于给攻击者一个探测信号。

⚠️ 兑换的校验顺序是**先验哈希 → 再看“是不是已经是成员” → 最后验状态**，否则重复兑换会得到“邀请码无效”。已经是成员不算失败：返回 200 + `already_member: true`。`POST` 请求体**没有 `role`**（只能给 member）。`used <= max_uses` 是数据库 CHECK，撞上时返回 400。

---

## 7. 超级管理员

- `users.role`：`user` / `superadmin`，**全局角色**，与工作区的 owner/member 不是一回事；
- 超管仍是**从用户行经 API Key 推导**，请求体不接受角色字段；
- 越过三档可见性：把 `UserContext.readable_kb_ids` 展开成全集，而不是在过滤器里加分支；
- `GET/POST/PATCH /admin/users` 仅超管；
- 防自锁：**不能降级/停用最后一个超管**，**不能停用自己**；
- 任何用户可用 `PATCH /auth/me` 改自己的显示名 / 昵称 / 邮箱 / 密码；改邮箱或密码**必须**带 `current_password`。

> 普通用户的三档可见性 SQL 一字未改，守卫钉住。超管只是把“范围”展开，不是绕开过滤逻辑。

---

## 8. 审计

审计表记录三元组：**API Key → 用户 → 知识库**，以及 query 摘要、结果数、耗时。

| `action` | 端点 |
|---|---|
| `search` | `POST /kb/{id}/search` · `POST /search` · `POST /kb/{id}/search/debug` · `GET /kb/{id}/ask/preview` |
| `ask` | `POST /kb/{id}/ask`（SSE） |
| `upload` | `POST /kb/{id}/documents` · `import/chat` · `import/dir` |
| `mcp_call` | `POST /mcp`（每次 `tools/call`） |
| `a2a_call` | `POST /a2a/tasks` |

要点：

- `ask_preview` 记 `search` 不记 `ask`（它没有生成任何东西）；
- `ask` 的 `detail` 里另记 `retrieval_query`（真正送去检索的，可能被指代消解改写）、`query_expanded`、`history_turns`；
- **不记审计的读端点**（刻意）：`/documents/{id}/chunks`、`/pages/{n}`、`/preview`、`/thumbs/{n}`、`/pii`；
- 新增检索端点时**必须**接上审计，`tests/test_audit_coverage.py` 是接线守卫；
- `GET /audit` 只能查自己有权管理的工作区（审计里有 query 摘要，那是用户的数据），`workspace_id` 必填；`action` 先校验再查（拼错值静默返回空列表 = 骗人）。

---

## 9. 权限之外的隐私边界

权限解决“谁能看”，隐私解决“数据去哪”。两者不要混：

| 机制 | 解决什么 | 默认 |
|---|---|---|
| 三档可见性 | 用户之间的隔离 | `private` |
| `KB_NOT_FOUND` | 不泄露资源存在性 | 恒开 |
| PII 掩码 | 索引文本里的敏感信息 | 按库关闭 |
| 抽取式生成 | 原文不出本机 | 恒开 |
| 外部 LLM 开关 | 是否允许原文外传 | 关 |
| 视觉服务无鉴权但只绑 127.0.0.1 | 重模型不暴露到网络 | 本机 |

> 视觉服务**没有鉴权**，它只绑 `127.0.0.1`。别把它指到 `0.0.0.0`；用 `deploy/visual.sh` 单独起停。

---

## 10. 常见错误与排查

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 列表里有、点恢复 403 | 回收站用了读判据而不是写判据 | 用 `writable_kb_clause`；`tests/test_container_trash.py` 用真值表逐格比对 |
| 删了工作区还能搜到 | 可见性没要求工作区还活着 | 加 `EXISTS`，并确认它 correlate 到当前 KB 行 |
| 同工作区看到别人的 private 库 | 三档写进一个 `IN` | 改三个独立分支 |
| 搜索返回了别的库 | `kb_ids` 被当成“全集”或过滤未传 | 子集只做求交；检查腿是否传了统一 filter |
| 401 后前端被登出 | Key 哈希改成完整长度，旧 Key 失效 | 前端 401 时重新登录换 Key |
| 邀请码重复兑换提示无效 | 校验顺序错了 | 先验哈希 → 再看是否已是成员 → 最后验状态 |

---

## 11. 相关文档

- 检索如何消费这些过滤条件：[检索与融合](./retrieval.md)
- 审计字段与端点：[API 契约](./DESIGN-API.md)
- MCP/A2A 的权限裁剪：[MCP 与 A2A](./mcp-a2a.md)
