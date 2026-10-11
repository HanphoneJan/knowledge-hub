/**
 * SSE 客户端（`POST /kb/{id}/ask`，契约 §5.1）。
 *
 * ## ⚠️ 为什么不能用 `EventSource`
 *
 * `EventSource` 只能 GET、不带自定义请求头、不带请求体。
 * 而 `/ask` 是 **POST + JSON body + `Authorization: Bearer`**。
 * 用 EventSource 的写法在这里必然失败（而且失败方式是"一直转圈"，
 * 因为连请求都发不出去）。
 *
 * → 必须 `fetch` + `ReadableStream` 自己解帧。这也是 D46（首 token < 2 秒）
 *    能观测的前提：拿到第一个 `sources` 帧就算"检索段结束"。
 *
 * ## 帧序列（`services/retrieval/ask.py::stream_answer`）
 *
 * ```
 * sources → delta* → done
 * ```
 * `sources` 必须最先发 —— 前端据此把 UI 从"检索中"切到"生成中"，
 * 并**立刻**画出引用卡片（D46：检索段 < 500ms）。
 *
 * ⚠️ 一帧可能被 TCP 切成任意多个 chunk（反之亦然），所以要按 `\n\n` 攒够一帧
 * 才解析；`data:` 后面是 JSON 序列化过的字符串，**不会**含裸换行
 * （服务端就是靠这一点才敢手拼帧）。
 */

import { API_BASE, ApiError, errText } from "./api";
import { getToken } from "./auth";
import type { SourceItem } from "../types/api";

export interface SseHandlers {
  onSources: (sources: SourceItem[], retrievalMs: number) => void;
  onDelta: (text: string) => void;
  onDone: (done: DoneEvent) => void;
  /** 兜底：连接异常/中断。要显示"生成中断"，不能静默停住。 */
  onError: (err: Error) => void;
}

export interface DoneEvent {
  citations: number[];
  elapsed_ms: number;
  retrieval_ms: number;
  generator: string;
  dangling_citations: number[];
  session_id: number;
  /**
   * 这一轮带进生成的上下文轮数（D141；0 = 没带）。
   * 🔑 与后端同一个名字：它回答的是"上一轮我说的话，模型这次看到没有"——
   * 之前 `chat_messages` 一直在写而生成端从不回读，多轮是**假的**。
   */
  history_turns: number;
  legs: Record<string, { n: number; ms: number; skipped: string | null }>;
}

/**
 * 发起一次问答并消费 SSE 流。
 *
 * @returns 一个 `abort()`，用于"停止生成"。
 */
export function askStream(
  kbIds: number[] | null,
  body: { question: string; session_id: number | null; top_k?: number; stream?: boolean },
  handlers: SseHandlers,
  signal?: AbortSignal,
): AbortController {
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort);
  ctrl.signal.addEventListener("abort", () => signal?.removeEventListener("abort", onAbort));

  void (async () => {
    try {
      const token = getToken();
      // D163：问答统一走跨库端点 `/ask`，范围用 `kb_ids`（`null` = 全部有权限）。
      const res = await fetch(`${API_BASE}/ask`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          Accept: "text/event-stream",
        },
        body: JSON.stringify({ stream: true, top_k: 8, kb_ids: kbIds, ...body }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw await resError(res);
      if (!res.body) throw new Error("响应没有 body，浏览器不支持流式读取");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // ⚠️ 一帧以空行结束；不能假设一个 chunk 就是一个帧
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          dispatch(frame, handlers);
        }
      }
      if (buf.trim()) dispatch(buf, handlers);
    } catch (e) {
      if (ctrl.signal.aborted) return; // 用户主动停止，不算错误
      handlers.onError(e instanceof Error ? e : new Error(errText(e)));
    }
  })();

  return ctrl;
}

function dispatch(frame: string, h: SseHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return; // 注释行（以 `:` 开头）用来保活
  const data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;

  switch (event) {
    case "sources":
      h.onSources(data.sources as SourceItem[], (data.retrieval_ms as number) ?? 0);
      break;
    case "delta":
      h.onDelta((data.text as string) ?? "");
      break;
    case "done":
      h.onDone(data as unknown as DoneEvent);
      break;
    default:
      // 未知事件：忽略而不是崩（契约可能扩展）
      break;
  }
}

async function resError(res: Response): Promise<Error> {
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    return new ApiError(
      res.status,
      body?.error?.code ?? "INTERNAL",
      body?.error?.message ?? res.statusText,
    );
  } catch {
    return new Error(`${res.status} ${res.statusText}`);
  }
}