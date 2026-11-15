/**
 * ToastProvider —— 轻量提示（替掉 mutation onError 里的 `window.alert`）。
 *
 * ## 为什么不是 alert
 *
 * alert 会打断当前操作、拿走焦点、逼用户点"确定"；而这里绝大多数消息是
 * "这次请求失败了"这种**不需要决策**的信息。toast 用 `role="status"` 播报，
 * 不抢焦点，5 秒后自己消失。
 *
 * ⚠️ 错误用 `role="alert"`（assertive）：读屏会立即念出来，不必等用户停下来。
 * ⚠️ 定时器集中登记并在卸载时清掉：组件卸载后还调用 setState 是 React 警告的常客。
 */
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";

export type ToastTone = "info" | "success" | "error";

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}

interface ToastApi {
  show: (message: string, tone?: ToastTone) => void;
  info: (message: string) => void;
  success: (message: string) => void;
  error: (message: string) => void;
}

const ToastContext = React.createContext<ToastApi | null>(null);

const TONE_CLS: Record<ToastTone, string> = {
  info: "border-border-button bg-bg-component text-text-primary",
  success: "border-state-success/40 bg-bg-component text-text-primary",
  error: "border-state-error/40 bg-bg-component text-text-primary",
};

/** 同时最多挂 4 条，旧的先走 —— 失败风暴时别把屏幕铺满。 */
const MAX_TOASTS = 4;
const TTL_MS = 5000;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);
  const seq = React.useRef(0);
  const timers = React.useRef(new Map<number, number>());

  const dismiss = React.useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer != null) window.clearTimeout(timer);
    timers.current.delete(id);
    setItems((list) => list.filter((t) => t.id !== id));
  }, []);

  React.useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const show = React.useCallback(
    (message: string, tone: ToastTone = "info") => {
      if (!message) return;
      const id = ++seq.current;
      setItems((list) => [...list.slice(-(MAX_TOASTS - 1)), { id, message, tone }]);
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), TTL_MS),
      );
    },
    [dismiss],
  );

  const api = React.useMemo<ToastApi>(
    () => ({
      show,
      info: (message) => show(message, "info"),
      success: (message) => show(message, "success"),
      error: (message) => show(message, "error"),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* 每条自己带 role=status / alert（隐式 live region）——
          这里别再套 aria-live，否则读屏会把同一条念两遍。 */}
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4">
        {items.map((t) => (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className={cn(
              "pointer-events-auto flex w-full max-w-md items-start gap-2 rounded-xl border-[0.5px] px-3.5 py-2.5 text-sm shadow-lg",
              TONE_CLS[t.tone],
            )}
          >
            <div className="min-w-0 flex-1 whitespace-pre-wrap">{t.message}</div>
            <button
              type="button"
              aria-label="关闭提示"
              onClick={() => dismiss(t.id)}
              className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
            >
              <X aria-hidden="true" className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 <ToastProvider> 内使用");
  return ctx;
}
