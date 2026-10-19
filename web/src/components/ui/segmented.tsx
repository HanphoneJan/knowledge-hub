/**
 * Segmented —— 分段控件（RAGFlow 的 `segmented.tsx` 同观感：一个圆角底槽 + 选中块）。
 *
 * 用法之一是**主题三态**（设置 → 外观）：RAGFlow/Open WebUI 的设置里
 * 这种三态都是分段控件，不是原生 `<select>`（D186）。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export interface SegmentedOption<T extends string> {
  value: T;
  label?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  title?: string;
  disabled?: boolean;
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
  size = "default",
  ariaLabel,
  name,
}: {
  value: T;
  onChange: (v: T) => void;
  options: Array<SegmentedOption<T>>;
  className?: string;
  size?: "default" | "sm";
  ariaLabel?: string;
  /** 给每个按钮加 `data-<name>-option`（测试/驱动用；不加也行）。 */
  name?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        // ⚠️ `w-fit`：它在 `flex flex-col` 的卡片里会被拉伸成整行宽（flex 交叉轴拉伸），
        // 而分段控件应该是**内容宽**的
        "inline-flex w-fit items-center gap-0.5 rounded-lg border-[0.5px] border-border-button bg-bg-input p-0.5",
        className,
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        const Icon = o.icon;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={o.title ?? (typeof o.label === "string" ? o.label : undefined)}
            disabled={o.disabled}
            data-selected={active ? "true" : "false"}
            {...(name ? { [`data-${name}-option`]: o.value } : {})}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-[6px] font-normal transition-colors disabled:opacity-50",
              size === "sm" ? "h-6 px-2 text-xs" : "h-7 px-2.5 text-13",
              active
                ? "bg-bg-component text-text-primary shadow-xs"
                : "text-text-secondary hover:text-text-primary",
            )}
          >
            {Icon && <Icon className="size-3.5" />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
