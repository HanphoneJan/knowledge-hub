/**
 * Progress —— RAGFlow 的进度条观感（细轨道 + 圆头填充）。
 *
 * ⚠️ 契约纪律（`lib/progress.ts` / CONVENTIONS §3.4）：
 * **百分比只能来自 `jobs.progress`**；没有 jobs 行就**什么都不渲染**
 * （不是 0%），`done`/`failed` 也不画条。调用方负责这个判定，本组件只画数。
 */
import { clampPct } from "../../lib/progress";
import { cn } from "../../lib/cn";

export type ProgressTone = "working" | "ready" | "failed" | "cancelled";

const TONE_BAR: Record<ProgressTone, string> = {
  working: "bg-state-warning",
  ready: "bg-state-success",
  failed: "bg-state-error",
  cancelled: "bg-text-disabled",
};

export function Progress({
  value,
  tone = "working",
  className,
  label,
}: {
  /** 0~1（`jobs.progress` 的口径）。 */
  value: number;
  tone?: ProgressTone;
  className?: string;
  label?: string;
}) {
  const pct = clampPct(value);
  return (
    <div
      className={cn("progress-track h-1.5 w-full overflow-hidden rounded-full bg-bg-card", className)}
      role="progressbar"
      aria-valuenow={Math.round(pct * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div
        className={cn("progress-fill h-full rounded-full transition-[width] duration-300", TONE_BAR[tone])}
        style={{ width: `${pct * 100}%` }}
      />
    </div>
  );
}
