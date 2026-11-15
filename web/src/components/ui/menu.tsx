/**
 * Menu —— 行内操作菜单（"⋯"下拉）。
 *
 * ## 语义：这是 **disclosure**，不是 ARIA menu
 *
 * 面板里就是一组按钮。没有方向键空间导航、没有 roving tabindex 的实现，
 * 就不该声明 `role="menu"` + `aria-haspopup="menu"` —— 那是给读屏用户的承诺，
 * 兑现不了比不声明更糟（他们按 ↓ 会发现没有任何反应）。真要做菜单，
 * 得把方向键 / Home / End / 焦点循环整套补上；这里的用法（表格行操作）
 * 用 Tab 走按钮完全够。
 *
 * ## 定位
 *
 * 面板 `position: fixed` + 由触发按钮的 `getBoundingClientRect()` 定位，
 * 表格的 `overflow: auto` 不会裁掉它。**必须先量面板高度再决定向上还是向下**：
 * 只夹 `top` 会把整块面板放到视口下方（表格最后一行点"⋯"看起来像没反应）。
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

const PANEL_W = 176;
const GAP = 4;
const EDGE = 8;

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
  const panelId = React.useId();

  const place = React.useCallback(() => {
    const btn = btnRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const h = panelRef.current?.offsetHeight ?? 0;
    const below = r.bottom + GAP;
    // 下方放不下就翻到按钮上方（而不是把它夹到视口底部）。
    const flip = below + h > window.innerHeight - EDGE;
    const top = flip ? Math.max(EDGE, r.top - GAP - h) : below;
    const rawLeft = align === "end" ? r.right - PANEL_W : r.left;
    const left = Math.min(Math.max(EDGE, rawLeft), Math.max(EDGE, window.innerWidth - PANEL_W - EDGE));
    setPos({ top, left });
  }, [align]);

  // 先渲染（hidden）→ 量高度 → 定位 → 再显示；整个测量发生在绘制之前。
  React.useLayoutEffect(() => {
    if (open) place();
  }, [open, place, items.length]);

  // 打开后把焦点交给第一项：键盘用户不必再按一次 Tab。
  // `preventScroll` 很重要：焦点若触发滚动，会把下面"滚动即关闭"的监听点着。
  // 依赖 `pos` 而不是只依赖 `open`：面板在量到位置之前是 visibility:hidden，
  // 对隐藏元素调用 focus() 不生效，等可见了再交焦点。
  const focusedRef = React.useRef(false);
  React.useEffect(() => {
    if (!open) {
      focusedRef.current = false;
      return;
    }
    if (!pos || focusedRef.current) return;
    focusedRef.current = true;
    panelRef.current
      ?.querySelector<HTMLElement>("button:not(:disabled)")
      ?.focus({ preventScroll: true });
  }, [open, pos]);

  const close = React.useCallback((focusTrigger = false) => {
    setOpen(false);
    if (focusTrigger) btnRef.current?.focus();
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close(true);
      }
    };
    // scroll / resize 的监听回调会拿到事件参数，不能直接传 close
    // （它的首参是 focusTrigger，事件对象会被当成 true）。
    const onScroll = () => close();
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    // 滚动 / 改变窗口大小时面板会与按钮脱开：直接收起，别让它悬在错的位置。
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, close]);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={label}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex size-7 items-center justify-center rounded-md text-text-secondary transition-colors",
          "hover:bg-bg-card hover:text-text-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent-primary",
        )}
      >
        {trigger ?? <MoreDots />}
      </button>
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          role="group"
          aria-label={label}
          onBlur={(e) => {
            const next = e.relatedTarget as Node | null;
            if (!next) return; // 点到不可聚焦处：交给 pointerdown 判
            if (panelRef.current?.contains(next) || btnRef.current?.contains(next)) return;
            close();
          }}
          style={{
            top: pos?.top ?? -9999,
            left: pos?.left ?? -9999,
            width: PANEL_W,
            visibility: pos ? "visible" : "hidden",
          }}
          className="fixed z-50 flex max-h-[min(60vh,320px)] flex-col overflow-y-auto rounded-lg border-[0.5px] border-border-button bg-bg-component p-1 shadow-lg"
        >
          {items.map((it) => {
            const Icon = it.icon;
            return (
              <button
                key={it.key}
                type="button"
                disabled={it.disabled}
                onClick={() => {
                  close();
                  it.onSelect();
                }}
                className={cn(
                  "flex h-8 shrink-0 items-center gap-2 rounded-md px-2 text-start text-sm transition-colors disabled:opacity-50",
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
