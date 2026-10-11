/**
 * Badge —— 照搬 RAGFlow `web/src/components/ui/badge.tsx`（Apache-2.0）。
 *
 * 改动：`@/lib/utils` → 我们的 `cn`；补一个 RAGFlow 里没有但本项目在用的
 * `dot`（圆点状态胶囊：文档状态 / 可见性）。
 */
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "../../lib/cn";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-normal transition-colors outline-none focus:outline-none",
  {
    variants: {
      variant: {
        default: "border-transparent bg-bg-card text-text-primary",
        secondary: "border-transparent bg-bg-card text-text-secondary rounded-md",
        success: "border-transparent bg-state-success-5 text-state-success rounded-md",
        warning: "border-transparent bg-state-warning-5 text-state-warning rounded-md",
        destructive: "border-transparent bg-state-error-5 text-state-error rounded-md",
        outline: "border-border-button text-text-secondary",
      },
      size: {
        default: "px-2.5 py-0.5 text-xs",
        sm: "px-1.5 py-0 text-2xs",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export type BadgeProps = React.HTMLAttributes<HTMLSpanElement> &
  VariantProps<typeof badgeVariants> & {
    /** 状态胶囊的圆点；颜色随 variant 的文字色（`text-current`）。 */
    dot?: boolean;
  };

function Badge({ className, variant, size, dot = false, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant, size }), className)} {...props}>
      {dot && (
        <span
          aria-hidden="true"
          className="size-1.5 shrink-0 rounded-full bg-current"
        />
      )}
      {children}
    </span>
  );
}

export { Badge, badgeVariants };
