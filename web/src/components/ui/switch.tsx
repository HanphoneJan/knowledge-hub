/**
 * Switch —— 开关（**原生 `<input type="checkbox">` + `peer`**，不引 Radix）。
 *
 * 用途：`pii_mask` / `generate_image_desc` 这类布尔配置（RAGFlow 的设置卡）。
 * 无障碍行为（空格切换、label 关联）全部来自原生控件。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export function Switch({
  checked,
  onChange,
  disabled,
  id,
  label,
  className,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  id?: string;
  /** 可见文案（放右侧）；不给就只渲染开关本身。 */
  label?: React.ReactNode;
  className?: string;
}) {
  const input = (
    <span className={cn("relative inline-flex h-5 w-9 shrink-0 items-center", className)}>
      <input
        id={id}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="peer absolute inset-0 z-10 m-0 cursor-pointer appearance-none rounded-full bg-bg-card outline-none transition-colors checked:bg-accent-primary focus-visible:ring-1 focus-visible:ring-accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute start-0.5 size-4 rounded-full bg-bg-component shadow-xs transition-transform peer-checked:translate-x-4 rtl:peer-checked:-translate-x-4"
      />
    </span>
  );
  if (label == null) return input;
  return (
    <label htmlFor={id} className="inline-flex cursor-pointer items-center gap-2 text-sm text-text-primary">
      {input}
      <span>{label}</span>
    </label>
  );
}
