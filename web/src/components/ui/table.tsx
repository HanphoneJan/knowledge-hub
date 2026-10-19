/**
 * Table —— 照搬 RAGFlow `web/src/components/ui/table.tsx`（Apache-2.0）。
 *
 * 改动：`cn` 的 import；`border-b-0.5` → `border-b-[0.5px]`（v4 不收小数边框）。
 * RAGFlow 的观感就是这里来的：**表头浅底 + 行 hover + 首末列缩进**。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement> & { rootClassName?: string }
>(({ className, rootClassName, ...props }, ref) => (
  <div
    className={cn(
      "relative w-full overflow-auto rounded-2xl bg-bg-card scrollbar-auto",
      rootClassName,
    )}
  >
    <table ref={ref} className={cn("w-full caption-bottom text-sm", className)} {...props} />
  </div>
));
Table.displayName = "Table";

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn("[&_tr]:border-b-[0.5px] top-0 sticky bg-bg-title z-10", className)}
    {...props}
  />
));
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn("[&_tr:last-child]:border-0", className)} {...props} />
));
TableBody.displayName = "TableBody";

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn(
      "border-t-[0.5px] border-border-button bg-bg-card font-medium [&>tr]:last:border-b-0",
      className,
    )}
    {...props}
  />
));
TableFooter.displayName = "TableFooter";

const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn(
      "border-b-[0.5px] border-border-button transition-colors hover:bg-bg-card",
      className,
    )}
    {...props}
  />
));
TableRow.displayName = "TableRow";

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      "first-of-type:pl-6 last-of-type:pr-6 h-11 px-4 text-left align-middle font-normal text-text-secondary [&:has([role=checkbox])]:pr-0",
      className,
    )}
    {...props}
  />
));
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td
    ref={ref}
    className={cn(
      "first-of-type:pl-6 last-of-type:pr-6 px-4 py-3 align-middle [&:has([role=checkbox])]:pr-0 text-text-primary font-normal",
      className,
    )}
    {...props}
  />
));
TableCell.displayName = "TableCell";

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(({ className, ...props }, ref) => (
  <caption ref={ref} className={cn("mt-4 text-sm text-text-secondary", className)} {...props} />
));
TableCaption.displayName = "TableCaption";

/**
 * 空表行（RAGFlow 里没有，本项目表格都要处理"空 / 加载"两态）。
 * ⚠️ 加载态**不要**塞进 tbody（占位行会让"表里有几行"这类判据失真），
 * 空态才用它。
 */
export function TableEmptyRow({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <tr className="hover:bg-transparent">
      <td colSpan={colSpan} className="px-6 py-10 text-center text-sm text-text-secondary">
        {children}
      </td>
    </tr>
  );
}

export {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
};
