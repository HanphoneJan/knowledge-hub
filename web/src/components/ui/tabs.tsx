/**
 * Tabs —— **下划线式**页签（RAGFlow 详情页的观感）。
 *
 * 🔑 本项目页签**由 URL 驱动**（`?tab=` / `?section=` 是唯一真相），
 * 所以这里**不引 Radix 的状态**：它只是渲染 + `onSelect`，
 * 选中态来自外部（`useSearchParams`）。多了个状态源 = 两处真相。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export interface TabItem<T extends string> {
  value: T;
  label: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  /** 右侧计数（如"文档 12"）。 */
  count?: React.ReactNode;
  disabled?: boolean;
}

export function Tabs<T extends string>({
  value,
  onChange,
  items,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  items: Array<TabItem<T>>;
  className?: string;
}) {
  return (
    <div role="tablist" className={cn("flex items-center gap-1 overflow-x-auto border-b-[0.5px] border-border-button", className)}>
      {items.map((it) => {
        const active = it.value === value;
        const Icon = it.icon;
        return (
          <button
            key={it.value}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={it.disabled}
            onClick={() => onChange(it.value)}
            className={cn(
              "relative inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-t px-3 text-sm transition-colors disabled:opacity-50",
              active
                ? "text-text-primary after:absolute after:inset-x-2 after:-bottom-[0.5px] after:h-[2px] after:rounded-full after:bg-text-primary"
                : "text-text-secondary hover:text-text-primary",
            )}
          >
            {Icon && <Icon className="size-3.5" />}
            {it.label}
            {it.count != null && <span className="text-xs text-text-secondary">{it.count}</span>}
          </button>
        );
      })}
    </div>
  );
}
