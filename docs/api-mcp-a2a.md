# 对外接口：HTTP、MCP 与 A2A

Knowledge Hub 有三个对外入口，面向三类调用方：

| 入口 | 面向 | 典型调用方 |
|---|---|---|
| REST API | 本站前端与自建客户端 | 浏览器、脚本 |
| MCP | 把知识库当工具用的 AI 客户端 | Claude Desktop、Cursor |
| A2A | 把本服务当另一个 Agent 调用的编排方 | 上游 Agent |

三者的权限、检索、审计完全一致，差别只在协议形状。MCP 的关键词是工具发现与调用，A2A 的关键词是任务与 Agent Card。

## HTTP 通用约定

- 基址：`/api/v1`。`/mcp`、`/a2a/tasks`、`/.well-known/agent-card.json`、`/health` 在根路径，不在 `/api/v1` 下。
- 鉴权：`Authorization: Bearer <api_key>`。请求体不接受租户字段。
- 错误体统一为 `{"error": {"code", "message", "detail?", "request_id"}}`；每个响应带 `x-request-id` 与 `x-elapsed-ms`。
- 分页：`page` 从 1 开始，`page_size` 默认 20、上限 100。

错误码与 HTTP 状态：

| code | 状态 | 什么时候 |
|---|---|---|
| `INVALID_REQUEST` | 400 | 参数缺失或格式错 |
| `INVALID_TOKEN` | 401 | Key 缺失、无效、吊销或过期 |
| `KB_NOT_FOUND` | 403 | 资源不存在**或无权限**，两者同码 |
| `DOC_NOT_FOUND` | 404 | 文档不存在或无权限 |
| `REGISTRATION_CLOSED` | 403 | 自助注册已关 |
| `FORBIDDEN` | 403 | 操作级拒绝，如非超管改全局配置 |
| `FILE_TOO_LARGE` | 413 | 单文件超 50 MB 或单请求超 20 个 |
| `UNSUPPORTED_FORMAT` | 415 | 扩展名或文件头不符 |
| `VALIDATION_FAILED` | 422 | 校验失败，如页码越界 |
| `RATE_LIMITED` | 429 | 注册或登录失败超限，带 `Retry-After` |
| `MODEL_UNAVAILABLE` | 503 | 生成器配置缺失 |
| `INTERNAL` | 500 | 未预期异常 |

`KB_NOT_FOUND` 用 403 而不是 404 是刻意的：无权限与不存在必须无法区分，否则错误码本身成了存在性探针。

## 端点地图

契约在 [`docs/DESIGN-API.md`](DESIGN-API.md)，那里是逐端点的请求与响应形状。按 router 分组大致是：

| 组 | 主要端点 | 说明 |
|---|---|---|
| 认证 | `POST /auth/register`、`/auth/login`、`GET/PATCH /auth/me`、`/auth/keys` | 登录每次新铸一把 key |
| 工作区 | `/workspaces`、成员、邀请码、恢复 | 邀请码明文只返回一次 |
| 知识库 | `/kb`、`/kb/{id}`、`/kb/trash`、`/kb/{id}/restore` | 三档可见性，软删 30 天 |
| 文档 | 上传、列表、详情、chunks、页图、预览、缩略图、重试、重解析、删除、恢复 | 上传返回 202，摄取异步 |
| 检索 | `/kb/{id}/search`、`/search`、`/kb/{id}/search/debug` | 三者共用 `engine.search` |
| 问答 | `/kb/{id}/ask`、`/ask`、`/kb/{id}/ask/preview` | SSE，帧序固定 |
| 会话 | `/sessions`、`/kb/{id}/sessions` | 会话是个人私产 |
| 导入 | `/kb/import/chat`、`/kb/{id}/import/dir`、`/kb/import/roots` | 服务端路径，白名单 |
| 时间线 | `/kb/{id}/timeline`、`/photos`、`/anniversaries`、`/milestones` | 只对 chat / photo 库有意义 |
| 任务 | `GET /jobs`、`/jobs/{id}` | 只读，租户按 `kb_id` 过滤 |
| 运维 | `/admin/reindex`、`/admin/consistency`、`/admin/llm`、`/admin/users`、`/workspaces/{id}/stats` | 部分仅超管 |
| 审计 | `GET /audit` | 只能查自己管理的工作区 |
| PII | `/kb/{id}/pii` | 只读报告，不返回原文片段 |

`GET /kb/{id}/timeline` 只允许 `month` 与 `date` 二选一，两个都给会 400。时间线视图只对 `source_kind` 为 `chat` 或 `photo` 的库有意义，其它库返回 400 而不是空列表——“不适用”与“没有记录”必须能区分。

`POST /kb/{id}/ask` 即使 `stream=false` 也返回 SSE，只是把增量合并成一个 `delta` 帧。这样 MCP、A2A 与浏览器共用一条代码路径，少一种响应格式。

## MCP 怎么工作？

`POST /mcp` 是无状态的 Streamable HTTP，不实现 `Mcp-Session-Id`。鉴权复用 API Key。支持批量 JSON-RPC；全是通知时返回 204；协议错误也回 HTTP 200，错误在 body 的 `error` 里，只有“根本不是 JSON-RPC”才回 400。`GET /mcp` 是能力自述，不属于协议，给人排查用。

实现的方法只有 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`。未知方法回 `-32601`。

四个工具：

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_kb` | — | 列出调用者有权限的库 |
| `search_kb` | `kb_id`、`query`、`top_k`、`mode` | 多路检索，`top_k` 上限 50 |
| `ask_kb` | `kb_id`、`question`、`top_k`、`session_id` | 非流式问答，内部复用 SSE 内核 |
| `get_document` | `kb_id`、`doc_id`、`limit` | 元信息与前 N 个 chunk，`limit` 上限 50 |

`ask_kb` 不是另写一条非流式路径，而是调 `stream_answer(one_shot=True)`，再把 SSE 帧解析回 JSON。这样 MCP 与 A2A 的答案、引用、悬空角标与浏览器完全同源。

工具结果一律是 JSON 字符串塞进 `content[0].text`。MCP 客户端必须支持 `text` 内容类型，自造类型会被当成未知类型丢掉，表现为“调用成功但没有内容”。

权限裁剪做两处：

1. `tools/list` 里 `kb_id` 的 schema 用 `enum` 列出可读的库；一个库都没有时，`search_kb`、`ask_kb`、`get_document` 三个工具直接不暴露，只留 `list_kb`。
2. `tools/call` 仍然走 `assert_kb_access`。schema 的 `enum` 是给调用方看的提示，不是访问控制；只靠它的话，改一次客户端入参就能越权。

错误分两层：

| 情况 | 落点 |
|---|---|
| 未知方法或未知工具 | 顶层 `error`，`-32601` |
| 参数不合法（缺必填、类型错、越界、未知参数、enum 不符） | 顶层 `error`，`-32602`，消息形如 `search_kb: 缺少必填参数 kb_id` |
| 库不存在或无权限 | `result.isError=true`，内容里是 `KB_NOT_FOUND` |
| 其它未预期异常 | 顶层 `error`，`-32603` |

参数错是协议错，业务失败是结果失败。混在一起的话，模型会把“我少传一个参数”误读成“这个库不存在”，于是换一个不存在的 `kb_id` 反复试。越界报错而不是静默夹取，因为调用方多半是 LLM，悄悄把 80 改成 50 会让它以为拿到了 80 条。消息里不出现 `KeyError`、`ValueError` 这类裸异常名。

每次 `tools/call` 都记审计：`api_key_id` → `user_id` → `kb_ids` → query 摘要 → 耗时。`tools/list` 不记，它不带 query，也不改数据。

## A2A 怎么工作？

```http
GET  /.well-known/agent-card.json     能力声明
POST /a2a/tasks                       委派任务
```

Agent Card 声明协议版本、传输方式、鉴权方式与两个 skill，每个 skill 带可机器读的 `inputSchema` 与 `outputSchema`。Card 里**不列任何库的名字**，只有 `readableKbCount`；`kb_id` 必填，但可用值刻意不暴露，那是权限信息。

| skill | 用途 | 实现 |
|---|---|---|
| `answer` | 单轮查资料 | `ask.prepare` + `stream_answer(one_shot=True)` |
| `research` | 多角度检索后按文档汇总 | 同一内核，`top_k` 放宽到 30，最多汇总 10 份文档 |

`research` 不是“让 LLM 规划多轮检索”。默认生成层是抽取式，这是隐私红线；它做的是可解释的多角度汇总：把命中按文档分组，每份文档给出最相关的两句摘录与出处，返回里带 `extractive: true`，并如实说明是否因上限被截断。

`POST /a2a/tasks` 收两种入参，走同一个内部函数：

```jsonc
// 协议面
{"jsonrpc":"2.0","id":1,"method":"tasks/send",
 "params":{"skill":"answer","kb_id":88,
           "message":{"parts":[{"kind":"text","text":"验收标准那一栏怎么填"}]}}}

// 便捷面，curl 与手测用
{"skill":"answer","kb_id":88,"query":"验收标准那一栏怎么填"}
```

响应是 A2A Task 对象。三个位置容易看错：

- 正文在 `artifacts[0].parts[0].text`，没有顶层 `answer`；
- 引用来源在顶层 `metadata.sources`，`idx` 与正文的 `[n]` 对应；
- 引用角标在 `artifacts[0].parts[0].metadata.citations`，悬空角标也在这里。

`capabilities.streaming=false` 是如实声明：这个端点是非流式的，声明了流式客户端会试着订阅，而那必然失败。无权限与不存在同样返回 403 与 `KB_NOT_FOUND`。

每个任务都记 `a2a_call` 审计，包含 skill、query 摘要、耗时与错误码。

## 契约怎么治理？

`docs/DESIGN-API.md` 是唯一契约，当前声明 82 个端点：81 个已实现，1 个明确不做。`eval/contract_check.py` 校验路径、方法与**参数面**，`--strict` 时未实现端点会让退出码非 0。

契约漂移在这个项目里发生过两次，都是人工发现的：页图路径写成 `/pages/{n}/image`，实现是 `/pages/{n}`；`/preview` 的返回类型写成 PDF，实现返回 JPEG。两次都不影响已写好的代码，所以没有测试会红，等到照契约新写前端时才咬人。

因此还有两层守卫：`tests/test_contract_parser.py` 钉住解析器本身，`tests/test_docs_pointers.py` 钉住 README 与入口文档里的路径、命令、端口与 pytest 项数。前端 `web/src/types/api.ts` 是契约的手抄投影，接口对齐以它和 `DESIGN-API.md` 为准。

新增端点时先改契约，再接路由，再补 `web/src/types/api.ts` 与审计接线，最后跑 `eval/contract_check.py`。

## 相关文档

- 检索与问答内核：[检索与问答](retrieval.md)
- 权限与审计：[多租户与权限](tenancy.md)
- 逐端点的请求与响应：[API 契约](DESIGN-API.md)
