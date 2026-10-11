/**
 * EmptyState —— 空态（居中图标 + 标题 + 说明 + 动作）。
 *
 * D184 的诉求：**不要裸写"加载中…"**，空 / 加载 / 错误三态齐全。
 * RAGFlow 管理台的空态就是这个形状。
 */
import type { LucideIcon } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";

export function EmptyState({
  icon: Icon,
  title,
  description,
  actions,
  size = "default",
  className,
}: {
  icon?: LucideIcon;
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  size?: "sm" | "default" | "lg";
  className?: string;
}) {
  const box = size === "sm" ? "size-9" : size === "lg" ? "size-16" : "size-12";
  const iconSize = size === "sm" ? "size-4" : size === "lg" ? "size-7" : "size-5";
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 px-6 text-center",
        size === "lg" ? "py-16" : "py-10",
        className,
      )}
    >
      {Icon && (
        <span className={cn("flex items-center justify-center rounded-2xl bg-bg-card text-text-secondary", box)}>
          <Icon aria-hidden="true" className={iconSize} />
        </span>
      )}
      <div className="flex flex-col gap-1">
        <div className={cn("font-medium text-text-primary", size === "lg" ? "text-base" : "text-sm")}>
          {title}
        </div>
        {description && (
          <div className="max-w-md text-sm text-text-secondary whitespace-pre-line">{description}</div>
        )}
      </div>
      {actions && <div className="mt-1 flex items-center gap-2">{actions}</div>}
    </div>
  );
}
