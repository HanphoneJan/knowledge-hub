# API 契约设计 — v1

> 2026-10-11 校订。
>
> 供前端与后端并行开发；契约变更需同步改本文。`eval/contract_check.py` 读**全部** `DESIGN-API*.md`
> 的端点声明（路径 + 方法 + query 参数面）。
> 🔑 **改端点先改这里**；契约与代码不一致时**以代码为准并立刻改契约**（改契约与改代码必须同一轮完成）。

## 0. 通用约定

### 0.1 基址与鉴权

```
基址：http://127.0.0.1:8010/api/v1          # deploy/dev.sh，可用 PKH_API_PORT 覆盖
鉴权：Authorization: Bearer <api_key>       # 每用户一把
```

> ⚠️ **端口以代码为准**：只有 `deploy/dev.sh` 的
> `PORT="${PKH_API_PORT:-8010}"` 一个来源，前端 dev proxy 也指向 8010（`web/vite.config.ts`）。
> **所有端点（除 `/health` 与 `/auth/*`）都要求 Bearer Token。**

### 0.2 错误响应

```json
{"error": {"code": "KB_NOT_FOUND", "message": "知识库不存在或无访问权限",
           "detail": {"kb_id": 999}, "request_id": "req_01H..."}}
```

> **权限不足与"不存在"返回同一个 code**（fail-closed）—— 不能让攻击者通过错误码差异探测资源是否存在。

### 0.3 错误码表

| HTTP | code | 含义 |
|---|---|---|
| 400 | `INVALID_REQUEST` | 参数缺失/格式错 |
| 401 | `INVALID_TOKEN` | 缺失或无效的 API Key |
| 403 | `KB_NOT_FOUND` | **同时覆盖"无权限"和"不存在"** |
| 404 | `DOC_NOT_FOUND` | 文档不存在或无权限 |
| 413 | `FILE_TOO_LARGE` | 超过 50MB |
| 415 | `UNSUPPORTED_FORMAT` | 非白名单格式 |
| 422 | `VALIDATION_FAILED` | 语义校验失败（如页数超限） |
| 429 | `RATE_LIMITED` | 超出配额 |
| 500 | `INTERNAL` | 服务端错误 |
| 503 | `MODEL_UNAVAILABLE` | 模型未就绪/加载失败 |

### 0.4 分页

```
GET /xxx?page=1&page_size=20      默认 page=1, page_size=20, 上限 100
响应：{"items":[...], "total": 123, "page":1, "page_size":20}
```

> ⚠️ **以代码为准的现状**：上面的分页包**只有部分端点真的返回它**，其余返回裸数组：
>
> | 返回 `{"items","total","page","page_size"}` | 返回裸数组 `[...]` |
> |---|---|
> | `GET /kb/{id}/sessions` · `POST /kb/{id}/search` · `POST /search` · `GET /jobs` | `GET /kb` · `GET /workspaces` · `GET /kb/{id}/documents` · `GET /kb/{id}/trash` · `GET /documents/{id}/chunks` · `GET /workspaces/{id}/members` · `GET /auth/keys` |
>
> （`SearchResp` 只有 items/total/mode/page + 可选的 `debug`，**没有 `page_size`**。）
> 裸数组端点照样接受 `page`/`page_size`（上限 100），只是**丢掉了 `total`**。**新端点一律用分页包**
> （`services/common/pagination.py::Page`）；把历史裸数组端点改成包属于**破坏性变更**，要单独排期。

### 0.5 通用字段约定

| 概念 | 字段 | 类型 |
|---|---|---|
| 可见性 | `visibility` | `private` / `workspace` / `global` |
| 文档状态 | `status` | `pending`/`parsing`/`chunking`/`embedding`/`ready`/`failed` |
| 引用类型 | `kind` | `text` / `page` / `image` |
| 时间 | 所有时间字段 | ISO 8601 带时区 |

---

## 1. 认证端点

```http
POST /api/v1/auth/register
Content-Type: application/json
{"email":"a@b.com","password":"...","display_name":"A","nickname":"可选"}
→ 201 {"user":{...},"api_key":"sk-...","key_prefix":"sk_1a2b3c"}
```

> **明文 api_key 只在此处返回一次**，之后不可再取。

⚠️ **限流**：此端点不需要 API Key，而部署通常对公网可达（空体请求返回 422 而不是 401），
所以它是"往库里塞垃圾账号"最省事的入口。

| 条件 | 响应 |
|---|---|
| 同一客户端 IP 在 3600s 内第 21 次 | `429 RATE_LIMITED` + **`Retry-After: <秒>`** 头 |
| 对端是回环且**无**任何转发头 | 走单独宽松桶（500/小时）—— 本机 curl 与验收脚本都在这里 |

- 桶键取真实客户端 IP：`CF-Connecting-IP`（**仅当对端是回环/内网时采信**，见 `services/api/rate_limit.py`）
- 🔑 限流**先于**业务判断 ⇒ 超限时**不泄露**「该邮箱已注册」

🔴 **自助注册开关**：`.env` 里 `ALLOW_REGISTER=false` 时关闭。`false` 且来源不是本机 → `403 REGISTRATION_CLOSED`；
本机（回环/内网且无任何转发头）照旧。⚠️ **开关在限流之前**：关掉就是关了（403），且**一次桶都不记**。
关掉后要新建账号走 `services/api/scripts/create_user.py`（本机 CLI）—— **不留任何 HTTP 后门**。

```http
POST /api/v1/auth/login
{"email":"a@b.com","password":"..."}       // email 字段可填邮箱或昵称
→ 200 {"api_key":"sk-...","key_prefix":"sk_1a2b3c"}
```

🔴 **失败限流**：这是唯一不需要任何凭证就能到达的"数据入口"。

| 条件 | 响应 |
|---|---|
| 同一客户端 IP 在 300s 内第 11 次**失败** | `429 RATE_LIMITED` + **`Retry-After: <秒>`** |
| 同一**账号**在 900s 内第 21 次失败（换 IP 也算） | 同上 |
| 对端是本机（回环且无转发头） | 走单独宽松桶（100 次/300s）|

- 🔑 **只数失败，成功不计数** —— 按"次数"限会把主人锁在自己的正常使用里
- 🔑 **IP 桶 + 账号桶都要有**：账号桶才是保护某个具体密码起作用的那个（换 IP 无效）
- 🔑 429 的文案**不说是哪个桶**，也**不说密码对不对**；用户不存在与密码错误都返回 `400 账号或密码不正确`

```http
GET    /api/v1/auth/keys          → 列表（不含明文，裸数组）
POST   /api/v1/auth/keys          {"name":"Cursor"} → 新建，返回明文一次
DELETE /api/v1/auth/keys/{id}     → 吊销
GET    /api/v1/auth/me            → 当前用户
PATCH  /api/v1/auth/me            → 改自己的资料（显示名/昵称/邮箱/密码）
```

> 路径参数名是 `{key_id}`（`api_keys.id`）。⚠️ 吊销是**删行**，**没有** `revoked_at`；字段名是 `name` 不是 `label`。
> `PATCH /auth/me` 改 `email` 或密码**必须**带 `current_password`。

---

## 2. 工作区

```http
GET    /api/v1/workspaces
POST   /api/v1/workspaces          {"name":"...","description":"..."}
GET    /api/v1/workspaces/{id}
PATCH  /api/v1/workspaces/{id}
DELETE /api/v1/workspaces/{id}      → 软删除（沿用 30 天机制）
POST   /api/v1/workspaces/{id}/restore   → 撤销软删除（仅 owner）
GET    /api/v1/workspaces/trash     → 回收站列表（裸数组 WorkspaceOut，**仅 owner**）
POST   /api/v1/workspaces/{id}/members   {"email":"x@y.com","role":"member"}
GET    /api/v1/workspaces/{id}/members
DELETE /api/v1/workspaces/{id}/members/{user_id}

# 邀请码（不接邮件服务 → 邀请必须自服务化）
POST   /api/v1/workspaces/{id}/invites   {"ttl_days":7,"max_uses":1}
GET    /api/v1/workspaces/{id}/invites
DELETE /api/v1/workspaces/{id}/invites/{invite_id}
POST   /api/v1/invites/redeem        {"code":"ABCD2345"}
```

### 2.1 邀请码

`POST /members` 只能加**已注册**的邮箱（没注册就 400），而设计上明确**不接邮件服务** —— 邀请码把它变成**自服务**。

| 端点 | 谁能调 | 说明 |
|---|---|---|
| `POST /workspaces/{id}/invites` | owner/admin | 生成，**明文只返回这一次** |
| `GET /workspaces/{id}/invites` | owner/admin | 列表（**只有前缀**，没有明文） |
| `DELETE /workspaces/{id}/invites/{invite_id}` | owner/admin | 撤销（幂等：重复撤销仍 204） |
| `POST /invites/redeem` | 登录用户 | 兑换 → 成为该工作区的 member |

🔑 **四条设计约束**：① 库里只存 HMAC-SHA256 哈希 + 前 4 位（每码随机盐，与 API Key 同一纪律）；
② 口令 8 位、去掉 `0O1IL`，规范化接受空格/连字符/小写但**不会**把 `0` 猜成 `O`；③ 默认 7 天 / 1 次（`max_uses` 上限 50）；
④ 🔴 **兑换失败统一一句话**（不存在/过期/用完/撤销都是同一句），区分开等于给攻击者一个探测信号。

⚠️ 兑换的校验顺序是 **先验哈希 → 再看"是不是已经是成员" → 最后验状态**（否则重复兑换会得到"邀请码无效"）。
⚠️ **已经是成员不算失败**：返回 200 + `already_member: true`。⚠️ `POST` 的请求体**没有 `role`**（只能给 member）。
⚠️ `used <= max_uses` 是**数据库 CHECK**（`ck_invite_used`）；撞上时返回 **400**（本项目错误词汇里没有 409）。

**数据可见范围**（服务端推导，不可由请求指定 ——）：私有=仅创建者；工作区=workspace 全部成员；全局=所有用户。

---

## 3. 知识库

```http
GET    /api/v1/kb?workspace_id=1&visibility=private&page=1
POST   /api/v1/kb
{
  "workspace_id": 1, "name": "项目文档", "description": "", "visibility": "private",
  "source_kind": "file",            // file | chat | photo
  "rerank_model": null,             // null = 不启用；否则是 cross-encoder 模型名
  "chunk_size": 500, "chunk_overlap": 50,
  "chunk_strategy": "recursive",   // fixed|recursive|semantic|parent_child|late|contextual
  "generate_image_desc": false      // }
GET    /api/v1/kb/{id}               → 含统计 {doc_count, chunk_count, size_bytes, stale_strategy_docs}
PATCH  /api/v1/kb/{id}               // 见下方可改字段清单
DELETE /api/v1/kb/{id}              → 软删除（其下文档一并进回收站）
POST   /api/v1/kb/{id}/restore      → 撤销软删除，**连带恢复那次删掉的文档**并重新摄取
GET    /api/v1/kb/trash             → 回收站列表（裸数组 KbOut，**按删除时间倒序**）

// 🔑 `GET /kb/{id}` 的 `stale_strategy_docs` = 还停在**旧切分策略**的当前文档数
//   （文档摄取时记在 `documents.quality.strategy` 的策略 ≠ 该库当前 `chunk_strategy`）。
//   改策略只对之后的摄取生效 ⇒ 它是"重摄取欠账"，用于提示"这 N 份可点重解析"。

// 🔑 PII 检出报告—— 只读
GET    /api/v1/kb/{id}/pii
```

**`PATCH /kb/{id}` 的字段清单**（不在表里的字段会被 pydantic **静默忽略** —— 所以"传了没生效"的第一问是"它在不在这张表里"）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `name` / `description` | str | 基本信息 |
| `visibility` | `private`/`workspace`/`global` | 三档 |
| `generate_image_desc` | bool | （默认关） |
| `pii_mask` | bool | 🔴 **默认 false**，只对之后的摄取生效 |
| `chunk_size` / `chunk_overlap` | int | 分块参数；⚠️ **不自动重跑索引**，要 `POST /documents/{id}/reparse` |
| `chunk_strategy` | 见上 | 切分策略（默认 `recursive`）；⚠️ 同 `chunk_size`，只对之后重新摄取的文档生效 |
| `rerank_model` | str \| null | 阶段 3 per-KB 精排。**`null` 与 `""` 都表示"不启用"**，**不是**"用全局默认" |

### 3.1 库与工作区的回收站

`DELETE /kb/{id}` 与 `DELETE /workspaces/{id}` 都是软删（30 天窗口），`POST …/restore` 能放回去 ——
但**没有任何地方能列出"我删过哪些"**的话，用户只能凭记忆按 id 调恢复端点。**看不见的窗口等于没有**。

| 端点 | 谁能调 | 说明 |
|---|---|---|
| `GET /kb/trash` | 登录用户 | 软删的库里**我能恢复**的那些（判据 = `can_write_kb`，按删除时间倒序） |
| `GET /workspaces/trash` | **仅 owner** | 我删过的工作区 |

⚠️ 两条都**不能用 `readable_kb_ids`/`workspace_ids` 过滤**：软删的行按定义就不在那两个集合里 → 恒为空。
**软删的行必须"先按行取、再判权限"**。⚠️ `KbOut` 与 `WorkspaceOut` 各多了一个 `deleted_at`（只有回收站列表里非空），
前端靠它算"还能恢复几天"。⚠️ 库在**软删的工作区**里时不会出现在 `GET /kb/trash`（必须先恢复工作区，这是故意的顺序）。
⚠️ 回收站的可见性判据是**写**那一侧（`writable_kb_clause`），**不是**读判据（`visible_kb_clause`）——
用读判据会出现"列表里有、点恢复 403"（`tests/test_container_trash.py` 用**真值表**逐格比对）。

### 3.1b PII 掩码与检出报告

🔑 **为什么默认关闭**：掩码只作用于**索引侧**，但它让"搜我自己的手机号/身份证号"失效 —— 而那正是个人知识库的核心用途。
所以开关是**按库**的。🔑 **三条边界**：① **只掩索引侧**（blob 原件、`/preview` 页图保持原文）；
② **不回溯**（只对之后摄取/`reparse` 的文档生效）；③ **报告不含原文片段**（只给计数与文件名）。

`GET /kb/{id}/pii` 回答**两个不同的问题**，给**两套数字**：

| 字段 | 含义 | 来源 |
|---|---|---|
| `source_total` / `source_by_type` | **原文**里检出几处 | `documents.pii_hits`（摄取时掩码**前**记下） |
| `indexed_total` / `indexed_by_type` | 索引文本**现在还剩**几处 | 现场扫 `chunks.content_norm` |
| `docs_not_scanned` | `pii_hits IS NULL` 的文档数（还没摄取完）| ⚠️ **不等于「干净」** |
| `docs_no_text` | 已摄取但**没有文本层**（照片/纯扫描件，`chunk_count = 0`）| 🔴 它们的 0 **不代表图片里没有敏感信息** |
| `docs` / `docs_truncated` | 逐份明细（按命中数降序，上限 200）| `source_total` 是**全量** |
| `scan_chars` / `scan_truncated` | 现场扫描读了多少字符、是否达上限（**4,000,000 字符**，`SCAN_CHAR_LIMIT`）| ⚠️ 原文写「4 MB」是错的，代码数的是**字符**（按代码更正）|

⚠️ 开了掩码就是 `source_total=7 / indexed_total=0` —— **这两个数字混成一个会让用户以为掩码没生效**。

---

## 4. 文档

### 4.1 上传

```http
POST /api/v1/kb/{kb_id}/documents
Content-Type: multipart/form-data
files: <最多 20 个文件>            // 每个 ≤50MB
generate_image_desc: false               // 可选
→ 202 {"batch_id": "bat_01H...",
       "documents": [{"id":123,"file_name":"a.pdf","status":"pending"}, ...]}
```

**约束校验顺序**（能降级就降级）：

| 阶段 | 检查 | 失败处理 |
|---|---|---|
| 上传时 | 格式白名单、大小 ≤50MB | 415 / 413，**同步返回** |
| 白名单内容 | `.pdf` `.md` `.markdown` `.txt` `.csv` `.json` `.html` `.htm` `.docx` `.xlsx` `.pptx` ＋ 图片 `.jpg` `.jpeg` `.png` `.webp` `.bmp` `.tiff` | 🔑 **由 `PARSERS` 单一来源派生**⇒ **加一种格式 = 加一条记录** |
| 上传时 | 加密 PDF | **不拒绝**，入队后转视觉路径 |
| 解析时 | 页数 >2000 页 | 422，doc.status=failed |
| 解析时 | 扫描件 >50 页 | 422，doc.status=failed |
| 解析时 | 文件完全损坏 | 400，doc.status=failed + status_msg |

> ⚠️ 上传接口**同步**只返回格式白名单与大小（415/413）；解析期三条都是**异步**发生的，
> `documents.status` 由**终态判定**写入（`services/worker/doc_state.py`）。

### 4.2 列表与详情

```http
GET /api/v1/kb/{kb_id}/documents?status=ready&page=1&page_size=50
→ [{"id":123,"file_name":"a.pdf","size_bytes":1234567,
   "status":"ready","status_msg":null,"is_current":true,"deleted_at":null,
   "chunk_count":87,"page_count":18,"is_scanned":false,
   "mime_type":"application/pdf","source_path":null,"uploaded_by":3,
   "created_at":"...","updated_at":"..."}]
```

> ⚠️ **三处以代码为准**：① **裸数组，不是分页包**（见 §0.4）；② **主键字段是 `id`，不是 `doc_id`**
> （`DocumentOut` 上有个 `doc_id` property，但 pydantic 不序列化普通 property）；③ **没有 `progress` 字段**，
> 进度在 `jobs` 表上，经 `GET /api/v1/jobs?doc_id=…` 取。

```http
GET /api/v1/documents/{doc_id}
→ {...DocumentOut..., "content_hash":"…", "preview_url":"/api/v1/documents/123/preview"}

GET /api/v1/documents/{doc_id}/chunks?page=1&page_size=100
→ [{"chunk_index":7,"content_norm":"...","char_start":1204,"char_end":1603,
   "locations":[{"page":1,"bbox":[0.15,0.19,0.31,0.21]}],
   "token_count":312,"kind":"text","table_index":null,"row_count":null,"col_count":null}]
```

> ⚠️ chunk 的正文字段是 **`content_norm`**，**没有 `text_norm` 也没有 `text_raw`**。要原文走 `SearchHit.text`。

### 4.3 更新与删除

```http
POST   /api/v1/documents/{doc_id}/reparse        # 重跑解析（改了分块配置后）
POST   /api/v1/documents/{doc_id}/retry          # 失败后重试（重新入队 KIND_INGEST）
DELETE /api/v1/documents/{doc_id}                 # 进回收站（30天）
POST   /api/v1/documents/{doc_id}/restore         # 从回收站恢复
GET    /api/v1/kb/{kb_id}/trash                   # 回收站列表（裸数组 DocumentOut，带 deleted_at）
DELETE /api/v1/trash?kb_id=&dry_run=            # 清空 → PurgeResp（参数见下）
```

**`DELETE /trash` 是本项目唯一不可撤销的端点**（删用户、删 KB 都还有软删兜底）：

```jsonc
// GET 不存在，用 DELETE + dry_run 预览
// ?kb_id=88 不给 → 清"我能读且能写"的**全部库**（scope 更大）
// ?dry_run=true → 只报告，不动任何数据
{"docs": 2, "chunks": 75, "blobs_dropped": 1, "bytes_freed": 318367,
 "doc_ids": [1313, 1314], "ids_truncated": false, "qdrant_failed": [], "dry_run": true}
```

| 字段 | 含义 |
|---|---|
| `docs` / `chunks` | 删了几份文档、连带多少 chunk |
| `blobs_dropped` / `bytes_freed` | **引用计数归零、文件真被删**的份数与字节 |
| `doc_ids` | 最多 500 个（`ids_truncated` 说明被截断）|
| `qdrant_failed` | **PG 已删但索引点没清掉**的文档 id（不吞：对账会捡，但要先有人知道）|
| `dry_run` | `true` = 这是预测值，什么都没删 |

⚠️ **恒定条件**：只清 `is_current = FALSE` 的文档（当前版本从来没进过回收站，30 天「反悔窗口」对它**不成立**）。
**覆盖式更新**：上传同名文件时旧文档 `is_current=FALSE, deleted_at=now()` 进回收站（旧向量由**周期清理**任务清掉，**不留 30 天**），新文档 `is_current=TRUE`。

### 4.4 预览与缩略图（L3 /）

```http
GET /api/v1/documents/{doc_id}/preview
→ application/pdf            # Office 文档返回转换后的 PDF（v1.1）；PDF/图片/MD 返回原格式或纯文本

GET /api/v1/documents/{doc_id}/pages/{page_no}?width=1200
→ image/jpeg                # 单页渲染图，L3 底图。width 必须在 100~3000
GET /api/v1/documents/{doc_id}/preview?page=1&chunk_index=7&width=1200
→ image/jpeg                # 服务端用 pymupdf 把 chunk 的框画进同一页（对照/调试用）

GET /api/v1/documents/{doc_id}/thumbs/{page_no}
→ image/jpeg                # 缩略图（：入库时预生成，宽 400px）
```

> 🔑 **v1.1：Office 预览走 LibreOffice 转换**。摄取成功后**派生**一个 `KIND_PDF_PREVIEW` 任务，
> 产物是 `data/blobs/{aa}/{sha}/preview.pdf`。三个端点的"预览源"由 `documents.py::_preview_source` **共用一份**。
> ⚠️ **转换会改变排版** ⇒ 页码与坐标与原件不一致；🔑 **带框预览（`?chunk_index=`）的坐标来自"抽这份坐标的那个文件"**
> ，两者都与被渲染的页**同源**。**定位不到的那些 chunk 故意没有坐标** → 带框预览返回 **415 并说清原因**；
> `chunk_index` 写错了（不存在）返回 **403/404**。⚠️ 转换**失败不**让文档失败（预览是增强）；
> 转换没跑完时 `/preview` 与 `/pages/{n}` 返回 **415 并说明原因**，不是 500。⚠️ 转换结果**按内容寻址复用**。

> 🔴 **参数名修正**：早期写的是 `{chunk_id}`，但缩略图存的是**页**（`data/blobs/{aa}/{sha}/thumbs/{page}.jpg`），
> `chunk_index` 不是页码。`thumb_url` 的推导在 `retrieval/types.py::thumb_url_for`：有 `locations[].page` 用它 →
> 页级向量用 `payload["page"]` → 兜底 `chunk_index < 0` 时按 `-(chunk_index)-1` 反推；**都没有就返回 `null`**。

🔴 **两处路径/返回类型的修正（原契约写错，照它写前端会失败）**：

```
❌ 契约旧版：GET /documents/{id}/pages/{n}/image?width=
✅ 实现：    GET /documents/{id}/pages/{n}?width=
❌ 契约旧版：GET /documents/{id}/preview  →  application/pdf
✅ 实现：                                 →  image/jpeg（第 1 页，可选叠加红框）
```

第二条尤其要小心：`DocumentDetail.preview_url` 指向的就是这个端点，而它**给的是第 1 页的 JPEG，不是 PDF**。

> 页图端点要 `Authorization: Bearer`，**前端不能用 `<img src>`**：`<img>` 带不了请求头 → 静默 401。

**L3 坐标约定**（实测已归一化到 0~1，与分辨率无关，往返误差 0.00e+00）：

```json
// 正文 chunk：单框
{"page": 2, "bbox": [0.1360, 0.2301, 0.8638, 0.2585]}
// 表格 chunk：每非空单元格一个框，带 row/col 让前端排网格
{"page": 3, "bbox": [0.1055, 0.1954, 0.2446, 0.2403], "row": 2, "col": 0}
```

> **前端必须区分两种形态**：`kind="text"` 单框直接画；`kind="table"` 多框按 `row`/`col` 画网格（实测框数上限 200）；
> `kind="page"` 扫描件**只高亮整页**。判据是 `locations` 的**形状**，不是 `kind`。

---

## 5. 检索

### 5.1 问答（语义）

```http
POST /api/v1/kb/{kb_id}/ask
{"question":"...","session_id":null,"top_k":8,"stream":true}
→ SSE (text/event-stream)
event: sources
data: {"sources":[
  {"idx":1,"kind":"text","score":0.032,"title":"张三-项目立项书","date":"","doc_id":123,"chunk_index":3,
   "text":"本文件说明立项背景与验收标准……","locations":[{"page":1,"bbox":[0.15,0.19,0.31,0.21]}],"thumb_url":null},
  {"idx":2,"kind":"image","score":0.028,"title":"2025-02-14 情人节.jpg","date":"2025-02-14","doc_id":345,
   "chunk_index":0,"text":"","locations":null,"thumb_url":null}],"retrieval_ms":312}
event: delta
data: {"text":"根据检索到的内容"}
event: done
data: {"citations":[1,2],"elapsed_ms":1840,"retrieval_ms":312,"generator":"extractive",
       "dangling_citations":[],"session_id":57,"history_turns":2,
       "legs":{"dense":0.9,"trgm":1.4,"sparse":1.1}}
```

> ⚠️ `done` 帧比契约早期多五项，**别当多余字段忽略**：`retrieval_ms`（验证 「检索段 < 500ms」）·
> `generator`（`extractive` 或模型名）· `dangling_citations`（答案里引用了不存在的编号时非空，**前端必须提示"这条引用失效了"**）·
> `session_id`（**多轮靠它续问**）· `history_turns`（：这一轮带进生成的上下文条数，第一轮是 0）。

**性能目标**：首 token < 2 秒；**检索段必须 < 500ms**；生成段靠 SSE 逐字输出掩盖慢速。
**多库问答**：范围是主题时可跨库提问，与单库同一套内核。

```http
POST /api/v1/ask
{"question":"...","kb_ids":null,"session_id":null,"top_k":8}
```

`kb_ids=null` = 全部有权限的库（服务端推导）。续问时**沿用会话自己的范围**。

```http
GET /api/v1/kb/{kb_id}/ask/preview?q=验收标准那一栏怎么填&top_k=8
→ {"question":"…","retrieval_ms":312,
   "evidences":[{"idx":1,"doc_id":345,"chunk_index":3,"score":0.032,"legs":["dense","sparse"],
                 "ranks":{"dense":2,"sparse":1},"title":"…","text":"…（≤400 字）","locations":[…]}]}
```

**只检索不生成**（前端"换个说法再问"之前先看证据列表用），调的是同一个 `engine.search`（§0.3）。

### 5.2 搜索（精确匹配 + 双路召回）

```http
POST /api/v1/kb/{kb_id}/search
{"query":"1381234","mode":"exact","top_k":20,"page":1,"include_debug":false}
→ {"items":[{"doc_id":345,"file_name":"产品需求.md","chunk_index":3,
             "text":"…我的手机号是138****5678…",
             "snippets":[{"text":"…","highlight":"1381234","positions":[[12,19]]}],
             "score":0.0326,"kind":"text","locations":[{"page":1,"bbox":[0.15,0.19,0.31,0.21]}],"thumb_url":null}],
   "mode":"exact","total":1,"page":1,"debug":null}
```

> **`include_debug`**：默认 `false`（`debug` 为 `null`）；传 `true` 时附上四路召回 + RRF 融合的全过程，
> **形状与 §5.4 的 `/search/debug` 去掉 `query`/`mode` 后一致**（共用 `routers/search.py::_debug_block`）。
> **`text`**：chunk 正文，上限 800 字。⚠️ **不要只靠 `snippets` 渲染结果** —— 它是 `re.escape(query)`
> 的**字面子串**匹配产物，自然语言提问从来不是原文子串（实测 100% 为空）。

| mode | 走哪条路 | 能搜什么 |
|---|---|---|
| `semantic` | Qdrant dense | 意思相近 |
| `exact` | Postgres trgm | 子串（`1381234`、`优秀学生奖学金`） |
| `hybrid` | 默认 | dense + BM25(Qdrant) + trgm(PG) 三路 RRF（/）|

> ⚠️ **「三路」有两种意思，别混**：这里指**三条文本腿**（dense/sparse/trgm）；而 /规格里的「三路混合」
> 指 dense + sparse + **visual**。视觉腿读侧由独立服务承担。

### 5.3 跨知识库搜索

```http
POST /api/v1/search
{"query":"...","kb_ids":null,"mode":"hybrid","top_k":20,"page":1,"include_debug":false}
```

`kb_ids=null` 表示"该用户有权限的所有 KB"；`kb_ids=[...]` 是**子集**：实际范围 = `readable_kb_ids ∩ kb_ids`。
> ⚠️ 契约早期版本写的 `workspace_id` **实现里不存在**。两条边界：**`kb_ids` 传空数组 `[]` 返回 400**；
> **求交后为空时返回空结果而不是报错**—— 报错会泄漏"这些 kb_id 里至少有一个存在"。

### 5.4 检索调试面板

```http
POST /api/v1/kb/{kb_id}/search/debug
{"query":"...","top_k":10}
→ {"query":"验收标准那一栏怎么填","mode":"hybrid",
   "legs":{"dense":{"n":20,"ms":88,"skipped":null,"items":[{"key":"text:123:3","doc_id":345,"chunk_index":3,
                                                          "rank":2,"score":0.032,"file_name":"…","text":"…（≤160 字）"}]},
           "sparse":{"n":20,"ms":30,"skipped":null,"items":[…]},
           "trgm":{"n":3,"ms":45,"skipped":null,"items":[…]},
           "visual":{"n":0,"ms":0,"skipped":"视觉腿在本进程不可用：colSmol 未启用。","items":[]}},
   "rrf":[{"key":"text:123:3","doc_id":345,"chunk_index":3,"score":0.0325,"ranks":{"dense":2,"sparse":1},
           "leg_scores":{…},"legs":["dense","sparse"]}],
   "elapsed":{"dense":88,"sparse":30,"trgm":45,"total_ms":312},"filtered_out_deleted":0}
```

**四条必须照抄的形状**：① `legs[].skipped` 是**字符串或 null**，不是布尔（前端要显示这个原因）；
② `elapsed` 的键就是腿名（`dense`/`sparse`/`trgm`），没有 `_ms` 后缀；
③ `rrf[].key` 带命名空间（`text:<id>`/`visual:<id>`，不加前缀 RRF 会互相抵消）；
④ `filtered_out_deleted` 是**软删除二次过滤**丢掉的数量。

> 🔑 **起过程数据也可从普通检索拿到**（`include_debug=true`）；本端点保留给只需要过程的调用方。⚠️ 本端点**只支持单库**。

---

## 6. 时间线与个人化视图

```http
GET /api/v1/kb/{kb_id}/timeline?month=2025-02          → 月视图
GET /api/v1/kb/{kb_id}/timeline?date=2025-02-14          → 当天明细
GET /api/v1/kb/{kb_id}/timeline/anniversaries?limit=20    → 纪念日（有照片的日子）
GET /api/v1/kb/{kb_id}/timeline/milestones?limit=20       → 里程碑
GET /api/v1/kb/{kb_id}/photos?page=1&date=2025-01-01       → 照片墙（photo KB）
```

> **这些端点仅在 `source_kind` 为 `chat` 或 `photo` 时有内容**，其它 KB 返回 400（的实现方式）。

## 7. 会话管理（多轮 · 多库）

```http
GET    /api/v1/sessions?kb_id=&page=1      → 我的全部会话
POST   /api/v1/sessions                     {"title":"…","kb_ids":[1,2]}
GET    /api/v1/kb/{kb_id}/sessions?page=1
POST   /api/v1/kb/{kb_id}/sessions        {"title":"第一次约会"}
GET    /api/v1/sessions/{session_id}                → 含消息历史
PATCH  /api/v1/sessions/{session_id}       {"title":"新标题"}
DELETE /api/v1/sessions/{session_id}
```

> 🔑 ：会话新增 `kb_ids`（检索范围，`null` = 全部有权限的库），`kb_id` 转为**可空**。
> 存量会话迁移时回填 `kb_ids=[kb_id]`。`GET /sessions` 是**用户级**列表（可选 `?kb_id=` 过滤）。

## 8. 聊天数据导入

```http
POST /api/v1/kb/import/chat
{"kb_id": 88, "source_path": "/path/to/chat_messages_clean.jsonl", "messages_per_doc": 50}
→ 202 {"job_id": 501, "estimated_docs": 1740}

GET /api/v1/jobs/{id}
GET /api/v1/jobs?kb_id=88&status=running&kind=ingest&doc_id=501&page=1&page_size=20
→ {"items":[{"id":501,"kind":"import_chat","status":"running","progress":0.42,"attempts":1,"doc_id":null,
             "kb_id":88,"error":null,"payload":{"processed_msgs":12000,"total_msgs":48000,"created_docs":240},
             "created_at":"…","updated_at":"…"}],"total":1,"page":1,"page_size":20}
```

> ⚠️ 早期版本把自定义数据写作 `detail`，**实现里是 `payload`**（`detail` 是审计日志的字段名）。

- 服务端读文件（不走 HTTP 上传 87k 条 JSON）；每 `messages_per_doc` 条消息切一个文档（= 50），`doc_source=chat_import`。
- **导入时重跑归属判定**：旧审计日志的判定被后续修复改动过 3 次，不能信任旧结果。
- 🔑 **归属判定只记录、不拦截**：聊天必然出现对话方的名字，拿它当门禁会把整个聊天库导空 ⇒
  `payload.owner_rule_applied=false`，真正生效的隔离手段是**库级**的（`source_kind=chat` + `visibility=private`）。
- 🔑 **`source_path` 必须在 `IMPORT_ROOTS` 白名单内**，且**先 `resolve()` 再判前缀**（用 `is_relative_to`）。
  越界返回 400 并**列出允许的目录**。可用 `GET /api/v1/kb/import/roots` 查白名单。
- 🔑 **目标库的 `source_kind` 必须匹配**（chat 端点要 `chat`、dir 端点要 `photo`）。
- ⚠️ 导入**不自己跑摄取**：只建 `Document` 行 + blob + `KIND_INGEST` 任务（导入结束时会有上千个 pending 的 ingest 任务）。

## 9. 图片 KB（/）

```http
# 方式1：单张上传（走统一文档上传端点）
POST /api/v1/kb/{kb_id}/documents      files=@photo.jpg
# 方式2：从服务器目录批量导入
POST /api/v1/kb/{kb_id}/import/dir
{"path":"/data/imports/photos_original","recursive":true}
→ 202 {"job_id":502,"estimated_files":23,"recursive":true}

GET /api/v1/kb/import/roots
→ 200 {"roots":["/…/data/imports"],"env":"IMPORT_ROOTS"}
```

- 目录必须在 `IMPORT_ROOTS` 白名单内；目标库 `source_kind` 必须是 `photo`；文档 `doc_source=photo_dir`。
- `payload` 计数器：`{"processed_files":…,"total_files":…,"created_docs":…,"skipped":…}`；**重复导入幂等**（内容 sha 相同则跳过）。
- ⚠️ 排序按文件名（`sorted`）：不排序的话两次导入的 doc 顺序与编号不同。
- ⚠️ 扫描有 20000 个文件的上限（没有上限时一个指向 `/usr` 的路径会让 worker 遍历几十万文件直到超时）。

## 10. MCP Server

传输：**Streamable HTTP**（`POST /mcp`），鉴权复用 `Authorization: Bearer <api_key>`。

```http
POST /mcp        → MCP Streamable HTTP（；无状态，不实现 Mcp-Session-Id）
GET  /mcp        → 能力自述（不属于协议，给人排查用）
```

### 10.1 暴露的工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_kb` | — | 列出调用者有权限的 KB（**按权限动态裁剪**） |
| `search_kb` | `kb_id`, `query`, `top_k`, `mode` | 三路 RRF 检索 |
| `ask_kb` | `kb_id`, `question`, `top_k`, `session_id` | RAG 问答（非流式） |
| `get_document` | `kb_id`, `doc_id`, `limit` | 文档元信息 + 前 N 个 chunk |

**方法**：`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`。未知方法回 `-32601`。

⚠️ **传输层是"无状态"的**（不实现 `Mcp-Session-Id`）。⚠️ **协议错误也回 HTTP 200**（错误在 body 的 `error` 里），
只有"根本不是 JSON-RPC"才回 400。⚠️ **工具失败用 `result.isError=true`**（业务错误，模型读得懂），而不是 JSON-RPC `error`。
⚠️ 批量请求（JSON 数组）也支持；全是通知时回 **204**。

```jsonc
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"search_kb","arguments":{"kb_id":88,"query":"验收标准那一栏怎么填","top_k":5}}}
→ 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\n  \"items\": [...]\n}"}],"isError":false}}
```

⚠️ 结果**一律是 JSON 字符串塞进 `content[0].text`**：MCP 客户端**必须**支持 `text` 内容类型，自造类型会被当"未知类型"丢掉。

### 10.2 工具列表的权限裁剪

`调用方 token → api_keys.user_id → 有权限的 kb_ids → list_tools 只返回这些 kb 对应的工具实例`。
> 不能"先返回全部工具、调用时再拒绝" —— 那会让调用方探测到不该看到的 KB 存在。

### 10.2b 错误码

| 情况 | JSON-RPC code | 落在哪 |
|---|---|---|
| 未知 `method` / 未知工具 | `-32601` | 顶层 `error` |
| **参数不合法**（缺必填 / 类型错 / 越界 / 未知参数 / enum 不符）| **`-32602` Invalid params** | 顶层 `error`，message 形如 `search_kb: 缺少必填参数 kb_id` |
| 库不存在 / 无权限 | —（不是协议错）| `result.isError = true` + `{"error":"KB_NOT_FOUND"}` |
| 其它未预期异常 | `-32603` Internal error | 顶层 `error` |

⚠️ **界线**：参数不合法是**协议错**；请求合法但结果失败是**业务结果**（`isError: true`）。⚠️ **越界报错而不是夹取**：
MCP 的调用方多半是个 LLM，悄悄夹取会让它拿着 50 条当 80 条用。⚠️ 消息里**不出现** `KeyError`/`ValueError` 这类裸异常类型名。

### 10.3 审计

每次调用记入 `audit_log`：`api_key_id` → `user_id` → `kb_ids[]` → `query` 摘要 → 耗时。

## 11. A2A Agent

> 🔑 **读响应先看 Agent Card**：`GET /.well-known/agent-card.json` 的每个 skill 带 `inputSchema`/`outputSchema`。
> 正文在 **`artifacts[].parts[].text`**（**没有**顶层 `answer`），引用来源在**顶层 `metadata.sources`**，
> 引用角标在 `artifacts[].parts[].metadata.citations`。⚠️ `kb_id` **必填**且 Card **刻意不列出**可用值（那是权限信息）。

与 MCP **共用同一套检索/问答内核**，对外按用途分 Agent。

```http
GET  /.well-known/agent-card.json     → Agent Card（能力声明）
POST /a2a/tasks                       → 委派任务
```

| Agent | skill | 用途 | 实现 |
|---|---|---|---|
| 问答 Agent | `answer` | 单轮查资料 | `ask.prepare` + `stream_answer(one_shot=True)` |
| 研究 Agent | `research` | 多轮检索 + 汇总 | 同一内核，`top_k` 放宽到 30，**按文档分组**逐份给摘录 |

⚠️ `research` **不是**"让 LLM 规划多轮检索"（默认生成层是抽取式，这是隐私红线）；它是**可解释的多角度汇总**，
返回里带 `extractive: true`。**`POST /a2a/tasks` 收两种入参**（都走同一个内部函数）：

```jsonc
// ① 协议面：JSON-RPC tasks/send
{"jsonrpc":"2.0","id":1,"method":"tasks/send","params":{"skill":"answer","kb_id":88,
  "message":{"parts":[{"kind":"text","text":"验收标准那一栏怎么填"}]}}}
// ② 便捷面：curl / 手测
{"skill":"answer","kb_id":88,"query":"验收标准那一栏怎么填"}
```

响应是 A2A Task 对象：`{id, contextId, status:{state:"completed"}, history:[…], artifacts:[{parts:[…]}],
metadata:{kb_id, skill, sources, documents}}`。⚠️ `capabilities.streaming=false` 是**如实**声明。
⚠️ Agent Card **不含任何库的名字**（只有 `readableKbCount`）。⚠️ 无权限与不存在**同码**（403）。
**审计**：MCP 的 `tools/call` 与 A2A 的每个任务都记 `audit_log`。

## 11.5 文件存储：只有本地盘，且这是**有意**的（顺带核实）

**上传的文件落在两处，各存一半**（「双份存储」—— 归一化会让字符偏移失真，所以必须留原文）：

| 存哪 | 是什么 | 怎么算位置 |
|---|---|---|
| `data/blobs/<sha[:2]>/<完整 sha>/original` | **原始文件**（内容寻址）| `common/storage.py::blob_path(sha)` |
| 同上目录的 `preview.pdf` / `thumbs/N.jpg` | Office 预览/ 缩略图 | `preview_path` / `thumbs_dir` |
| PG 的 `chunks` 表 | **归一化文本 + 切分**（`content_norm`、`locations` L3 坐标）| 检索命中的是这一份 |

- **文件名就是内容的 sha256** ⇒ 同一份文件传两次只存一份；写入是"先写 `.tmp` 再原子改名"。
- `blobs` 表只存元信息；**`ref_count` 是删除的安全阀**（引用数不为 0 时不许物理删）。
- ⚠️ **`storage_path` 这一列不是存储抽象**：写入时是 `str(blob_path(sha256))`，**没有任何代码读它做分发**。
- **备份**（`deploy/backup.sh`）：PG（`pg_dump -Fc`）+ `data/blobs/` 一起备；**Qdrant 不备**，但 manifest 里记点数。
  🔑 触发条件（要做对象存储的那天）：**要上第二台机器 / 备份要出本机** —— 到那时先做**只读的远端副本**，不动在线读写路径。

## 12. 运维端点

```http
GET  /                          → **两态**（免鉴权）：有 `web/dist` 时返回**界面**（深链回 `index.html`）；
                                  没有构建产物时返回 `{service, version, docs, api}`
GET  /health                     → 免鉴权存活探针 {status,db,qdrant,worker,models,queue,available_legs}
GET  /api/v1/health              → 同一份 HealthResp，走 Bearer（models/queue/available_legs 才有值）
GET  /api/v1/jobs                → 任务列表（分页包）?kb_id=&doc_id=&kind=&status=&page=&page_size=
GET  /api/v1/jobs/{id}           → 单个任务
POST /api/v1/jobs/{id}/retry     → ❌ **明确不做**（不是待办）—— 要重试文档摄取用 POST /documents/{doc_id}/retry
GET  /api/v1/audit?workspace_id=&user_id=&action=&date_from=&date_to=
GET  /api/v1/workspaces/{id}/stats             → 工作区统计（KB/文档/chunk/磁盘）
POST /api/v1/admin/reindex?kb_id=             → 入队 KIND_REINDEX（不阻塞）
GET  /api/v1/admin/consistency?kb_id=         → PG vs Qdrant 对账明细
GET  /api/v1/admin/llm                         → 读外部 LLM 配置（API Key 只回掩码）
PUT  /api/v1/admin/llm                         → 写外部 LLM 配置（页面可配）
POST /api/v1/admin/llm/test                    → 用当前配置试发一次请求
GET  /api/v1/admin/users                        → 列出全部用户（超管）
POST /api/v1/admin/users                        → 建号（超管；自助注册关闭时的入口）
PATCH  /api/v1/admin/users/{id}                  → 改角色/状态/昵称（超管）
```

> **⚠️ 三个"框架自带"路径不进本契约表**：`/docs`（Swagger UI）、`/redoc`、`/openapi.json`。
> 它们由 FastAPI 提供、**不是本服务的业务端点**，受 `EXPOSE_DOCS` 开关控制（关掉后三条路由**根本不存在**）。
> 写进契约表会永久多出三条"声明了但未实现"。

> **`available_legs` 是本轮新增的**：回答"**这个部署实际能跑哪几条检索腿**"：`["dense","sparse","trgm"]` 恒在；
> `"visual"` 在视觉查询服务可达且模型就绪时才有。⚠️ **它不会硬编码**；⚠️ 只在鉴权版里有值（部署细节）；
> 🔑 与 `kb_visual` 有没有点**无关**（那是数据，这是能力）。

> **`queue` 是本轮新增的**：`queue = {pending, running, failed_24h, oldest_running_seconds, stuck_after_seconds}`。
> ⚠️ 它**不是心跳**；`oldest_running_seconds` 没有 running 时是 `null` 而不是 `0`；`stuck_after_seconds = 1800` 与巡检一致；
> ⚠️ **只在鉴权版里有值**；查不到时 `queue = {"error": "…"}`，**健康检查仍然 200**。

**外部 LLM 配置要点**：

- 🔑 **页面可配、立即生效**：配置落库（`llm_settings` 全局单例），生成层与 Contextual 切分都读它；env 只兜底。
- 🔴 **API Key 明文只进不出**：`GET`/`PUT` 的响应一律只回掩码（`has_key` + `api_key_masked`）。
- 🔑 **写时 `api_key` 不传 = 保持原值**（不是清空）；要清空用 `clear_api_key=true`。
- 🔑 **`external_active`**：`provider=openai_compat` **且** `enabled=true` **且** base_url/model/api_key 齐全才为真。
- 🔑 **细粒度功能开关**：`use_for_ask` 与 `use_for_contextual` **分开控制**。
- 🔑 **自定义请求头**（`extra_headers`）：接入要求特殊头的网关不用改代码。
- 🔒 **写端点要工作区 owner**（`base_url` 能指向任意主机 ⇒ 任何登录用户都能改的话会把所有人的检索原文导向自己的服务器）；读端点任意登录用户可读。

**超级管理员与用户管理（/）要点**：

- 🔑 **全局角色** `users.role`（`user`|`superadmin`），与工作区的 owner/member **不是一回事**。角色与 API Key 一样**服务端从用户行推导**。
- 🔑 **唯一昵称** `users.nickname`：`POST /auth/login` 的 `email` 字段可填**邮箱或昵称**。
- 🔒 `GET/POST/PATCH /admin/users` **仅超管**（非超管 403 `FORBIDDEN`）。
- 🔑 超管能改**任意用户**的 `display_name`/`nickname`/`email`/`role`/`is_active`，并能**重置密码**。
- 🔑 **任何用户**用 `PATCH /auth/me` 改**自己**的显示名/昵称/邮箱/密码；改 `email` 或密码**必须**带 `current_password`。
- 🔒 防自锁：**不能降级/停用最后一个超级管理员**，**不能停用自己**。

**`GET /audit` 要点**：🔑 **只能查自己有权管理的工作区**（审计里有 query 摘要 —— 那是用户的数据）。
`workspace_id` **必填**。无权限与不存在**同码**。`action` **先校验再查**（拼错值静默返回空列表 = 骗人）。

**哪些端点写审计**：

| `action` | 端点 |
|---|---|
| `search` | `POST /kb/{id}/search` · `POST /search` · `POST /kb/{id}/search/debug` · `GET /kb/{id}/ask/preview`（补）|
| `ask` | `POST /kb/{id}/ask`（SSE）|
| `upload` | `POST /kb/{id}/documents` · `import/chat` · `import/dir` |
| `mcp_call` | `POST /mcp`（每次 `tools/call`）|
| `a2a_call` | `POST /a2a/tasks` |

- 🔑 `ask_preview` 记 `search` **不记 `ask`**（它没有生成任何东西）；另两条都带 `detail.surface`。
- 🔑 ：`ask` 的 `detail` 里另记 `retrieval_query`（**真正送去检索的**）与 `query_expanded`、`history_turns`。
- ⚠️ **不记审计的读端点**（刻意）：`/documents/{id}/chunks`、`/pages/{n}`、`/preview`、`/thumbs/{n}`、`/pii`。
- 🔑 新增检索端点时**必须**接上：`tests/test_audit_coverage.py` 是接线守卫。

**`GET /admin/consistency` 要点**（索引对账）：

```jsonc
{"kb_ids":[1],"pg_chunks":74,"qdrant_points":148,
 "legs":{"kb_dense":{"points":74,"missing":[],"dangling":[],"missing_count":0,"dangling_count":0,"consistent":true},
         "kb_sparse":{…},"kb_visual":{"points":3,…}},
 "missing_in_qdrant":[],"dangling_in_pg":[],"missing_count":0,"dangling_count":0,"consistent":true}
```

🔑 **`legs` 分腿报，不要只看顶层**（顶层是 dense ∪ sparse 的并集口径，"只有一条腿坏了"在顶层看起来是**一致**的）。
🔑 **起是三条腿**（多了 `kb_visual`，键用 `point_id_for_page`，PG 侧真相来源是 `documents.scan_pages`）。
⚠️ 视觉腿的 `missing` **不能**靠 `KIND_REINDEX` 修（要重跑 `POST /documents/{id}/reparse`）。

| 报告项 | 含义 | 该做什么 |
|---|---|---|
| `legs.*.missing` | PG 有、这条腿没有 | 漏索引 → 文本腿 `KIND_REINDEX` **能**修；视觉腿要 `reparse` |
| `legs.*.dangling` | 这条腿有、PG 没有 | 残留点 → reindex **修不了**，要重跑摄取或软删 |

⚠️ 两个列表都截断到 200 条；⚠️ 对账**两侧都用同一个函数**算键；⚠️ `POST /admin/reindex` **默认范围是"我有权访问的库"**；
⚠️ 重建**只处理两条文本腿**；⚠️ BM25 词表 `terms.df`/`corpus_stats` 是**全局量**（重建任一库时都会为全体活 chunk 重新登记）。

**阶段 3：查询类型路由与 Reranker**（无新端点，行为层）：

| 能力 | 怎么开 | 关掉时的行为 |
|---|---|---|
| 查询类型路由 | **默认开**，但只在 `confidence ≥ 0.35` 时改权重/过滤 | 等权 RRF + 不过滤 = 与引入前**完全一致** |
| Reranker | **per-KB**：`kb.rerank_model` 非空 | `skipped` + 保持原顺序 |

`GET /kb/{id}/search/debug` 额外返回两段：

```jsonc
"route": {"intent": "photo", "weights": {"visual": 3.0}, "prefer_kinds": null,
          "prefer_source_kinds": ["photo"], "date_hint": null, "confidence": 0.9,
          "why": "识别为「找照片」：优先照片库", "note": "库里没有 ['photo'] 类型的库，不做过滤"},
"rerank": {"applied": false, "model": "BAAI/bge-reranker-v2-m3", "ms": 0,
           "skipped": "已通过 RERANK_MODEL=\"\" 显式禁用", "scores": {}}
```

⚠️ **`rerank_model` 只从库里读，不接受请求参数**（排序是**库的属性**）；**`rerank.scores` 是未归一化的 logits**，
不能当"置信度"展示；**跨库检索一律不启用精排**。⚠️ 路由的偏好过滤是**软**的（过滤后为空会自动退回全部，并在 `note` 里说明）。
**默认 Reranker 模型是 `BAAI/bge-reranker-v2-m3`**（：base 成对判别 2/4、v2-m3 4/4）。

**`GET /workspaces/{id}/stats` 要点**：给**两个**字节数 —— `sum_doc_size_bytes`（各库文档 `size_bytes` 之和，**只有原始文件**）
与 `blob_disk_bytes`（blob **目录**实际占盘 = 原始 + 派生文件 `preview.pdf`/`thumbs/*.jpg`，所以通常**更大**）。

**`/jobs` 的租户口径**：能看到的任务 = `jobs.kb_id ∈ ctx.readable_kb_ids`，**同源**于 `auth.load_user_context`。
`kb_id IS NULL` 的任务（`cleanup`/`reindex`）**自动看不见**（`IN (…)` 对 NULL 恒不匹配）；无权限的 `job_id` 与不存在的返回**同一个错误码**。

## 13. 已记录的补充决策

| # | 决策 | 来源 |
|---|---|---|
| | 聊天按 50 条消息一个文档（引用更精确）| 本轮 |
| | 工作区软删除，沿用 30 天机制 | 本轮 |
| | 全局公共 KB 所有用户可读**可编辑**，预留 RBAC | 本轮 |
| | 非当前文档的向量点及时清理（取代 「3 天」的说法）| 本轮 |
| | 批量上传单请求 20 个文件 | 本轮 |
| | 问答首 token < 2 秒（检索段 < 500ms）| 本轮 |
| | 聊天导入用专用端点（服务端读文件）| 本轮 |
| | 图片 KB 支持上传 + 服务器目录导入两种方式 | 本轮 |
