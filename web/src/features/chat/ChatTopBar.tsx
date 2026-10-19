/**
 * 对话页顶部**细栏**（~48px）—— 对齐 Open WebUI：左侧会话标题（单行截断、
 * 次级色），右侧小图标按钮。**不要**粗边框、不要大标题。
 */
import { BookOpen, Check, Copy, Pencil, SquarePen } from "lucide-react";
import * as React from "react";

import { Button } from "../../components/ui/button";
import { Hint } from "../../components/ui/tooltip";
import { useScope } from "../../app/scope";

export function ChatTopBar({
  title,
  onRename,
  onNew,
  onToggleCitations,
  citationsOpen,
  hasCitations,
  onCopy,
}: {
  title: string;
  onRename?: () => void;
  onNew: () => void;
  onToggleCitations: () => void;
  citationsOpen: boolean;
  hasCitations: boolean;
  onCopy?: () => void;
}) {
  const { label } = useScope();
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(() => {
    onCopy?.();
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }, [onCopy]);

  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b-[0.5px] border-border-button bg-bg-component/80 px-3 backdrop-blur">
      <div className="flex min-w-0 items-center gap-2">
        <span className="truncate text-[15px] text-text-secondary">{title}</span>
        <span className="hidden shrink-0 rounded-full bg-bg-card px-2 py-0.5 text-2xs text-text-secondary sm:inline">
          {label}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-0.5">
        {onCopy && (
          <Hint label={copied ? "已复制" : "复制这段对话"}>
            <Button variant="ghost" size="icon-sm" aria-label="复制对话" onClick={handleCopy}>
              {copied ? (
                <Check aria-hidden="true" className="size-4" />
              ) : (
                <Copy aria-hidden="true" className="size-4" />
              )}
            </Button>
          </Hint>
        )}
        {onRename && (
          <Hint label="重命名会话">
            <Button variant="ghost" size="icon-sm" aria-label="重命名会话" onClick={onRename}>
              <Pencil aria-hidden="true" className="size-4" />
            </Button>
          </Hint>
        )}
        <Hint label={citationsOpen ? "收起引用" : "查看引用来源"}>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="查看引用来源"
            disabled={!hasCitations}
            onClick={onToggleCitations}
          >
            <BookOpen aria-hidden="true" className="size-4" />
          </Button>
        </Hint>
        <Hint label="新建对话">
          <Button variant="ghost" size="icon-sm" aria-label="新建对话" onClick={onNew}>
            <SquarePen aria-hidden="true" className="size-4" />
          </Button>
        </Hint>
      </div>
    </header>
  );
}
