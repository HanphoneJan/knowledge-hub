/** Spinner —— 照搬 RAGFlow `web/src/components/ui/spin.tsx` 的观感。 */
import { Loader2 } from "lucide-react";

import { cn } from "../../lib/cn";

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 text-text-secondary text-sm", className)}>
      <Loader2 aria-hidden="true" className="size-4 animate-spin" />
      {label}
    </span>
  );
}

export function PageSpinner({ label = "加载中" }: { label?: string }) {
  return (
    <div className="flex h-full items-center justify-center">
      <Spinner label={label} />
    </div>
  );
}
