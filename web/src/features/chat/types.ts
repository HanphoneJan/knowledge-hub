/**
 * 一轮问答在前端的内存形状。
 *
 * ⚠️ 历史消息与实时消息**不是同一种东西**：
 * - 实时：SSE 的 `sources` 帧带来完整的来源（文件名 / 坐标 / 正文 / 缩略图），
 *   所以引用点得开、画得出框；
 * - 历史：`chat_messages` 只存了 `citations: number[]`（角标编号），
 *   **没有存来源身份**（`services/retrieval/ask.py::_persist` 只写 content+citations）。
 *   ⇒ 历史消息的角标只能"知道有第 n 条引用"，点不出原文。
 *   界面必须**如实说明**这一点，而不是给一个点了没反应的角标（`history` 标志）。
 */
import type { SourceItem } from "../../types/api";

export type TurnStatus = "streaming" | "done" | "error" | "stopped";

export interface Turn {
  key: string;
  role: "user" | "assistant";
  content: string;
  status: TurnStatus;
  /** 合法角标（1-based，指向 `sources`）。 */
  citations?: number[];
  /** 悬空角标：答案里引用了不存在的编号（后端也会报，见 `done.dangling_citations`）。 */
  dangling?: number[];
  sources?: SourceItem[];
  /** 历史消息：没有 `sources`，角标不可核验。 */
  history?: boolean;
  /** 触发这一轮回答的问题（"重试"要重发它，而不是空字符串）。 */
  question?: string;
  error?: string;
  retrievalMs?: number;
  elapsedMs?: number;
  generator?: string;
}

let seq = 0;
export function nextKey(prefix: string): string {
  seq += 1;
  return `${prefix}-${seq}`;
}

/** SSE `sources` → 引用列表（`idx` 就是答案里的角标编号）。 */
export function sourceByIndex(sources: SourceItem[] | undefined, idx: number): SourceItem | null {
  if (!sources) return null;
  return sources.find((s) => s.idx === idx) ?? null;
}
