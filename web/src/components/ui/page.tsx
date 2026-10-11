/**
 * PageHeader / Page —— 页面骨架（D184 的共享骨架，D185 的"页面自己滚动"）。
 *
 * 🔑 **外壳 `<main>` 是 `overflow-hidden`，页面自己滚**（CONVENTIONS §10.5.2）：
 * - 非对话页用 `<Page>`：撑满高度 + 内部滚动 + 居中列；
 * - 对话页自己拼 `flex` 列（消息区 `flex-1 overflow-y-auto` + 输入条 `shrink-0`）。
 *
 * ⚠️ 新页面**不要**再假设 main 会滚动。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export function Page({
  children,
  className,
  /** 内容列宽度：列表/设置页宽一些，阅读页窄一些。 */
  width = "wide",
}: {
  children: React.ReactNode;
  className?: string;
  width?: "wide" | "read" | "full";
}) {
  const max =
    width === "read" ? "max-w-[58rem]" : width === "full" ? "max-w-none" : "max-w-[76rem]";
  return (
    <div className={cn("h-full overflow-y-auto", className)}>
      <div className={cn("mx-auto w-full px-4 py-6 sm:px-8", max)}>{children}</div>
    </div>
  );
}

export function PageHeader({
  title,
  meta,
  actions,
  tabs,
  className,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  /** 下划线式页签（紧跟标题那一行下面）。 */
  tabs?: React.ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("mb-4", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="truncate text-xl font-semibold text-text-primary">{title}</h1>
          </div>
          {meta && <div className="flex flex-wrap items-center gap-2 text-sm text-text-secondary">{meta}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {tabs && <div className="mt-3">{tabs}</div>}
    </header>
  );
}
