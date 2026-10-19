/**
 * 由 `docs/DESIGN-API.md` 契约手抄的类型。
 *
 * ⚠️ 手抄而不是生成：契约没有 OpenAPI 导出步骤，而这一层只需要
 * 前端真正用到的字段。改契约时**以契约为准**同步这里。
 *
 * ⚠️ 两条容易抄错的地方：
 *
 * 1. `SearchHit.kind` / SSE `sources[].kind` 是**引用类型**
 *    （`text` / `page` / `image`），**不是** `chunks.kind`
 *    （`text` / `table` / `page` / `chat`，D58）。
 *    表格 chunk 在 `/search` 里是 `kind="text"` + 40 个带 row/col 的框。
 *    → 画框必须按 `locations` 的形状分派，见 `lib/boxes.ts`。
 * 2. `locations` 是 `list[dict] | null`，**没有静态形状**。
 *    `bbox` 是归一化的 `[x0, y0, x1, y1]`（0~1），表格单元格另有
 *    `row` / `col`（D58）。所以这里用 `Location` 收窄它。
 */

/** 契约的引用类型（展示用）。 */
export type CitationKind = "text" | "page" | "image";

/** `chunks.kind`（仅 `GET /documents/{id}/chunks` 会返回）。 */
export type ChunkKind = "text" | "table" | "page" | "chat";

export type Visibility = "private" | "workspace" | "global";
export type SourceKind = "file" | "chat" | "photo";
export type SearchMode = "semantic" | "exact" | "hybrid";
/**
 * D164 切分策略（`KbCreateReq` / `KbPatch` / `KbOut` 都有这个字段）。
 *
 * ⚠️ 取值必须与 `services/ingest/strategies.STRATEGY_NAMES` 一致
 * （后端那边由守卫测试钉住两边相等；这边是手抄投影，改契约时同步）。
 */
export type ChunkStrategy =
  | "fixed"
  | "recursive"
  | "semantic"
  | "parent_child"
  | "late"
  | "contextual";
export type DocStatus =
  | "pending"
  | "parsing"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed"
  | "deleted";

export interface User {
  id: number;
  email: string;
  display_name: string;
  /** D174 唯一昵称（可作登录名）；存量用户可能为 null。 */
  nickname?: string | null;
  /** D173 全局角色：`user` | `superadmin`。 */
  role?: string;
  created_at: string;
}

export interface LoginResp {
  user: User;
  api_key: string;
  key_prefix: string;
}

export interface Workspace {
  id: number;
  name: string;
  description: string | null;
  owner_id: number;
  created_at: string;
  /** D129：软删时间。**只有回收站列表里非空**，其它响应恒为 null。 */
  deleted_at?: string | null;
}

/**
 * `POST /workspaces` 的请求（契约 §2）。
 *
 * 🔑 服务端把**创建者自动加成 `owner` 成员**（少了这一步，
 * `assert_workspace_access` 会因为"不是成员"把创建者自己拒之门外）——
 * 所以前端建完工作区就能立刻往里建库，不需要再"加入"一次。
 *
 * ⚠️ 工作区**名字不唯一**（DB 上没有唯一约束，实测库里就有多个同名工作区）
 * ⇒ 名字重复不是错误，别在前端拦。
 */
export interface CreateWorkspaceReq {
  name: string;
  description?: string | null;
}

/**
 * `GET /workspaces/{id}/stats`（契约 §9 · **只有 owner/admin 能看**）。
 *
 * ⚠️ **两个字节数量的是不同的东西**（契约里专门解释过，缺一个都会误导人去清盘）：
 * - `sum_doc_size_bytes`：各库文档 `size_bytes` 之和 —— **只有原始文件**
 * - `blob_disk_bytes`：这些 blob **目录**的实际占盘 = 原始 + 派生物
 *   （`preview.pdf`、`thumbs/*.jpg`），所以通常**更大**（实测 318KB → 722KB）
 *
 * 🔑 它**包含工作区里所有库**（包括别人的 `private`）：磁盘账要全 ——
 * 看不到的文档**仍然占着盘**。用可见性去过滤会让"删哪个库能腾空间"算错。
 */
export interface WorkspaceStats {
  workspace_id: number;
  kb_count: number;
  doc_count: number;
  chunk_count: number;
  sum_doc_size_bytes: number;
  exclusive_blob_count: number;
  blob_disk_bytes: number;
}

export interface Kb {
  id: number;
  workspace_id: number;
  name: string;
  description: string | null;
  visibility: Visibility;
  source_kind: SourceKind;
  embed_model: string;
  chunk_size: number;
  chunk_overlap: number;
  /**
   * D164 切分策略（`KbOut.chunk_strategy`，后端默认 `recursive`）。
   *
   * ⚠️ 这里写成**可选**是为了不动"冻结的无头核心"：`src/lib/kbScope.test.ts`
   * 与 `src/lib/workspace.test.ts` 里的 `Kb` 夹具（D189 前写的）没有这个字段，
   * 改成必填会让那两个文件编译不过 —— 而它们的**内容不改**是 D188 的硬约束。
   * 运行时后端**总是**返回它（有默认值）⇒ 取值一律走
   * `features/knowledge/strategy.ts::strategyOf(kb)`，默认值只在那里落一次。
   */
  chunk_strategy?: ChunkStrategy;
  generate_image_desc: boolean;
  /** D121：运行时 PII 掩码开关（🔴 默认 false）。只作用于之后摄取的文档。 */
  pii_mask: boolean;
  /**
   * 阶段 3 per-KB 精排（D87）。
   *
   * 🔑 `null` / `""` 都表示**不启用**（D90），**不是**"用全局默认" ——
   * 每个库都跑精排的话，排序会在用户没要求的情况下改变，而界面上看不出来。
   * ⚠️ 之前这个字段后端**根本不返回**（只能改数据库），所以这里曾经没有它。
   */
  rerank_model: string | null;
  created_by: number;
  created_at: string;
  /**
   * D129：软删时间。**只有回收站列表里非空**。
   *
   * 🔑 前端靠它算"还能恢复几天"（`lib/trash.ts::daysLeft`，与文档回收站同口径）。
   * 之前 `KbOut` 里根本没有这个字段 —— 于是界面上无法告诉用户
   * "删掉的东西还有 29 天可以被要回来"，D26 的反悔窗口等于看不见。
   */
  deleted_at?: string | null;
  updated_at: string;
}

/**
 * PII 检出报告（D121 · 契约 §3.1）。
 *
 * ⚠️ `source_*` 与 `indexed_*` 是**两套数字**：
 * 开了掩码就是 `source_total=7 / indexed_total=0`。
 * 合成一个会让用户以为掩码没生效（D121）。
 */
export interface PiiReport {
  kb_id: number;
  masking: "on" | "off";
  source_total: number;
  source_by_type: Record<string, number>;
  indexed_total: number;
  indexed_by_type: Record<string, number>;
  doc_count: number;
  docs_with_pii: number;
  /** ⚠️ `pii_hits IS NULL` 的文档数（还没摄取完）—— **不等于「干净」** */
  docs_not_scanned: number;
  /**
   * 🔴 已摄取但**没有文本层**的文档数（照片 / 纯扫描件，`chunk_count = 0`）。
   *   它们贡献 0，而那个 0 **不代表图片里没有敏感信息**（OCR 不在检测范围内）。
   */
  docs_no_text: number;
  docs: {
    doc_id: number;
    file_name: string;
    status: string;
    total: number;
    by_type: Record<string, number>;
  }[];
  docs_truncated: boolean;
  scan_chars: number;
  scan_truncated: boolean;
}

export interface KbDetail extends Kb {
  doc_count: number;
  chunk_count: number;
  size_bytes: number;
  /** D181：还停在**旧切分策略**的当前文档数（0 = 干净）。改策略只对之后摄取生效 ⇒ 这是"重摄取欠账"。 */
  stale_strategy_docs: number;
}

export interface DocumentRow {
  id: number;
  kb_id: number;
  file_name: string;
  source_path: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  status: DocStatus;
  status_msg: string | null;
  is_current: boolean;
  /** D192：同一 (kb_id, file_name) 的版本号（从 1 起，覆盖式更新 +1）。可选，避免动既有夹具。 */
  version_id?: number;
  /** 软删时间（D26）。普通列表里为 null；回收站里用它算"还能恢复几天"。 */
  deleted_at: string | null;
  page_count: number | null;
  chunk_count: number;
  is_scanned: boolean;
  uploaded_by: number;
  created_at: string;
  updated_at: string;
}

export interface UploadResp {
  batch_id: string;
  documents: DocumentRow[];
}

/**
 * `jobs.status`（`ck_job_status`）。
 *
 * ⚠️ 与 `DocStatus` **不是一套**：文档状态是"这件事走到哪一步了"，
 * 任务状态是"worker 领了没有"。两者短暂不一致是**正常的**
 * —— `finish_job` 在 handler 返回之后才跑（D14 允许）。
 */
export type JobStatus = "pending" | "running" | "done" | "failed" | "cancelled";

/** `jobs.kind`（`ck_job_kind`，7 个值）。 */
export type JobKind =
  | "ingest"
  | "embed_visual"
  | "thumbnail"
  | "pdf_preview"
  | "cleanup"
  | "import_chat"
  | "reindex";

/** `GET /jobs` / `GET /jobs/{id}` 的一条（契约 §12）。 */
export interface Job {
  id: number;
  kind: JobKind;
  doc_id: number | null;
  kb_id: number | null;
  status: JobStatus;
  /** 0~1。**这是唯一的百分比来源**，`DocumentOut` 里没有这个字段。 */
  progress: number;
  attempts: number;
  error: string | null;
  /** ⚠️ 叫 `payload` 不是 `detail`（后者是审计日志的字段名）。 */
  payload: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** 分页包（契约 §0.4）。列表类端点大多还返回裸数组，`/jobs` 是新的所以是包。 */
// ---- D20 时间线与照片墙（契约 §6）----
/**
 * 🔑 `MonthDayOut.photo_count` 是**数量**，`TimelineDayOut.photos` 是**列表**。
 *
 * 两个形状同名过一次（`photos` 一会儿是 int 一会儿是 list），
 * 结果月视图的响应在 pydantic 校验时 500，而错误信息完全指不到
 * "这是设计错了"。所以这里分成两个类型，别合并。
 */
export interface MonthDay {
  date: string;
  total: number;
  photo_count: number;
  highlights: Array<Record<string, unknown>>;
}

/** `GET /kb/{id}/timeline?month=YYYY-MM` */
export interface TimelineMonth {
  kb_id: number;
  month: string;
  total: number;
  active_days: number;
  photo_count: number;
  speakers: Record<string, number>;
  /**
   * ⚠️ 是**子集**（有照片 / 消息量 ≥ 该月中位数），不是全部日期。
   * 所以"某天不在 days 里"≠"那天没记录"，前端不能这么显示。
   */
  days: MonthDay[];
}

/** `GET /kb/{id}/timeline?date=YYYY-MM-DD` */
export interface TimelineDay {
  date: string;
  total: number;
  types: Record<string, number>;
  speakers: Record<string, number>;
  highlights: Array<Record<string, unknown>>;
  photos: PhotoRow[];
  docs: number;
}

/** 纪念日 = **有照片的日子**（契约原话，不是"消息最多的一天"）。 */
export interface Anniversary {
  date: string;
  count: number;
  photos: string[];
  chat_total: number;
}

/** 里程碑 = 按消息量降序 top-N（**不设阈值**魔法数）。 */
export interface Milestone {
  date: string;
  chat_total: number;
  photos: number;
  note: string;
}

/**
 * 照片墙的一行。
 *
 * ⚠️ `date` 可空：没有 EXIF 也抽不到文件名日期时后端**不猜**，
 * 这张照片挂不上时间线，但仍要在照片墙里列出来（排在最后）——
 * 过滤掉等于让用户以为照片丢了。
 */
export interface PhotoRow {
  doc_id: number;
  file_name: string;
  date: string | null;
  size_bytes: number | null;
  mime_type: string | null;
  status: string;
}

/** `/kb/{id}/timeline` 的两种返回（`?month=` 与 `?date=`）。 */
export type TimelineResp = TimelineMonth | TimelineDay;

/** 靠 `month` 字段区分两种形态（`TimelineDay` 没有 `month`）。 */
export function isMonthView(r: TimelineResp): r is TimelineMonth {
  return typeof (r as TimelineMonth).month === "string";
}

// ---- 检索调试（契约 §12 · D15）----
/**
 * 🔑 `skipped` 是**字符串原因**，不是布尔。
 *
 * 视觉腿在 API 进程恒为 skipped（编码只在 worker 加载，D50）——
 * 返回 `false` 或省略的话，界面只能显示"这条腿没结果"，
 * 而真相是"它压根没跑"。用户会以为自己的照片没被索引。
 */
export interface DebugLegItem {
  key: string;
  doc_id: number;
  chunk_index: number;
  rank: number;
  score: number;
  file_name: string | null;
  text: string;
}

export interface DebugLeg {
  n: number;
  ms: number;
  /** null = 正常参与；非 null = 为什么被跳过（如"视觉编码只在 worker 进程加载"） */
  skipped: string | null;
  items: DebugLegItem[];
}

/** 每路排第几名 —— 调试面板最想看的就是这个 */
export type LegName = "dense" | "sparse" | "trgm" | "visual";

export interface DebugRoute {
  intent: string;
  weights: Record<string, number>;
  why: string;
}

export interface DebugRerank {
  model: string | null;
  applied: boolean;
  skipped: string | null;
}

export interface DebugRrfRow {
  key: string;
  doc_id: number;
  chunk_index: number;
  score: number;
  ranks: Partial<Record<LegName, number>>;
  leg_scores: Partial<Record<LegName, number>>;
  legs: LegName[];
}

export interface DebugResp {
  query: string;
  mode: SearchMode;
  route: DebugRoute | null;
  rerank: DebugRerank | null;
  legs: Partial<Record<LegName, DebugLeg>>;
  rrf: DebugRrfRow[];
  elapsed: Record<string, number>;
  /** 被"软删过滤"掉的命中数（D7） */
  filtered_out_deleted: number;
}

// ---- 会话（契约 §7）----
/**
 * ⚠️ **没有 `message_count`**：`SessionOut`（契约 §7 + `schemas.py`）只有
 * id/kb_id/title/created_at/updated_at。第一版我脑补了 message_count，
 * 于是界面显示「undefined 条消息」—— 编出来的数字比没有更糟。
 *
 * → 列表只显示「标题 + 时间」；要看条数点「查看」（详情端点才有 messages）。
 */
export interface SessionRow {
  id: number;
  kb_id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionMessage {
  role: string;
  content: string;
  citations?: number[];
  created_at?: string;
}

export interface SessionDetail {
  id: number;
  kb_id: number;
  title: string | null;
  messages: SessionMessage[];
}

// ---- API Key（契约 §1 · D64：明文只出现一次）----
/**
 * ⚠️ 字段名以**后端/契约**为准：`name`（不是 label）、`scopes`、
 *   `last_used_at`，**没有 `revoked_at`** ——
 *   吊销是把 ApiKey 行**删掉**（`auth.py::revoke_key`），
 *   所以"已吊销"的 key 不会出现在列表里，UI 不需要那个状态。
 */
export interface ApiKeyRow {
  id: number;
  name: string;
  key_prefix: string;
  scopes: string[];
  last_used_at: string | null;
  created_at: string;
}

/** 建 Key 的响应。🔑 `api_key` **只在这一次**出现，之后再也查不到（D64）。 */
export interface ApiKeyCreated {
  api_key: string;
  key: ApiKeyRow;
}

// ---- 工作区成员（契约 §2）----
/** 邀请码（D120 · 契约 §2.1）。🔑 **永远不含明文**，只有 `code_prefix`。 */
export interface InviteRow {
  id: number;
  workspace_id: number;
  code_prefix: string;
  role: string;
  max_uses: number;
  used: number;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  expiry_text: string;
  state: "可用" | "已用完" | "已过期" | "已撤销";
}

/** 生成成功。🔑 `code` 是明文，**只出现在这一个响应里**（D120）。 */
export interface InviteCreated {
  invite: InviteRow;
  code: string;
  notice: string;
}

export interface MemberRow {
  user_id: number;
  email: string;
  display_name: string | null;
  role: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** L3 坐标：归一化到 0~1（D33），与渲染分辨率无关。 */
export interface Location {
  page: number;
  bbox: [number, number, number, number];
  /** 表格单元格才有（D58）。 */
  row?: number;
  col?: number;
}

/**
 * 表格的**结构化**网格（D203，`chunks.table_json`）。
 *
 * 文本（`content_norm`）仍是 Markdown（人读），这一份是给前端渲染成真表格用的。
 * 只随 `GET /documents/{id}/chunks` 返回；旧点/旧行可能为 `null`（重摄取欠账）。
 */
export interface TableJson {
  /** 首行当表头（可能是空串）。 */
  header: string[];
  /** 数据行，已按列补空。 */
  rows: string[][];
  row_count: number;
  col_count: number;
}

export interface HighlightSpan {
  text: string;
  highlight: string;
  positions: number[][];
}

export interface SearchHit {
  doc_id: number;
  file_name: string;
  chunk_index: number;
  /**
   * chunk 正文（≤800 字，D77）。
   *
   * ⚠️ 别再用 `snippets` 当正文：它是**字面子串**匹配的产物，
   * 自然语言提问下实测 100% 为空（§5.34）。`snippets` 只用来打高亮。
   */
  text: string;
  snippets: HighlightSpan[];
  /** RRF 分（`1/60 + 1/62 ≈ 0.0326`）—— **只能用来排序**，不是置信度。 */
  score: number;
  kind: CitationKind;
  locations: Location[] | null;
  thumb_url: string | null;
}

export interface SearchResp {
  items: SearchHit[];
  total: number;
  mode: SearchMode;
  page: number;
  /**
   * D161：`include_debug=true` 时的四路召回 + RRF 过程。
   * 形状 = `DebugResp` 去掉 `query`/`mode`；默认检索为 `null`。
   */
  debug: Omit<DebugResp, "query" | "mode"> | null;
}

/** SSE `event: sources`。 */
export interface SourceItem {
  idx: number;
  kind: CitationKind;
  score: number;
  title: string;
  doc_id: number;
  chunk_index: number;
  date: string;
  text: string;
  locations: Location[] | null;
  thumb_url: string | null;
}

/** SSE `event: done`。 */
export interface DoneFrame {
  citations: number[];
  elapsed_ms: number;
  retrieval_ms: number;
  generator: string;
  dangling_citations: number[];
  session_id: number;
  legs: Record<string, { n: number; ms: number; skipped: string | null }>;
}
// ============================================================
// 外部 LLM 配置（D170 · 契约 §12）
// ============================================================

export type LlmProvider = "extractive" | "openai_compat";

/** `GET/PUT /admin/llm` 的响应。**API Key 只回掩码**（`api_key_masked`）。 */
export interface LlmConfig {
  provider: LlmProvider;
  base_url: string | null;
  model: string | null;
  enabled: boolean;
  has_key: boolean;
  api_key_masked: string | null;
  extra_headers: Record<string, string>;
  temperature: number;
  /** 🔑 D171：哪些功能用外部 LLM（细粒度开关） */
  use_for_ask: boolean;
  use_for_contextual: boolean;
  updated_at: string | null;
  updated_by: number | null;
  /** 后端算好的"能不能真的走外部 LLM"（前端不重算） */
  external_active: boolean;
}

/** `PUT /admin/llm` 的请求。⚠️ `api_key` 不传 = 保持原值。 */
export interface UpdateLlmReq {
  provider: LlmProvider;
  base_url?: string | null;
  model?: string | null;
  api_key?: string | null;
  clear_api_key?: boolean;
  extra_headers?: Record<string, string> | null;
  temperature?: number | null;
  enabled?: boolean | null;
  use_for_ask?: boolean | null;
  use_for_contextual?: boolean | null;
}

/** `POST /admin/llm/test` 的响应。 */
export interface LlmTestResp {
  ok: boolean;
  message: string;
  latency_ms: number | null;
  model: string | null;
}

// ============================================================
// 超管：用户管理（D173 · 契约 §12）
// ============================================================

export type UserRole = "user" | "superadmin";

/** `GET /admin/users` 的一行（超管）。 */
export interface AdminUser {
  id: number;
  email: string;
  nickname: string | null;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

/** `POST /admin/users`。 */
export interface CreateUserReq {
  email: string;
  password: string;
  display_name: string;
  nickname?: string | null;
  role: UserRole;
}

/** `PATCH /admin/users/{id}`。不传的字段保持不变。 */
export interface UpdateUserReq {
  display_name?: string | null;
  nickname?: string | null;
  clear_nickname?: boolean;
  email?: string | null;
  /** 超管重置密码（不需要旧密码，D175）。 */
  new_password?: string | null;
  role?: UserRole | null;
  is_active?: boolean | null;
}

/** `PATCH /auth/me`（D175）：改自己的资料。改 email/密码要带 `current_password`。 */
export interface UpdateMeReq {
  display_name?: string | null;
  nickname?: string | null;
  clear_nickname?: boolean;
  email?: string | null;
  current_password?: string | null;
  new_password?: string | null;
}
