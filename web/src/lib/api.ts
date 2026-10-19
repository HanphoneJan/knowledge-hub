/**
 * 契约客户端（`docs/DESIGN-API.md`）。
 *
 * ## 三条约定在这里落地
 *
 * 1. **API Key 是唯一租户来源**（§0.5）：请求体里永远不带 `user_id`，
 *    身份只从 `Authorization: Bearer` 推导。前端**不做**任何可见性判定 ——
 *    无权限与不存在都返回 `KB_NOT_FOUND`（§0.4），所以这里只需要如实显示错误码。
 * 2. **错误一律按契约的 code 显示**，不要自己猜含义。
 * 3. **不在这里重算检索逻辑**（§0.3）：只调那三个端点。
 */

import { clearAuth, getToken } from "./auth";
import type { PurgeResp } from "./trash";
import type {
  AdminUser,
  Anniversary,
  ApiKeyCreated,
  ApiKeyRow,
  ChunkKind,
  ChunkStrategy,
  CreateUserReq,
  CreateWorkspaceReq,
  DebugResp,
  DocumentRow,
  InviteCreated,
  InviteRow,
  Job,
  Kb,
  KbDetail,
  LlmConfig,
  LlmTestResp,
  Location,
  MemberRow,
  Milestone,
  Page,
  TableJson,
  WorkspaceStats,
  PhotoRow,
  PiiReport,
  SessionDetail,
  SessionRow,
  SearchMode,
  SearchResp,
  TimelineResp,
  UpdateLlmReq,
  UpdateMeReq,
  UpdateUserReq,
  UploadResp,
  User,
  Workspace,
} from "../types/api";

export const API_BASE = "/api/v1";

/**
 * 把"契约里的路径"归一成 fetch 用的路径。
 *
 * ⚠️ 有的字段自带 `/api/v1` 前缀（`SearchHit.thumb_url` / SSE 的
 * `sources[].thumb_url` —— 它们要直接给 MCP/A2A 消费方用，所以是绝对路径），
 * 而本文件其它方法传的是不带前缀的相对路径。**两种都要支持**，
 * 否则拼出来是 `/api/v1/api/v1/...` → 404，而页面表现只是"图不出来"。
 */
export function apiPath(p: string): string {
  if (!p) return p;
  return p.startsWith("/api/") ? p : `${API_BASE}${p}`;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId?: string;
  readonly detail?: unknown;

  constructor(status: number, code: string, message: string, requestId?: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.detail = detail;
  }

  /** 无权限 ≡ 不存在：这两条只能靠 code 区分不了，**不要**试图区分。 */
  get isMissing(): boolean {
    return this.code === "KB_NOT_FOUND" || this.code === "DOC_NOT_FOUND";
  }

  get isAuth(): boolean {
    return this.code === "INVALID_TOKEN";
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function toApiError(res: Response): Promise<ApiError> {
  const rid = res.headers.get("x-request-id") ?? undefined;
  let code = "INTERNAL";
  let message = `${res.status} ${res.statusText}`;
  let detail: unknown;
  try {
    const body = (await res.json()) as {
      error?: { code?: string; message?: string; detail?: unknown };
    };
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      detail = body.error.detail;
    }
  } catch {
    // 非 JSON 错误体（如 502 网关页），保留状态码文案
  }
  const err = new ApiError(res.status, code, message, rid, detail);
  // key 失效就清掉，让路由跳登录（**不自动重登** —— D64 会多铸一把 key）
  if (err.isAuth) clearAuth();
  return err;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(apiPath(path), {
    ...init,
    headers: {
      ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...authHeaders(),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** 拿二进制（页图）。⚠️ 不能用 `<img src>` —— 那个端点要 Bearer，见调用处。 */
export async function requestBlob(path: string): Promise<Blob> {
  // ⚠️ 缩略图 URL 自带 `/api/v1`（见 apiPath），这里不能再拼一次
  const res = await fetch(apiPath(path), { headers: authHeaders() });
  if (!res.ok) throw await toApiError(res);
  return res.blob();
}

// ============================================================
// 端点
// ============================================================

export interface LoginResp {
  user: User;
  api_key: string;
  key_prefix: string;
}

/** `GET /documents/{id}/chunks` 的一行（契约 §4.2）。 */
export interface ChunkRow {
  chunk_index: number;
  content_norm: string;
  locations: Location[] | null;
  kind: ChunkKind;
  table_index: number | null;
  row_count: number | null;
  col_count: number | null;
  /** D203：结构化网格（仅表格 chunk 有）。旧行可能为 null。 */
  table_json: TableJson | null;
}

export const api = {
  // ---- 认证（契约 §1）----
  login: (email: string, password: string) =>
    request<LoginResp>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  register: (email: string, password: string, display_name: string, nickname?: string) =>
    request<LoginResp>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, display_name, nickname: nickname || null }),
    }),
  me: () => request<User>("/auth/me"),
  /** D175：改自己的资料（显示名/昵称/邮箱/密码）。 */
  updateMe: (body: UpdateMeReq) =>
    request<User>("/auth/me", { method: "PATCH", body: JSON.stringify(body) }),

  // ---- 工作区 / 知识库（契约 §2 §3）----
  listWorkspaces: () => request<Workspace[]>("/workspaces"),
  /**
   * 建工作区（契约 §2，D190 补的前端面）。
   *
   * 🔑 创建者会被服务端**自动加成 owner 成员**（`routers/workspaces.py`），
   * 所以调用方拿回 `Workspace` 之后可以直接拿它的 id 去 `POST /kb`。
   * ⚠️ 名字**不唯一**（无约束）：重名不是错误，前端不要拦。
   */
  createWorkspace: (body: CreateWorkspaceReq) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(body) }),
  listKb: () => request<Kb[]>("/kb"),
  getKb: (kbId: number) => request<KbDetail>(`/kb/${kbId}`),
  /** D121：PII 检出报告（只读）。 */
  piiReport: (kbId: number) => request<PiiReport>(`/kb/${kbId}/pii`),
  /**
   * 改掩码开关（D121）。🔴 **只对之后摄取的文档生效**，不回溯已有 chunk ——
   *   所以调用方（设置页）必须在文案里说清"已有文档要重新摄取"。
   */
  setPiiMask: (kbId: number, on: boolean) =>
    request<Kb>(`/kb/${kbId}`, {
      method: "PATCH",
      body: JSON.stringify({ pii_mask: on }),
    }),
  /**
   * 改这个库的精排开关（D87）。传 `""` = 关闭。
   *
   * 🔑 精排改变排序，所以它**只能由服务端按库决定** ——
   * 请求体里没有"top_k 之外再传个 rerank 开关"这种东西（见 test_rerank_switch）。
   */
  setRerank: (kbId: number, model: string) =>
    request<Kb>(`/kb/${kbId}`, {
      method: "PATCH",
      body: JSON.stringify({ rerank_model: model }),
    }),
  createKb: (body: {
    workspace_id: number;
    name: string;
    description?: string | null;
    visibility: Kb["visibility"];
    source_kind: Kb["source_kind"];
    /** D164 切分策略（不传 = 后端默认 `recursive`）；D190 补的前端面。 */
    chunk_strategy?: ChunkStrategy;
    chunk_size?: number;
    chunk_overlap?: number;
  }) => request<Kb>("/kb", { method: "POST", body: JSON.stringify(body) }),
  /**
   * 改一个库的配置（契约 §3，`PATCH /kb/{id}`）。
   *
   * ⚠️ `embed_model` **不可改**（D22 全局统一，列只作记录）；`visibility`
   * 给 `""` 会被后端拒绝 —— 三档只能取枚举值。
   * ⚠️ `chunk_size`/`chunk_overlap`/`chunk_strategy` **只对之后重新摄取的文档生效**
   * （PATCH 不自动重跑），改用 `POST /documents/{id}/reparse` 逐份重跑（D190）。
   */
  updateKb: (
    kbId: number,
    body: Partial<{
      name: string;
      description: string | null;
      visibility: Kb["visibility"];
      chunk_size: number;
      chunk_overlap: number;
      chunk_strategy: ChunkStrategy;
      generate_image_desc: boolean;
      pii_mask: boolean;
      rerank_model: string;
    }>,
  ) => request<Kb>(`/kb/${kbId}`, { method: "PATCH", body: JSON.stringify(body) }),
  /** 删库（软删，30 天可恢复，D26/D42）。 */
  deleteKb: (kbId: number) => request<void>(`/kb/${kbId}`, { method: "DELETE" }),

  // ---- 文档（契约 §4）----
  listDocs: (kbId: number) => request<DocumentRow[]>(`/kb/${kbId}/documents`),
  getDoc: (docId: number) => request<DocumentRow & { preview_url: string }>(`/documents/${docId}`),
  uploadDocs: (kbId: number, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    return request<UploadResp>(`/kb/${kbId}/documents`, { method: "POST", body: fd });
  },
  deleteDoc: (docId: number) => request<void>(`/documents/${docId}`, { method: "DELETE" }),
  chunks: (docId: number, page = 1, pageSize = 100) =>
    request<ChunkRow[]>(`/documents/${docId}/chunks?page=${page}&page_size=${pageSize}`),

  // ---- 回收站 / 覆盖式更新（契约 §4.3）----
  /**
   * 回收站列表。**裸数组**，不是分页包（历史端点，见契约 §0.4）。
   *
   * 返回的是软删文档（`deleted_at IS NOT NULL`），含 `is_current=FALSE`
   * 的旧版本 —— 与 `DELETE /trash` 的筛选条件是同一个定义
   * （`purge.purgeable_stmt`）。
   */
  trash: (kbId: number, page = 1, pageSize = 100) =>
    request<DocumentRow[]>(`/kb/${kbId}/trash?page=${page}&page_size=${pageSize}`),

  /** 恢复：重新入队摄取（D44 旧向量只留 3 天，所以必须重跑）。 */
  restoreDoc: (docId: number) =>
    request<DocumentRow>(`/documents/${docId}/restore`, { method: "POST" }),

  /** 失败重试：重新入队 `KIND_INGEST`。 */
  retryDoc: (docId: number) =>
    request<{ job_id: number | null; status: string }>(`/documents/${docId}/retry`, {
      method: "POST",
    }),

  // ---- 库 / 工作区的回收站（D129 · D26/D42 的反悔窗口要**看得见**）----
  /**
   * 我删过的**库**。裸数组，按删除时间倒序。
   *
   * 🔑 判据是"我能恢复的"（写侧权限），不是"我能读的" ——
   * 两者差在"workspace 可见性但创建者已不是成员"那一格，那种库读得到、
   * 恢复不了。列出来只会让人点一下拿到 403。
   */
  kbTrash: () => request<Kb[]>("/kb/trash"),
  /** 恢复库：连带恢复那次删掉的文档并重新摄取（D44 旧向量只留 3 天）。 */
  restoreKb: (kbId: number) => request<Kb>(`/kb/${kbId}/restore`, { method: "POST" }),

  /**
   * 我删过的**工作区**（仅 owner —— 成员从来没被允许删它）。
   *
   * ⚠️ 与文档回收站同一个坑：`DELETE /workspaces/{id}` 软删，
   * 恢复端点 D125 补上了，但**没有地方能列出"删过哪些"** ——
   * 于是 D42 的 30 天窗口只能凭记忆按 id 调恢复，用户根本不知道还有它。
   */
  workspaceTrash: () => request<Workspace[]>("/workspaces/trash"),
  /**
   * 工作区用量（D132 · M9「磁盘用量按工作区统计」）。
   *
   * ⚠️ 口径 = **我能读**的那些库：同工作区里别人的 `private` 库不在其中
   *   （否则一个统计接口就成了绕过 §0.6 的探针）。
   */
  workspaceStats: (wsId: number) =>
    request<WorkspaceStats>(`/workspaces/${wsId}/stats`),
  restoreWorkspace: (wsId: number) =>
    request<Workspace>(`/workspaces/${wsId}/restore`, { method: "POST" }),

  // ---- 外部 LLM 配置（D170 · 契约 §12）----
  /** 读当前配置（**API Key 只回掩码**）。 */
  llmConfig: () => request<LlmConfig>("/admin/llm"),
  /** 写配置。⚠️ `api_key` 不传 = 保持原值；清空用 `clear_api_key`。 */
  updateLlm: (body: UpdateLlmReq) =>
    request<LlmConfig>("/admin/llm", { method: "PUT", body: JSON.stringify(body) }),
  /** 用当前配置试发一次请求。 */
  testLlm: () => request<LlmTestResp>("/admin/llm/test", { method: "POST" }),

  // ---- 超管：用户管理（D173 · 契约 §12）----
  listUsers: () => request<AdminUser[]>("/admin/users"),
  createUser: (body: CreateUserReq) =>
    request<AdminUser>("/admin/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (id: number, body: UpdateUserReq) =>
    request<AdminUser>(`/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  /** 重解析：改了 KB 的分块配置后用。 */
  reparseDoc: (docId: number) =>
    request<{ job_id: number | null }>(`/documents/${docId}/reparse`, { method: "POST" }),

  /**
   * 清空回收站 —— **本项目唯一不可撤销的端点**（删用户/删 KB 都还有软删兜底）。
   *
   * ⚠️ 先 `dryRun: true` 拿预览、再让用户确认：
   * DELETE 没有请求体，没法"先问一下"，而这里丢掉的正是 D26 的 30 天反悔窗口。
   * ⚠️ 不传 `kbId` 时 scope 是"我能读且能写"的**全部库** —— 更大，所以更要预览。
   */
  purgeTrash: (opts: { kbId?: number; dryRun?: boolean } = {}) => {
    const p = new URLSearchParams();
    if (opts.kbId != null) p.set("kb_id", String(opts.kbId));
    if (opts.dryRun) p.set("dry_run", "true");
    return request<PurgeResp>(`/trash?${p.toString()}`, { method: "DELETE" });
  },

  /**
   * 取某一个 chunk 的正文。
   *
   * ⚠️ `GET /documents/{id}/chunks` 只支持**分页**，没有 `?chunk_index=` 过滤，
   * 而 `/search` 的结果里又**不带正文**（`SearchHit` 只有 snippets，而
   * snippets 是字面子串匹配的产物 —— 自然语言提问常常匹配不到，于是是空数组）。
   * 所以要按 chunk_index 落到对应那一页再取。
   * chunks 按 `chunk_index` 升序返回，所以页号是确定的（`PAGE_SIZE=100`）。
   */
  chunkAt: async (docId: number, chunkIndex: number): Promise<ChunkRow | null> => {
    const PAGE_SIZE = 100;
    const page = Math.floor(chunkIndex / PAGE_SIZE) + 1;
    const rows = await api.chunks(docId, page, PAGE_SIZE);
    return rows.find((r) => r.chunk_index === chunkIndex) ?? null;
  },

  // ---- 检索（契约 §5）----
  search: (kbId: number, body: { query: string; mode: SearchMode; top_k: number; page?: number }) =>
    request<SearchResp>(`/kb/${kbId}/search`, { method: "POST", body: JSON.stringify(body) }),
  /**
   * 跨库检索（契约 §5.3 · D161）。`kbIds=null` = 全部有权限的库（D8 服务端推导）。
   *
   * 🔑 多选范围**一律走它**：单库只是 `kbIds=[id]` 的特例。这样前端只有一条
   * 检索路径，`include_debug` 也天然支持跨库（`/search/debug` 只支持单库）。
   */
  searchKbs: (
    kbIds: number[] | null,
    body: { query: string; mode: SearchMode; top_k: number; page?: number; include_debug?: boolean },
  ) =>
    request<SearchResp>("/search", {
      method: "POST",
      body: JSON.stringify({ ...body, kb_ids: kbIds }),
    }),

  /**
   * L3 底图：把第 `page` 页渲染成 JPEG。
   *
   * ⚠️ 必须 `fetch` + blob URL，**不能**写 `<img src="/api/v1/...">` ——
   * 这个端点要 `Authorization: Bearer`，`<img>` 带不了自定义请求头，
   * 会拿到 401 然后页面上一片空白（浏览器控制台里才看得到）。
   */
  pageImage: (docId: number, page: number, width = 1200) =>
    requestBlob(`/documents/${docId}/pages/${page}?width=${width}`),

  /**
   * 任务列表（契约 §12）。进度百分比的**唯一**来源。
   *
   * ⚠️ 这不是 `documents.status` 的替代品，两者要一起看：
   * `status` 回答"成了吗"，`progress` 回答"到哪了"。
   * 单看 progress 会漏掉"失败在 67%"这种情况。
   *
   * ⚠️ 租户口径在服务端（`kb_id ∈ ctx.readable_kb_ids`），
   * `kb_id IS NULL` 的运维任务（cleanup/reindex）**自动看不见**，
   * 所以这里不需要、也**不应该**在前端过滤 kind（CONVENTIONS §0.5）。
   */
  jobs: (q: { kb_id?: number; doc_id?: number; kind?: string; status?: string } = {}) => {
    const p = new URLSearchParams();
    if (q.kb_id != null) p.set("kb_id", String(q.kb_id));
    if (q.doc_id != null) p.set("doc_id", String(q.doc_id));
    if (q.kind) p.set("kind", q.kind);
    if (q.status) p.set("status", q.status);
    const qs = p.toString();
    return request<Page<Job>>(`/jobs${qs ? `?${qs}` : ""}`);
  },
  job: (jobId: number) => request<Job>(`/jobs/${jobId}`),

  // ---- 时间线与照片墙（契约 §6 · D20）----
  /**
   * ⚠️ `month` 与 `date` **互斥**，两个都给或都不给后端都返 **400**。
   * 前端别"两个都带上试试"—— 那是把一个明确的错误换成另一个。
   *
   * ⚠️ 非 chat/photo 库也返 **400**（不是 200 + 空列表）：
   * "空"和"不适用"是两件事，返回空列表会让用户以为"这个库还没导入数据"。
   * 页面据此显示"该视图对这类库没意义"（`lib/timeline.ts::notApplicableHint`）。
   */
  timelineMonth: (kbId: number, month: string) =>
    request<TimelineResp>(`/kb/${kbId}/timeline?month=${encodeURIComponent(month)}`),
  timelineDay: (kbId: number, date: string) =>
    request<TimelineResp>(`/kb/${kbId}/timeline?date=${encodeURIComponent(date)}`),
  anniversaries: (kbId: number, limit = 20) =>
    request<Anniversary[]>(`/kb/${kbId}/timeline/anniversaries?limit=${limit}`),
  milestones: (kbId: number, limit = 20) =>
    request<Milestone[]>(`/kb/${kbId}/timeline/milestones?limit=${limit}`),
  photos: (kbId: number, q: { date?: string; page?: number; pageSize?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.date) p.set("date", q.date);
    if (q.page != null) p.set("page", String(q.page));
    if (q.pageSize != null) p.set("page_size", String(q.pageSize));
    const qs = p.toString();
    return request<Page<PhotoRow>>(`/kb/${kbId}/photos${qs ? `?${qs}` : ""}`);
  },
  /**
   * 照片墙取**原图**：`GET /documents/{id}/preview`。
   *
   * 🔑 为什么不用 `thumbs/{n}`：缩略图只给 PDF 页生成，图片文档没有缩略图
   * （`_can_render_pages` 只认 PDF）。所以照片墙走 preview 拿原件。
   * ⚠️ 必须 `fetch` + blob URL（端点要 Bearer，`<img src>` 会静默 401）。
   */
  photoBlob: (docId: number) => requestBlob(`/documents/${docId}/preview`),

  // ---- 检索调试（契约 §12）----
  /**
   * 🔑 四路消融 + 路由/精排结论。这是"为什么这条排在前面"的唯一依据（D72），
   * 所以 `skipped` 必须带回来 —— 视觉腿在 API 进程恒 skipped（D50），
   * 不显示原因的话界面只能画"这条腿没结果"，用户会以为照片没被索引。
   */
  searchDebug: (kbId: number, body: { query: string; mode?: SearchMode; top_k?: number }) =>
    request<DebugResp>(`/kb/${kbId}/search/debug`, {
      method: "POST",
      body: JSON.stringify({ top_k: 10, ...body }),
    }),

  // ---- 会话（契约 §7）----
  // 🔑 返回的是**分页包** `{items,total,page,page_size}`（§0.4：新端点一律分页），
  //   不是裸数组。第一版按裸数组写，于是页面把整个包当数组渲染
  //   → 一个会话都列不出来（症状：`rows.map` 报错或渲染成空）。
  // ⚠️ 列表**不含软删的**：软删的会话还能恢复，混进列表会让"删了但还在"
  //   看起来像删除失败。
  /**
   * 我的**全部**会话（D163，`GET /sessions`）。
   *
   * 跨库问答后一个会话可能覆盖多个库，所以侧栏列表不该再按单库过滤。
   * `kb_id` 只作可选过滤。
   */
  sessionsAll: (kbId?: number) => {
    const qs = kbId != null ? `?kb_id=${kbId}` : "";
    return request<Page<SessionRow>>(`/sessions${qs}`);
  },
  /** 新建会话（可带库范围，D163）。 */
  createSession: (body: { title?: string | null; kb_ids?: number[] | null } = {}) =>
    request<SessionRow>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  sessions: (kbId: number) => request<Page<SessionRow>>(`/kb/${kbId}/sessions`),
  session: (sessionId: number) => request<SessionDetail>(`/sessions/${sessionId}`),
  renameSession: (sessionId: number, title: string) =>
    request<SessionRow>(`/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  /**
   * 删会话是**软删**（可恢复）—— 所以确认文案不能说"不可撤销"，
   * 而且 UI 上要给"恢复"留位置（否则用户会以为永久没了）。
   */
  deleteSession: (sessionId: number) =>
    request<void>(`/sessions/${sessionId}`, { method: "DELETE" }),

  // ---- API Key（契约 §1）----
  keys: () => request<ApiKeyRow[]>("/auth/keys"),
  /**
   * 🔑 新建 Key。**`api_key` 明文只在这一次响应里**（D64）——
   *   所以页面必须当场显示 + 让用户复制，不能"下次再给"。
   */
  // ⚠️ 请求体字段是 **`name`**（契约 §1 + `CreateKeyReq.name`），
  //   写成 `label` 会拿到 422 `Field required: body.name` ——
  //   而错误信息完全指不到"是你字段名猜错了"。
  createKey: (name: string) =>
    request<ApiKeyCreated>("/auth/keys", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  revokeKey: (keyId: number) => request<void>(`/auth/keys/${keyId}`, { method: "DELETE" }),

  // ---- 工作区成员（契约 §2）----
  members: (wsId: number) => request<MemberRow[]>(`/workspaces/${wsId}/members`),
  addMember: (wsId: number, body: { email: string; role?: string }) =>
    request<MemberRow>(`/workspaces/${wsId}/members`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  removeMember: (wsId: number, userId: number) =>
    request<void>(`/workspaces/${wsId}/members/${userId}`, { method: "DELETE" }),

  // ---- 邀请码（D120 · 契约 §2.1）----
  /** 🔑 明文**只出现在这一次响应里** —— 丢了就重新生成一段。 */
  createInvite: (wsId: number, body: { ttl_days?: number | null; max_uses?: number | null } = {}) =>
    request<InviteCreated>(`/workspaces/${wsId}/invites`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  invites: (wsId: number) => request<InviteRow[]>(`/workspaces/${wsId}/invites`),
  revokeInvite: (wsId: number, inviteId: number) =>
    request<void>(`/workspaces/${wsId}/invites/${inviteId}`, { method: "DELETE" }),
  /**
   * 兑换邀请码 → 加入工作区。
   *
   * ⚠️ 失败**统一一句话**（不存在 / 过期 / 用完 / 撤销都是同一句，D120）——
   *   所以界面上**不能**替后端猜"是过期了还是被撤销了"，
   *   只能原样显示那句话。
   */
  redeemInvite: (code: string) =>
    request<{ workspace_id: number; workspace_name: string; role: string;
              already_member: boolean; message: string }>("/invites/redeem", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
};

export function errText(e: unknown): string {
  if (e instanceof ApiError) {
    return `${e.code}：${e.message}`;
  }
  if (e instanceof Error) return e.message;
  return String(e);
}