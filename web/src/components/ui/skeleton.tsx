/**
 * Skeleton —— 加载态骨架（D184 的诉求：**不要裸写"加载中…"**）。
 *
 * ⚠️ `SkeletonRows` 刻意**不放在 `<tbody>` 里**：占位行会把"表里有几行"
 * 这类计数带偏（原来的 e2e 就吃过这个亏）。它渲染在表格**外面**。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-bg-card", className)}
      aria-hidden="true"
      {...props}
    />
  );
}

/** n 行占位（列表 / 卡片区用）。 */
export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 p-4", className)}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

/** 表格骨架：**表格外面**的一块，不产生 `<tr>`。 */
export function SkeletonTable({ rows = 4 }: { rows?: number }) {
  return (
    <div className="rounded-2xl bg-bg-card">
      <SkeletonRows rows={rows} />
    </div>
  );
}
