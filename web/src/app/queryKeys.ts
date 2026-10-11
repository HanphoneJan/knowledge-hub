/**
 * React Query 的 query key 工厂（照 RAGFlow 的 `{Domain}Keys` 约定）。
 *
 * 🔑 **不许在 `useQuery` / `invalidateQueries` 里手写裸数组**：
 * 两处各写一份，键结构一漂就变成"改了没生效 / 缓存永远旧的"，
 * 而这类 bug 不会报错，只表现为"页面数据是上一次的"。
 */
export const Keys = {
  me: () => ["me"] as const,

  workspaces: () => ["workspaces"] as const,
  workspaceTrash: () => ["workspaces", "trash"] as const,
  members: (wsId: number) => ["workspaces", wsId, "members"] as const,
  invites: (wsId: number) => ["workspaces", wsId, "invites"] as const,
  wsStats: (wsId: number) => ["workspaces", wsId, "stats"] as const,

  kbs: () => ["kb"] as const,
  kb: (kbId: number) => ["kb", kbId] as const,
  kbTrash: () => ["kb", "trash"] as const,
  pii: (kbId: number) => ["kb", kbId, "pii"] as const,

  docs: (kbId: number) => ["kb", kbId, "documents"] as const,
  /** 某个库的**文档**回收站（与 `kbTrash()` 的"库回收站"不是一回事）。 */
  kbTrashDocs: (kbId: number) => ["kb", kbId, "trash"] as const,
  chunks: (docId: number) => ["documents", docId, "chunks"] as const,

  jobs: (q: { kb_id?: number; doc_id?: number }) => ["jobs", q] as const,

  sessions: (kbId?: number) => ["sessions", kbId ?? null] as const,
  session: (id: number) => ["session", id] as const,

  keys: () => ["keys"] as const,
  users: () => ["admin", "users"] as const,
  llm: () => ["admin", "llm"] as const,

  timeline: (kbId: number, key: string) => ["kb", kbId, "timeline", key] as const,
  photos: (kbId: number, q: Record<string, unknown>) => ["kb", kbId, "photos", q] as const,
} as const;
