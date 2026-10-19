# MCP 与 A2A

> 两者**共用同一套检索 / 问答内核**（`services/retrieval/`），各自只做协议适配。
> 这是硬约束：三套逻辑各自演化，半年后会出现“API 搜得到、MCP 搜不到”这类极难排查的不一致。

---

## 1. 为什么要有两个对外入口

| 入口 | 面向 | 典型调用方 |
|---|---|---|
| REST API | 本平台前端与自建客户端 | 浏览器、脚本 |
| **MCP** | AI 客户端要把知识库当“工具”用 | Claude Desktop、Cursor 等 |
| **A2A** | 别的 Agent 要把本服务当“另一个 Agent”调用 | 上游编排 Agent |

MCP 的关键词是 **工具发现与调用**：客户端先 `tools/list`，再 `tools/call`。A2A 的关键词是 **任务与 Agent Card**：调用方先读能力声明，再派任务、取 artifacts。

两者在权限、检索、审计上完全一致，差别只在协议形状与返回结构。

---

## 2. MCP Server

### 2.1 传输与鉴权

```
POST /mcp     MCP Streamable HTTP（无状态，不实现 Mcp-Session-Id）
GET  /mcp     能力自述（不属于协议，给人排查用）
鉴权：Authorization: Bearer <api_key>
```

- 传输层是**无状态**的：不实现会话 ID；
- 支持批量请求（JSON 数组）；全是通知时回 **204**；
- 协议错误也回 **HTTP 200**（错误在 body 的 `error` 里），只有“根本不是 JSON-RPC”才回 400；
- 方法集：`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`；未知方法回 `-32601`。

### 2.2 暴露的四个工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_kb` | — | 列出调用者有权限的 KB（**按权限动态裁剪**） |
| `search_kb` | `kb_id`, `query`, `top_k`, `mode` | 多路 RRF 检索 |
| `ask_kb` | `kb_id`, `question`, `top_k`, `session_id` | RAG 问答（非流式） |
| `get_document` | `kb_id`, `doc_id`, `limit` | 文档元信息 + 前 N 个 chunk |

调用示例：

```jsonc
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"search_kb",
           "arguments":{"kb_id":88,"query":"验收标准那一栏怎么填","top_k":5}}}

→ 200 {"jsonrpc":"2.0","id":1,
       "result":{"content":[{"type":"text","text":"{\n  \"items\": [...]\n}"}],
                 "isError":false}}
```

⚠️ 结果**一律是 JSON 字符串塞进 `content[0].text`**：MCP 客户端必须支持 `text` 内容类型，自造类型会被当“未知类型”丢掉。

### 2.3 工具列表的权限裁剪

```
调用方 token → api_keys.user_id → 有权限的 kb_ids → list_tools 只返回这些 kb 对应的工具实例
```

不能“先返回全部工具、调用时再拒绝”——那会让调用方探测到不该看到的 KB 存在。`initialize` 返回的 `serverInfo` 之后，`tools/list` 里 `kb_id` 的 `enum` 就是该 Key 可读的库。

### 2.4 错误语义

| 情况 | JSON-RPC code | 落在哪 |
|---|---|---|
| 未知 `method` / 未知工具 | `-32601` | 顶层 `error` |
| **参数不合法**（缺必填 / 类型错 / 越界 / 未知参数 / enum 不符） | **`-32602` Invalid params** | 顶层 `error`，message 形如 `search_kb: 缺少必填参数 kb_id` |
| 库不存在 / 无权限 | —（不是协议错） | `result.isError = true` + `{"error":"KB_NOT_FOUND"}` |
| 其它未预期异常 | `-32603` Internal error | 顶层 `error` |

⚠️ **界线**：参数不合法是**协议错**；请求合法但结果失败是**业务结果**（`isError: true`）。

⚠️ **越界报错而不是夹取**：MCP 的调用方多半是个 LLM，悄悄夹取会让它拿着 50 条当 80 条用。

⚠️ 消息里**不出现** `KeyError` / `ValueError` 这类裸异常类型名。

### 2.5 审计

每次 `tools/call` 记入 `audit_log`：`api_key_id` → `user_id` → `kb_ids[]` → query 摘要 → 耗时（`action = mcp_call`）。

---

## 3. A2A Agent

### 3.1 Agent Card

```http
GET /.well-known/agent-card.json     → 能力声明
POST /a2a/tasks                      → 委派任务
```

读响应先看 Agent Card：每个 skill 带 `inputSchema` / `outputSchema`。⚠️ Card **不含任何库的名字**（只有 `readableKbCount`）——那是权限信息。`kb_id` **必填**，但 Card 刻意不列出可用值。

### 3.2 两个 skill

| Agent | skill | 用途 | 实现 |
|---|---|---|---|
| 问答 Agent | `answer` | 单轮查资料 | `ask.prepare` + `stream_answer(one_shot=True)` |
| 研究 Agent | `research` | 多轮检索 + 汇总 | 同一内核，`top_k` 放宽到 30，**按文档分组**逐份给摘录 |

⚠️ `research` **不是**“让 LLM 规划多轮检索”（默认生成层是抽取式，这是隐私红线）；它是**可解释的多角度汇总**，返回里带 `extractive: true`。

### 3.3 请求形状

`POST /a2a/tasks` 收两种入参，都走同一个内部函数：

```jsonc
// ① 协议面：JSON-RPC tasks/send
{"jsonrpc":"2.0","id":1,"method":"tasks/send",
 "params":{"skill":"answer","kb_id":88,
           "message":{"parts":[{"kind":"text","text":"验收标准那一栏怎么填"}]}}}

// ② 便捷面：curl / 手测
{"skill":"answer","kb_id":88,"query":"验收标准那一栏怎么填"}
```

### 3.4 响应形状

A2A Task 对象：

```jsonc
{"id":"...", "contextId":"...",
 "status":{"state":"completed"},
 "history":[...],
 "artifacts":[{"parts":[{"kind":"text","text":"...",
                         "metadata":{"citations":[1,2]}}]}],
 "metadata":{"kb_id":88,"skill":"answer","sources":[...],"documents":[...]}}
```

要点：

- 正文在 **`artifacts[].parts[].text`**（**没有**顶层 `answer`）；
- 引用来源在**顶层 `metadata.sources`**；
- 引用角标在 `artifacts[].parts[].metadata.citations`；
- `capabilities.streaming=false` 是**如实**声明；
- 无权限与不存在**同码**（403）。

### 3.5 审计

`POST /a2a/tasks` 的每个任务都记 `audit_log`（`action = a2a_call`）。

---

## 4. 对外访问的实测姿态

`/mcp` 与 `/a2a/tasks` 在**根路径**（不在 `/api/v1` 下），这是对外契约。实测：

| 探测 | 结果 |
|---|---|
| `POST /mcp` 无 Key | **401** + `INVALID_TOKEN` |
| `POST /mcp` `initialize`（带 Key） | 200，`protocolVersion` + `serverInfo` |
| `POST /mcp` `tools/list` | 200，四个工具；`kb_id.enum` = 该 Key 可读的库 |
| `POST /mcp` `tools/call` 缺 `kb_id` | 200 + JSON-RPC **`-32602`** |
| `GET /.well-known/agent-card.json` | 200，两个 skill 都带 `inputSchema` / `outputSchema` |
| `POST /a2a/tasks` 不带 `kb_id` | **400**（Card 不泄露有哪些库） |
| `POST /api/v1/mcp`、`POST /api/v1/a2a/tasks` | **404** `NOT_FOUND`（不是 405） |

⇒ 对外鉴权只有 API Key 一道。Key 泄露的爆炸半径由租户隔离兜住（**无权限 ≡ 不存在**）。

---

## 5. 与 MCP 客户端对接时的常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端拿不到工具 | `tools/list` 按权限裁剪，先确认 Key 至少能读一个库 | 用 `GET /mcp` 看自述 |
| 调用报参数错但看着没错 | `-32602` 是协议错，检查参数名与 enum | 越界不会被夹取，会直接报错 |
| 业务失败却去看 JSON-RPC `error` | 库不存在/无权限在 `result.isError` 里 | 读 `result.isError` + content |
| 自造 content 类型丢失 | 结果一律是 JSON 字符串进 `content[0].text` | 用支持 `text` 的客户端 |
| 打错前缀得到 404 | `/mcp` 不在 `/api/v1` 下 | 用根路径 |

---

## 6. 相关文档

- 检索内核与降级：[检索与融合](./retrieval.md)
- 权限与审计：[多租户与权限](./tenancy.md)
- 端点总表：[API 契约](./DESIGN-API.md)
