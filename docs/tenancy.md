# 多租户与权限

这个项目的权限判定只有一条主线：**API Key → 用户 → 可读的知识库集合**。请求体里不接受 `user_id`、`workspace_id` 之外的身份字段，服务端也不相信客户端说的"我能看哪个库"。所有过滤都从同一个 `UserContext.readable_kb_ids` 出发。

## API Key 推出上下文

每个请求带 `Authorization: Bearer sk-pkh_...`。服务端：

1. 用完整 SHA-256 查 `api_keys.key_hash`，要求未吊销、未过期、用户仍活跃。
2. 从用户行读全局角色（`user` / `superadmin`）。
3. 查我加入的工作区。
4. 用可见性条件查出所有可读 KB 的 id，折叠成 `readable_kb_ids`。
5. 把 `readable_kb_ids`、`workspace_ids`、`is_superadmin` 放进一个 frozen dataclass。

失败一律 `INVALID_TOKEN`，不区分"不存在/已吊销/已过期"，同样是阻断存在性探测。

API Key 只存哈希，不存明文；哈希是完整 64 位 hex，不是截断过的前缀。截断会让在线爆破的成本从 2^256 降到 2^48——猜的是哈希前缀，不是原始 Key 的全部位。

## 三档可见性

![读写两条路径与三档可见性](diagrams/tenancy-decision.png)

| visibility | 谁能读 | 谁能写 |
|---|---|---|
| `private` | 创建者 | 创建者 |
| `workspace` | 该工作区成员 **或创建者** | **只有该工作区成员** |
| `global` | 所有用户 | 所有用户 |

读写差一格：一个 `workspace` 库的创建者如果已经退出工作区，他**读得到**（读判定里 `created_by == 我` 是一条独立的 `or` 分支），但**改不了**（写判定按 visibility 分派，不看 `created_by`）。混用读判据去筛回收站，会出现"列表里有、点恢复 403"。

`global` 可读可写是 D43 的有意取舍：5~20 人的信任团队里够用；如果对外开放，这是最先被攻击的点。所以判定集中在 `auth.py`，将来接 RBAC 只改那两处。

## 两个过滤入口，不是四个

| 函数 | 服务对象 | 用在哪 |
|---|---|---|
| `build_qdrant_filter` | Qdrant | dense / sparse / visual 三腿 |
| `build_kb_scope_sql` | Postgres | trgm 子串腿 |
| `visible_kb_clause` | Postgres | KB 列表 |
| `writable_kb_clause` | Postgres | KB 回收站 |

前两个只读 `ctx.readable_kb_ids`，不各自重算三档可见性。后两个是 SQL 条件，读侧与写侧各一份，语义与 `can_read_kb` / `can_write_kb` 的行版本逐档一致。

这不是"多写几个函数"的问题。项目唯一一次真实越权，就是同一段可见性逻辑被复制到两处，然后其中一处开始漂。现在再加第三个使用点时，必须往这张表里加一行，而不是在调用方拼条件。

两个容易写错的形状：

```python
# ❌ workspace 和 global 合并成一个 IN
visibility.in_(("workspace", "global"))

# ❌ 把成员资格限定追加成另一条 or
or_(visibility == "workspace", workspace_id.in_(my_ws))
```

第一种把所有工作区的库泄露给任何用户；第二种让"限定条件"完全无效，只要可见性是 workspace 就可见。正确的写法是三个分支分别判定，workspace 的两个条件写在同一个 `AND` 里。

## fail-closed

拿不到授权范围时，过滤条件必须变成**恒假**，绝不能退化成"不过滤"：

```python
# Qdrant：kb_id == -1 永远不存在
{"must": [{"key": "kb_id", "match": {"value": -1}}]}

# SQL：id == -1
col == NO_SUCH_KB
```

跨库请求里的 `kb_ids` 只是"意图"：实际范围是 `readable_kb_ids ∩ kb_ids`。交集只会更小，所以带一个无权限的 kb_id 进来不会越权，只会少结果。传空数组返回 400；交集为空返回空结果，不报错——报错会泄漏"这些 id 里至少有一个存在"。

## 无权限 ≡ 不存在

资源级失败统一用 `ResourceNotFound` → `KB_NOT_FOUND` / 403。列表接口只返回我有权限的行，不返回"存在但无权"的占位。文档级用 `DOC_NOT_FOUND` / 404，但同样不区分"不存在"与"没权限"。

MCP 的 `tools/list` 与 A2A 的 Agent Card 也遵守这条：只列出调用者有权限的库；一个库都没有时，MCP 不暴露 `search_kb` / `ask_kb` / `get_document`，A2A 的 `readableKbCount` 为 0。不能"先返回全部工具、调用时再拒绝"——那等于让调用方探测到不该存在的 KB。

## 工作区、邀请码与超管

- 工作区只有 owner 能改设置、增删成员、删除；member 只能读。删工作区是软删除，恢复也要求 owner。
- 邀请码只存 HMAC-SHA256 与 4 位前缀，不存明文；过期、用完、被撤销三种失效对兑换者给同一句话，区分开等于给攻击者一个"这个码存在但失效了"的探测信号。邀请码只能授予 `member`，不能授予 `owner`。
- 超管 `users.role=superadmin` 越过三档可见性：所有工作区、所有未删的库都进 `readable_kb_ids`，写判定直接为真。它仍然是从用户行推导的，请求体不接受这个字段。
- 操作级拒绝用 `FORBIDDEN` / 403，例如非超管调 `/admin/users`。它与资源级 `KB_NOT_FOUND` 不同：这里没有"资源存不存在"可探测。

## 工作区软删的连锁

删工作区只标记 `workspaces.deleted_at`，不级联软删 KB/文档。为了让"删掉"真的生效，可见性判定里多一个条件：工作区必须未软删。这个 `EXISTS` 必须与三档条件放在同一个 `AND` 里，否则只要库里存在任意一个未软删的工作区，所有行都会通过。

连带结果是顺序：工作区还软删着的时候，它的库既搜不到、也恢复不了；`GET /kb/trash` 与 `POST /kb/{id}/restore` 都要求"先恢复工作区"。这与文档说明一致，不是 bug。

## 守卫

- `tests/test_permissions.py`：三档必须分别判定；workspace 分支必须带成员资格；private 不能被同工作区成员读到。
- `tests/test_container_trash.py`：`can_write_kb` 的行版本与 `writable_kb_clause` 的 SQL 版在真实 PG 上逐格比对，回收站列表与恢复的判据必须一致。
- `tests/test_qdrant_filter_scope.py`：Qdrant 过滤只允许 `kb_id`；`services/` 下任何过滤字典都不得用 `visibility` / `workspace_id` / `source_kind` / `owner_id` 做 key。payload 里的 `visibility` 是摄取时写死的，改库可见性不回写；一旦有人按它过滤，陈旧值会立刻造成越权。

## 不看什么

- 不做细粒度 RBAC。scope 目前只有 `kb:read` / `chat:read` 这类粗粒度门禁，写操作靠资源归属判定，不靠 scope。
- 不把租户字段放进请求体。`kb_ids` 是检索范围意图，不是身份。
- 不靠前端隐藏。前端不显示的按钮，后端仍然要判。
- 不在日志里打完整 API Key 或审计 detail 的原文；审计里的 query 摘要已经是用户数据，查询端点要求工作区 owner。
