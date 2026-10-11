/**
 * 消息列（Open WebUI 的阅读列观感）：
 *
 * - **用户**：右对齐 `rounded-3xl` 灰底气泡（`px-4 py-1.5`，最宽 90%）；
 * - **助手**：左**头像** + 正文（**无气泡**，行高 ~1.8）；角标可点；
 * - 居中列 `max-w-[58rem]`，上下留白足。
 */
import { AlertTriangle, Copy, Square } from "lucide-react";
import * as React from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { cn } from "../../lib/cn";
import { AnswerText } from "./AnswerText";
import { toRefs } from "./citations";
import type { Turn } from "./types";

export function MessageList({
  turns,
  onCite,
  onRetry,
}: {
  turns: Turn[];
  onCite: (turnKey: string, idx: number) => void;
  onRetry?: (question: string) => void;
}) {
  return (
    <div className="flex flex-col gap-7 pb-4">
      {turns.map((t) =>
        t.role === "user" ? (
          <UserBubble key={t.key} turn={t} />
        ) : (
          <AssistantBlock key={t.key} turn={t} onCite={onCite} onRetry={onRetry} />
        ),
      )}
    </div>
  );
}

/** 助手头像：**品牌标**（而不是首字母"助"）—— Open WebUI 就是自己的标。 */
function AssistantAvatar() {
  return (
    <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full bg-bg-card text-text-primary">
      <svg aria-hidden="true" viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 1 4 16z" />
        <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5h5A1.5 1.5 0 0 0 20 16z" />
      </svg>
    </span>
  );
}

function UserBubble({ turn }: { turn: Turn }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[90%] whitespace-pre-wrap rounded-3xl bg-bg-card px-4 py-2 text-sm leading-6 text-text-primary">
        {turn.content}
      </div>
    </div>
  );
}

function AssistantBlock({
  turn,
  onCite,
  onRetry,
}: {
  turn: Turn;
  onCite: (turnKey: string, idx: number) => void;
  onRetry?: (question: string) => void;
}) {
  const refs = React.useMemo(() => toRefs(turn.sources), [turn.sources]);
  const citations = turn.citations ?? [];

  return (
    <div className="flex gap-3">
      <AssistantAvatar />
      <div className="min-w-0 flex-1">
        {turn.status === "streaming" && !turn.content && (
          <div className="flex flex-col gap-2 py-1">
            <Spinner label={turn.sources ? "已找到证据，正在生成…" : "正在检索…"} />
            {turn.sources && (
              <div className="text-2xs text-text-secondary">
                检索到 {turn.sources.length} 条证据
                {turn.retrievalMs != null ? `（${Math.round(turn.retrievalMs)}ms）` : ""}
              </div>
            )}
          </div>
        )}

        {turn.content && (
          <AnswerText
            text={turn.content}
            refs={refs}
            dangling={turn.dangling}
            onCite={(idx) => onCite(turn.key, idx)}
          />
        )}

        {turn.status === "error" && (
          <div className="mt-2 flex items-center gap-2 rounded-lg bg-state-error-5 px-2.5 py-1.5 text-13 text-state-error">
            <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">生成中断：{turn.error}</span>
            {onRetry && turn.question && (
              <Button variant="ghost" size="xs" onClick={() => onRetry(turn.question as string)}>
                重试
              </Button>
            )}
          </div>
        )}

        {turn.status === "stopped" && (
          <div className="mt-2 flex items-center gap-2 text-2xs text-text-secondary">
            <Square aria-hidden="true" className="size-3" />
            已停止生成（已生成的部分保留）
          </div>
        )}

        {turn.status === "done" && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {citations.length > 0 && (
              <button
                type="button"
                onClick={() => onCite(turn.key, citations[0])}
                className="text-2xs text-text-secondary transition-colors hover:text-text-primary"
              >
                {citations.length} 条引用 · 点角标核对原文
              </button>
            )}
            {turn.history && citations.length > 0 && (
              <Badge variant="secondary" size="sm">
                历史消息：只有角标编号
              </Badge>
            )}
            {(turn.dangling?.length ?? 0) > 0 && (
              <Badge variant="destructive" size="sm">
                悬空引用 {turn.dangling?.join(", ")}
              </Badge>
            )}
            {turn.generator && (
              <Badge variant="secondary" size="sm">
                {turn.generator}
              </Badge>
            )}
            {turn.elapsedMs != null && (
              <span className="text-2xs text-text-disabled">
                {(turn.elapsedMs / 1000).toFixed(1)}s
              </span>
            )}
            <CopyButton text={turn.content} />
          </div>
        )}
      </div>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [ok, setOk] = React.useState(false);
  return (
    <button
      type="button"
      aria-label="复制回答"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => {
            setOk(true);
            window.setTimeout(() => setOk(false), 1500);
          },
          () => setOk(false),
        );
      }}
      className={cn(
        "inline-flex size-5 items-center justify-center rounded text-text-disabled transition-colors hover:text-text-primary",
        ok && "text-state-success",
      )}
    >
      <Copy aria-hidden="true" className="size-3" />
    </button>
  );
}
