/**
 * Menu —— 行内操作菜单（"⋯"下拉）。**不引 Radix DropdownMenu**（没装，且用法很窄）。
 *
 * 实现要点（都不性感但都要有）：
 * - 面板 `position: fixed` + 由触发按钮的 `getBoundingClientRect()` 定位
 *   ⇒ 表格的 `overflow: auto` **不会裁掉**它（绝对定位会被裁，这是最常见的坑）；
 * - 点面板外 / Esc / 滚动 / 改窗口大小 → 关闭；
 * - `role="menu"` / `role="menuitem"`，键盘 Esc 可退。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export interface MenuItem {
  key: string;
  label: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

export function Menu({
  items,
  trigger,
  align = "end",
  label = "更多操作",
}: {
  items: MenuItem[];
  trigger?: React.ReactNode;
  align?: "start" | "end";
  label?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [pos, setPos] = React.useState<{ top: number; left: number } | null>(null);
  const btnRef = React.useRef<HTMLButtonElement>(null);
  const panelRef = React.useRef<HTMLDivElement>(null);

  const place = React.useCallback(() => {
    const el = btnRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const W = 176;
    const left = align === "end" ? Math.max(8, r.right - W) : Math.min(window.innerWidth - W - 8, r.left);
    setPos({ top: Math.min(r.bottom + 4, window.innerHeight - 12), left });
  }, [align]);

  const close = React.useCallback(() => setOpen(false), []);

  React.useEffect(() => {
    if (!open) return;
    place();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open, place, close]);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex size-7 items-center justify-center rounded-md text-text-secondary transition-colors",
          "hover:bg-bg-card hover:text-text-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent-primary",
        )}
      >
        {trigger ?? <MoreDots />}
      </button>
      {open && pos && (
        <div
          ref={panelRef}
          role="menu"
          style={{ top: pos.top, left: pos.left, width: 176 }}
          className="fixed z-50 flex flex-col overflow-hidden rounded-lg border-[0.5px] border-border-button bg-bg-component p-1 shadow-lg animate-overlay-in"
        >
          {items.map((it) => {
            const Icon = it.icon;
            return (
              <button
                key={it.key}
                type="button"
                role="menuitem"
                disabled={it.disabled}
                onClick={() => {
                  close();
                  it.onSelect();
                }}
                className={cn(
                  "flex h-8 items-center gap-2 rounded-md px-2 text-start text-sm transition-colors disabled:opacity-50",
                  it.danger
                    ? "text-state-error hover:bg-state-error-5"
                    : "text-text-primary hover:bg-bg-card",
                )}
              >
                {Icon && <Icon className="size-3.5" />}
                {it.label}
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}

function MoreDots() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="size-4" fill="currentColor">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}
