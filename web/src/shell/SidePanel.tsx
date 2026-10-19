/**
 * 二级面板的外壳（~280px）：顶部槽（库范围）+ 标题 + 可滚内容 + 底部用户信息。
 *
 * ⚠️ **同一次只渲染一个面板**（图标栏三个入口互斥），移动端整块进 `Sheet`。
 * 桌面与移动是**二选一渲染**（靠 `useMediaQuery`），不是"CSS 藏一个"——
 * 否则同一控件在 DOM 里出现两次（重复入口 / 重复 id，都是错的）。
 */
import * as React from "react";

import { Avatar } from "../components/ui/avatar";
import { useAuth } from "../app/auth";
import { cn } from "../lib/cn";

export function SidePanel({
  title,
  top,
  actions,
  children,
  footer = true,
}: {
  title: string;
  /** 顶部槽（库范围选择器之类）—— 常驻可见，不随列表滚动。 */
  top?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  footer?: boolean;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-bg-component">
      {top}
      <div className="flex h-11 shrink-0 items-center justify-between gap-2 px-3">
        <span className="text-sm font-medium text-text-primary">{title}</span>
        {actions && <div className="flex items-center gap-1">{actions}</div>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">{children}</div>
      {footer && <UserFooter />}
    </div>
  );
}

/** 面板底部：当前账号（头像 + 名字 + 角色）。退出在图标栏（单一入口）。 */
function UserFooter() {
  const { user, isSuperadmin } = useAuth();
  const name = user?.display_name || user?.nickname || user?.email || "未登录";
  return (
    <div className="flex shrink-0 items-center gap-2 border-t-[0.5px] border-border-button px-3 py-2.5">
      <Avatar name={name} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-13 text-text-primary">{name}</div>
        <div className="truncate text-2xs text-text-secondary">{user?.email ?? ""}</div>
      </div>
      {isSuperadmin && (
        <span className="shrink-0 rounded-full bg-bg-card px-1.5 py-0.5 text-2xs text-text-secondary">
          超管
        </span>
      )}
    </div>
  );
}

/** 面板里的分组小标题。 */
export function PanelGroup({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div className="mb-1">
      {title && <div className="px-2 pb-1 pt-2 text-2xs text-text-secondary">{title}</div>}
      <div className="flex flex-col gap-0.5">{children}</div>
    </div>
  );
}

/** 面板里的可点行（会话 / 库 / 设置分区都用它）。
 *
 * ⚠️ 行**不是一个 `<button>`**：它有 hover 才出现的操作按钮（删除…），
 * 而"按钮里嵌按钮"是无效 HTML（点击事件与焦点都会乱）。结构是
 * 「外层 div + 主区域 button + 尾部操作」—— 和 Open WebUI 的侧栏一致。 */
export function PanelRow({
  active,
  onClick,
  icon: Icon,
  title,
  subtitle,
  trailing,
  className,
}: {
  active?: boolean;
  onClick?: () => void;
  icon?: React.ComponentType<{ className?: string }>;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  trailing?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "group flex w-full min-w-0 items-center rounded-lg transition-colors",
        active ? "bg-bg-card" : "hover:bg-bg-card",
        className,
      )}
    >
      <button
        type="button"
        onClick={onClick}
        aria-current={active ? "page" : undefined}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-start",
          active ? "text-text-primary" : "text-text-secondary hover:text-text-primary",
        )}
      >
        {Icon && <Icon className="size-4 shrink-0" />}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm">{title}</span>
          {subtitle && <span className="block truncate text-2xs text-text-secondary">{subtitle}</span>}
        </span>
      </button>
      {trailing && <span className="flex shrink-0 items-center gap-0.5 pe-1">{trailing}</span>}
    </div>
  );
}
