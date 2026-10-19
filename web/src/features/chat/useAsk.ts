/**
 * `/ask` 的流式消费（POST + SSE，所以**不能用 `EventSource`** —— 见 `lib/sse.ts`）。
 *
 * 帧序 `sources → delta* → done`。`sources` 先到 ⇒ **先把引用卡画出来**
 * （D46：检索段 < 500ms，用户不该对着"正在生成…"等引用）。
 *
 * ⚠️ 停止生成：`abort()` 之后**要落到一个明确的状态**（`stopped`），
 * 不然那条消息会永远停在"生成中"的转圈上。
 */
import * as React from "react";

import { askStream, type DoneEvent } from "../../lib/sse";
import type { SourceItem } from "../../types/api";
import { nextKey, type Turn } from "./types";

export interface AskHandlers {
  /** 新会话的第一轮结束后，后端才会给出 `session_id` ⇒ 调用方负责换 URL。 */
  onSessionCreated?: (sessionId: number) => void;
}

export function useAsk(
  kbIds: number[] | null,
  sessionId: number | null,
  handlers: AskHandlers = {},
) {
  const [turns, setTurns] = React.useState<Turn[]>([]);
  const ctrlRef = React.useRef<AbortController | null>(null);
  const [busy, setBusy] = React.useState(false);

  // 回调放进 ref：SSE 是一段长跑，闭包拿到旧的 handler 会漏掉新的导航函数
  const handlersRef = React.useRef(handlers);
  handlersRef.current = handlers;

  const patch = React.useCallback((key: string, up: Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.key === key ? { ...t, ...up } : t)));
  }, []);

  const send = React.useCallback(
    (question: string) => {
      const q = question.trim();
      if (!q || ctrlRef.current) return;
      const userKey = nextKey("u");
      const botKey = nextKey("a");
      setTurns((prev) => [
        ...prev,
        { key: userKey, role: "user", content: q, status: "done" },
        { key: botKey, role: "assistant", content: "", status: "streaming", question: q },
      ]);
      setBusy(true);

      let acc = "";
      const ctrl = askStream(
        kbIds,
        { question: q, session_id: sessionId },
        {
          onSources: (sources: SourceItem[], retrievalMs: number) => {
            patch(botKey, { sources, retrievalMs });
          },
          onDelta: (text: string) => {
            acc += text;
            patch(botKey, { content: acc });
          },
          onDone: (done: DoneEvent) => {
            patch(botKey, {
              status: "done",
              citations: done.citations,
              dangling: done.dangling_citations,
              elapsedMs: done.elapsed_ms,
              generator: done.generator,
            });
            ctrlRef.current = null;
            setBusy(false);
            handlersRef.current.onSessionCreated?.(done.session_id);
          },
          onError: (err: Error) => {
            // ⚠️ 已经有半截正文时不能把它抹掉：保留 + 标注中断（用户可能还想看）
            patch(botKey, { status: "error", error: err.message, content: acc });
            ctrlRef.current = null;
            setBusy(false);
          },
        },
      );
      ctrlRef.current = ctrl;
    },
    [kbIds, sessionId, patch],
  );

  const stop = React.useCallback(() => {
    const c = ctrlRef.current;
    if (!c) return;
    c.abort();
    ctrlRef.current = null;
    setBusy(false);
    setTurns((prev) => {
      const out = [...prev];
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].role === "assistant" && out[i].status === "streaming") {
          out[i] = { ...out[i], status: "stopped" };
          break;
        }
      }
      return out;
    });
  }, []);

  /** 会话切换 / 重新拉取时重置（正在流式时不要清，否则会吞掉半截回答）。 */
  const reset = React.useCallback(() => {
    if (ctrlRef.current) return;
    setTurns([]);
  }, []);

  React.useEffect(() => () => ctrlRef.current?.abort(), []);

  return { turns, busy, send, stop, reset, setTurns };
}

/** 历史消息 → Turn（`history: true`：角标不可核验，见 `types.ts`）。 */
export function historyTurns(
  messages: Array<{ role: string; content: string; citations?: number[] }>,
): Turn[] {
  return messages.map((m, i) => ({
    key: `h-${i}`,
    role: m.role === "user" ? "user" : "assistant",
    content: m.content,
    status: "done" as const,
    citations: m.citations,
    history: true,
  }));
}
