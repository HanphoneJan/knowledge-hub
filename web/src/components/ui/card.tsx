/**
 * Card —— 照搬 RAGFlow `web/src/components/ui/card.tsx`（Apache-2.0）。
 *
 * 改动：`cn` 的 import 路径。⚠️ RAGFlow 的 `CardHeader` 自带 `p-6`，
 * 而本项目的卡是**紧凑卡**（`p-3.5`）—— 调用处用 `className` 覆盖即可
 * （`cn()` 的 tailwind-merge 会让后者胜）。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

const Card = React.forwardRef<
  HTMLElement,
  React.HTMLAttributes<HTMLElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As
    ref={ref}
    className={cn(
      "rounded-2xl border-[0.5px] border-border-button bg-bg-component shadow-sm",
      className,
    )}
    {...props}
  />
));
Card.displayName = "Card";

const CardHeader = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As ref={ref} className={cn("flex flex-col space-y-1.5 p-4", className)} {...props} />
));
CardHeader.displayName = "CardHeader";

const CardTitle = React.forwardRef<
  HTMLElement,
  React.HTMLAttributes<HTMLElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As ref={ref} className={cn("text-base font-medium leading-normal", className)} {...props} />
));
CardTitle.displayName = "CardTitle";

const CardDescription = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As ref={ref} className={cn("text-sm text-text-secondary", className)} {...props} />
));
CardDescription.displayName = "CardDescription";

const CardContent = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As ref={ref} className={cn("p-4 pt-0", className)} {...props} />
));
CardContent.displayName = "CardContent";

const CardFooter = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { as?: React.ElementType }
>(({ as: As = "div", className, ...props }, ref) => (
  <As ref={ref} className={cn("flex items-center p-4 pt-0", className)} {...props} />
));
CardFooter.displayName = "CardFooter";

export { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle };
