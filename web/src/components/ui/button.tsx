/**
 * Button —— 照搬 RAGFlow `web/src/components/ui/button.tsx`
 * （Apache-2.0，见 `web/THIRD-PARTY-NOTICES.md`）。
 *
 * 改动只有三处（D188：允许逐字照搬，只改 import 接我们自己的数据）：
 * 1. `@/lib/utils` → 我们的 `../../lib/cn`；
 * 2. 去掉 `react-router` 的 `asLink`（我们的按钮不承载路由）；
 * 3. `border-0.5` → `border-[0.5px]`（Tailwind v4 的数字边框 utility 不收小数）。
 */
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";

const buttonVariants = cva(
  cn(
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm transition-colors outline-none",
    "disabled:pointer-events-none disabled:opacity-50 rounded border-[0.5px] border-transparent",
    '[&_svg]:pointer-events-none [&_svg:not([class*="size-"])]:size-4 shrink-0 [&_svg]:shrink-0',
  ),
  {
    variants: {
      variant: {
        // Solid variant series：按钮自带底色，可能有边框
        default:
          "bg-text-primary text-bg-base shadow-xs hover:bg-text-primary/90 focus-visible:bg-text-primary/90",

        secondary: `
          bg-bg-card
          hover:text-text-primary hover:bg-border-button
          focus-visible:text-text-primary focus-visible:bg-border-button
        `,

        highlighted: `
          bg-text-primary text-bg-base border-b-4 border-b-accent-primary
          hover:bg-text-primary/90 focus-visible:bg-text-primary/90
        `,

        accent: `
          bg-accent-primary text-white
          hover:bg-accent-primary/90 focus-visible:bg-accent-primary/90
        `,

        destructive: `
          bg-state-error text-white shadow-xs
          hover:bg-state-error/90 focus-visible:ring-state-error/20 dark:focus-visible:ring-state-error/40
        `,

        // Outline variant series：透明或浅灰底，可能有边框
        outline: `
          text-text-secondary bg-bg-input border-[0.5px] border-border-button
          hover:text-text-primary hover:bg-border-button hover:border-border-default
          focus-visible:text-text-primary focus-visible:bg-border-button focus-visible:border-border-button
        `,

        dashed: `
          text-text-secondary border-border-button border-dashed
          hover:text-text-primary hover:bg-border-button hover:border-border-default
          focus-visible:text-text-primary focus-visible:bg-border-button focus-visible:border-border-button
        `,

        icon: "bg-transparent text-foreground hover:bg-transparent/80",

        transparent: `
          text-text-secondary bg-transparent border-[0.5px] border-border-button
          hover:text-text-primary hover:bg-border-button
          focus-visible:text-text-primary focus-visible:bg-border-button focus-visible:border-border-button
        `,

        danger: `
          bg-transparent border border-state-error text-state-error
          hover:bg-state-error/10 focus-visible:bg-state-error/10
        `,

        "danger-hover": `
          bg-bg-input border-border-button
          hover:bg-state-error/10 focus-visible:bg-state-error/10
          hover:text-state-error focus-visible:text-state-error
          hover:border-state-error focus-visible:border-state-error
        `,

        // Ghost variant series：透明底、无边框
        ghost: `
          text-text-secondary
          hover:bg-border-button focus-visible:bg-border-button
          hover:text-text-primary focus-visible:text-text-primary
        `,

        delete: `
          text-text-secondary
          hover:bg-state-error-5 hover:text-state-error
          focus-visible:text-state-error focus-visible:bg-state-error-5
        `,

        link: "text-accent-primary underline-offset-4 hover:underline",

        // Static：无交互态变化
        static: "text-text-secondary hover:text-text-primary focus-visible:text-text-primary",
      },
      size: {
        auto: "",

        xl: "h-12 rounded-xl px-5 gap-3",
        lg: "h-10 rounded-lg px-4",
        default: "h-8 rounded px-3",
        sm: "h-7 rounded-sm px-2 gap-1",
        xs: "h-6 rounded-xs px-1 gap-0.5",

        "icon-xl": "size-12 rounded-xl",
        "icon-lg": "size-10 rounded-lg",
        icon: "size-8 rounded",
        "icon-sm": "size-7 rounded-sm",
        "icon-xs": "size-6 rounded-xs",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export type ButtonVariants = VariantProps<typeof buttonVariants>;

export type ButtonProps = {
  asChild?: boolean;
  loading?: boolean;
  block?: boolean;
  dot?: boolean;
} & ButtonVariants &
  React.ButtonHTMLAttributes<HTMLButtonElement>;

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { children, className, variant, size, dot = false, asChild = false, loading = false, disabled = false, block = false, ...props },
  ref,
) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={cn(
        buttonVariants({ variant, size, className }),
        { "w-full": block },
        { relative: dot },
      )}
      ref={ref}
      disabled={loading || disabled}
      {...props}
    >
      <>
        {dot && (
          <span className="absolute size-[6px] rounded-full -right-[3px] -top-[3px] bg-state-error animate" />
        )}
        {loading && <Loader2 className="animate-spin" />}
        {children}
      </>
    </Comp>
  );
});

Button.displayName = "Button";

export const ButtonLoading = Button;

export { Button, buttonVariants };
