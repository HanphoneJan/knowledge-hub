/**
 * Textarea —— 照搬 RAGFlow `web/src/components/ui/textarea.tsx`（Apache-2.0）。
 *
 * 保留它的 `autoSize`（对话输入条靠它自增高 —— Open WebUI 的观感就来自这里）。
 * 改动：`cn` 的 import；`border` → `border-[0.5px]`。
 */
import * as React from "react";

import { cn } from "../../lib/cn";

interface TextareaProps extends Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, "autoSize"> {
  autoSize?: { minRows?: number; maxRows?: number };
  resize?: "none" | "vertical" | "horizontal" | "both";
}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, autoSize, resize = "vertical", ...props }, ref) => {
    const innerRef = React.useRef<HTMLTextAreaElement>(null);
    const manualHeightRef = React.useRef<number | null>(null);
    const isAdjustingRef = React.useRef(false);

    const getLineHeight = (el: HTMLElement): number =>
      parseInt(window.getComputedStyle(el).lineHeight, 10) || 20;

    const adjustHeight = React.useCallback(() => {
      if (!innerRef.current || !autoSize) return;
      const lineHeight = getLineHeight(innerRef.current);
      const maxHeight = (autoSize.maxRows || 3) * lineHeight;
      isAdjustingRef.current = true;
      innerRef.current.style.height = "auto";
      requestAnimationFrame(() => {
        if (!innerRef.current) return;
        const desired = Math.min(innerRef.current.scrollHeight, maxHeight);
        const manual = manualHeightRef.current;
        innerRef.current.style.height = `${manual && manual > desired ? manual : desired}px`;
        isAdjustingRef.current = false;
      });
    }, [autoSize]);

    React.useEffect(() => {
      if (autoSize) adjustHeight();
    }, [autoSize, adjustHeight, props.value]);

    React.useEffect(() => {
      if (typeof ref === "function") ref(innerRef.current);
      else if (ref) (ref as React.MutableRefObject<HTMLTextAreaElement | null>).current = innerRef.current;
    }, [ref]);

    const resizable = resize !== "none";

    return (
      <textarea
        className={cn(
          "flex min-h-[80px] w-full bg-bg-input rounded-md border-[0.5px] border-border-button px-3 py-2 text-sm placeholder:text-text-disabled focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent-primary disabled:cursor-not-allowed disabled:opacity-50",
          resizable ? "overflow-auto" : "overflow-hidden",
          className,
        )}
        rows={autoSize?.minRows ?? props.rows ?? undefined}
        style={{
          maxHeight: autoSize?.maxRows && !resizable ? `${autoSize.maxRows * 20}px` : undefined,
          resize,
        }}
        ref={innerRef}
        {...props}
      />
    );
  },
);
Textarea.displayName = "Textarea";

export { Textarea };
