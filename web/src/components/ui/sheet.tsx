/**
 * Sheet —— 侧边抽屉（Radix Dialog 的变体，shadcn 写法）。
 *
 * 用途：**移动端（<768px）的二级面板**（D162 信息架构 / D189 重写）。
 * ⚠️ 桌面与移动的二级面板**互斥渲染**（同一控件不能在 DOM 里出现两次）。
 */
import * as SheetPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";

const Sheet = SheetPrimitive.Root;
const SheetTrigger = SheetPrimitive.Trigger;
const SheetClose = SheetPrimitive.Close;

const SheetContent = React.forwardRef<
  React.ComponentRef<typeof SheetPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SheetPrimitive.Content> & { title: string; side?: "start" | "end" }
>(({ className, children, title, side = "start", ...props }, ref) => (
  <SheetPrimitive.Portal>
    <SheetPrimitive.Overlay className="fixed inset-0 z-50 bg-scrim animate-overlay-in" />
    <SheetPrimitive.Content
      ref={ref}
      className={cn(
        "fixed inset-y-0 z-50 flex w-[300px] max-w-[86vw] flex-col border-border-button bg-bg-component shadow-lg",
        side === "start" ? "start-0 border-e-[0.5px] animate-sheet-in-start" : "end-0 border-s-[0.5px] animate-sheet-in-end",
        className,
      )}
      {...props}
    >
      <div className="flex h-12 shrink-0 items-center justify-between border-b-[0.5px] border-border-button px-3">
        <SheetPrimitive.Title className="text-sm font-medium text-text-primary">
          {title}
        </SheetPrimitive.Title>
        <SheetPrimitive.Close
          aria-label="关闭"
          className="rounded-md p-1 text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
        >
          <X aria-hidden="true" className="size-4" />
        </SheetPrimitive.Close>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </SheetPrimitive.Content>
  </SheetPrimitive.Portal>
));
SheetContent.displayName = SheetPrimitive.Content.displayName;

export { Sheet, SheetClose, SheetContent, SheetTrigger };
