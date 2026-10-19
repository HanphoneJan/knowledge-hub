/**
 * Alert —— 提示条（信息 / 警告 / 危险 / 成功）。
 *
 * ⚠️ 正文里的 `**粗体**` 交给 `RichText` 渲染（页面没有 markdown 渲染器，
 * 星号会原样显示 —— CONVENTIONS §10.5）。
 */
import { AlertTriangle, CheckCircle2, Info, ShieldAlert } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";
import { RichText } from "./rich-text";

export type AlertTone = "info" | "warning" | "danger" | "success";

const TONE: Record<AlertTone, { cls: string; Icon: typeof Info }> = {
  info: { cls: "bg-bg-accent border-border-button text-text-primary", Icon: Info },
  warning: {
    cls: "bg-state-warning-5 border-state-warning/40 text-text-primary",
    Icon: AlertTriangle,
  },
  danger: { cls: "bg-state-error-5 border-state-error/40 text-text-primary", Icon: ShieldAlert },
  success: {
    cls: "bg-state-success-5 border-state-success/40 text-text-primary",
    Icon: CheckCircle2,
  },
};

export function Alert({
  tone = "info",
  title,
  children,
  actions,
  className,
  icon = true,
}: {
  tone?: AlertTone;
  title?: React.ReactNode;
  children?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
  icon?: boolean;
}) {
  const { cls, Icon } = TONE[tone];
  return (
    <div
      role="status"
      className={cn(
        "flex items-start gap-2.5 rounded-xl border-[0.5px] px-3.5 py-2.5 text-sm",
        cls,
        className,
      )}
    >
      {icon && <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
      <div className="min-w-0 flex-1">
        {title && <div className="font-medium">{title}</div>}
        {children && (
          <div className="text-text-secondary [&_strong]:font-semibold [&_strong]:text-text-primary">
            {typeof children === "string" ? <RichText text={children} /> : children}
          </div>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
