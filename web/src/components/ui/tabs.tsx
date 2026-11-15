/**
 * Tabs —— **下划线式**页签（RAGFlow 详情页的观感）。
 *
 * ## 两种形态，语义跟着真相走
 *
 * 1. 每个 item 都带 `href`（`?tab=` 驱动）⇒ 渲染成 `<nav>` + 链接 +
 *    `aria-current="page"`。导航就该用链接：键盘 / 中键 / 新标签页白送。
 * 2. 没有 `href`（本地 state 切的视图）⇒ 渲染成一组开关按钮 +
 *    `aria-pressed`。
 *
 * ⚠️ 之前这里声明了 `role="tablist"` / `role="tab"`，但没有 `tabpanel`、
 *    没有方向键 —— 那是没兑现的 ARIA 承诺，读屏用户按 ←/→ 会得到沉默。
 *    宁可用链接 / 开关按钮这两种**完整**的原生语义。
 */
import * as React from "react";
import { Link } from "react-router-dom";

import { cn } from "../../lib/cn";

export interface TabItem<T extends string> {
  value: T;
  label: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  /** 右侧计数（如"文档 12"）。 */
  count?: React.ReactNode;
  disabled?: boolean;
  /** 有 href ⇒ 这一组渲染成导航链接。 */
  href?: string;
}

const CONTAINER = "flex items-center gap-1 overflow-x-auto border-b-[0.5px] border-border-button";

function itemClass(active: boolean, disabled?: boolean) {
  return cn(
    "relative inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-t px-3 text-sm transition-colors",
    active
      ? "text-text-primary after:absolute after:inset-x-2 after:-bottom-[0.5px] after:h-[2px] after:rounded-full after:bg-text-primary"
      : "text-text-secondary hover:text-text-primary",
    disabled && "pointer-events-none opacity-50",
  );
}

export function Tabs<T extends string>({
  value,
  onChange,
  items,
  className,
  label = "页面分区",
  replace = false,
}: {
  value: T;
  onChange?: (v: T) => void;
  items: Array<TabItem<T>>;
  className?: string;
  /** 导航形态下的 `<nav>` 名字 / 按钮形态下分组的名字。 */
  label?: string;
  /** 导航形态：历史里替换而不是新增一条（与原 `setSearchParams(..., {replace:true})` 一致）。 */
  replace?: boolean;
}) {
  const asLinks = items.length > 0 && items.every((it) => it.href != null);

  const content = (it: TabItem<T>) => {
    const Icon = it.icon;
    return (
      <>
        {Icon && <Icon aria-hidden="true" className="size-3.5" />}
        {it.label}
        {it.count != null && <span className="text-xs text-text-secondary">{it.count}</span>}
      </>
    );
  };

  if (asLinks) {
    return (
      <nav aria-label={label} className={cn(CONTAINER, className)}>
        {items.map((it) => {
          const active = it.value === value;
          return (
            <Link
              key={it.value}
              to={it.href as string}
              replace={replace}
              aria-current={active ? "page" : undefined}
              aria-disabled={it.disabled || undefined}
              onClick={(e) => {
                if (it.disabled) e.preventDefault();
              }}
              className={itemClass(active, it.disabled)}
            >
              {content(it)}
            </Link>
          );
        })}
      </nav>
    );
  }

  return (
    <div role="group" aria-label={label} className={cn(CONTAINER, className)}>
      {items.map((it) => {
        const active = it.value === value;
        return (
          <button
            key={it.value}
            type="button"
            aria-pressed={active}
            disabled={it.disabled}
            onClick={() => onChange?.(it.value)}
            className={itemClass(active, it.disabled)}
          >
            {content(it)}
          </button>
        );
      })}
    </div>
  );
}
