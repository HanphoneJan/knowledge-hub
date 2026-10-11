/**
 * 拖拽上传区（RAGFlow 的虚线框 + 图标 + 说明）。
 *
 * 契约纪律（`lib/upload.ts`）：
 * - 单请求 **≤20 份**（D45）⇒ 必须**分批**；
 * - **上传阶段不给百分比**（`fetch` 拿不到上传进度，给百分比就是编数字）；
 * - 汇总**只算这一批**，平均**排除失败**；
 * - 有失败时不能说"全部完成"。
 */
import { UploadCloud } from "lucide-react";
import * as React from "react";

import { Button } from "../../components/ui/button";
import { cn } from "../../lib/cn";

export function UploadZone({
  onFiles,
  disabled,
  busy,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  busy?: boolean;
}) {
  const [drag, setDrag] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const pick = React.useCallback(
    (list: FileList | null) => {
      if (!list || list.length === 0) return;
      onFiles(Array.from(list));
    },
    [onFiles],
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        if (!disabled) pick(e.dataTransfer.files);
      }}
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-2xl border border-dashed px-4 py-7 text-center transition-colors",
        drag ? "border-border-accent bg-bg-card" : "border-border-default",
        disabled && "opacity-60",
      )}
    >
      <UploadCloud aria-hidden="true" className="size-6 text-text-secondary" />
      <div className="text-sm text-text-primary">把文件拖到这里，或者</div>
      <Button variant="outline" size="sm" disabled={disabled || busy} onClick={() => inputRef.current?.click()}>
        {busy ? "正在上传…" : "选择文件"}
      </Button>
      <div className="text-2xs text-text-secondary">
        单次请求最多 20 份（自动分批）· 同名文件重传会把旧版本移进回收站
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          pick(e.target.files);
          // 允许连续选同一个文件两次
          e.target.value = "";
        }}
      />
    </div>
  );
}
