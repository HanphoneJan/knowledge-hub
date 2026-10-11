/**
 * Select —— **原生 `<select>`**，只做外观统一。
 *
 * 🔑 刻意不引 Radix Select（CONVENTIONS §10.5「原生控件够用就别换 Radix」）：
 * 少一层封装、少一个依赖，键盘 / 无障碍行为白送。
 */
import { ChevronDown } from "lucide-react";
import { cn } from "../../lib/cn";

export interface SelectOption<T extends string | number> {
  value: T;
  label: string;
  disabled?: boolean;
}

export function Select<T extends string | number>({
  value,
  onChange,
  options,
  className,
  placeholder,
  disabled,
  id,
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: Array<SelectOption<T>>;
  className?: string;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
}) {
  return (
    <div className={cn("relative inline-flex", className)}>
      <select
        id={id}
        aria-label={ariaLabel}
        disabled={disabled}
        value={String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          const hit = options.find((o) => String(o.value) === raw);
          if (hit) onChange(hit.value);
        }}
        className={cn(
          "h-8 w-full appearance-none rounded-md border-[0.5px] border-border-button bg-bg-input ps-3 pe-8 text-sm text-text-primary outline-none transition-colors",
          "focus-visible:ring-1 focus-visible:ring-accent-primary disabled:cursor-not-allowed disabled:opacity-50",
        )}
      >
        {placeholder && value === "" && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((o) => (
          <option key={String(o.value)} value={String(o.value)} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute end-2 top-1/2 size-3.5 -translate-y-1/2 text-text-secondary"
      />
    </div>
  );
}
