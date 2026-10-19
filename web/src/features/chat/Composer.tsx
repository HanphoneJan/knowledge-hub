/**
 * 底部**悬浮输入条** —— 对齐 Open WebUI / RAGFlow 的对话输入：
 *
 * - `rounded-3xl` + **细边框 + 一处 `shadow-lg`** + 半透明模糊底；
 * - 内部文字**垂直居中**（单行时不能顶在上边）；
 * - 右侧**圆形发送钮**：有内容时实心深色、空时灰禁用；生成中变成"停止"；
 * - Enter 发送 / Shift+Enter 换行；多行自增高（`Textarea` 的 `autoSize`）。
 */
import { ArrowUp, Square } from "lucide-react";
import * as React from "react";

import { Textarea } from "../../components/ui/textarea";
import { cn } from "../../lib/cn";

export function Composer({
  onSend,
  onStop,
  busy,
  disabled,
  placeholder,
}: {
  onSend: (text: string) => void;
  onStop: () => void;
  busy: boolean;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = React.useState("");
  const taRef = React.useRef<HTMLTextAreaElement>(null);

  const canSend = !busy && !disabled && text.trim().length > 0;

  const submit = React.useCallback(() => {
    if (!canSend) return;
    onSend(text);
    setText("");
    // 发完把焦点留在输入条：连续追问是常态
    requestAnimationFrame(() => taRef.current?.focus());
  }, [canSend, onSend, text]);

  const handleKeyDown = React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // ⚠️ 输入法组合中的 Enter 不能当发送（中文输入法选词就是 Enter）
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        submit();
      }
    },
    [submit],
  );

  return (
    <div className="shrink-0 px-3 pb-3 pt-1 sm:px-6 sm:pb-5">
      <div className="mx-auto w-full max-w-[58rem]">
        <div
          className={cn(
            "flex items-end gap-2 rounded-3xl border-[0.5px] border-border-button bg-bg-component/85 p-2 shadow-lg backdrop-blur",
            "focus-within:border-border-default",
          )}
        >
          <Textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            autoSize={{ minRows: 1, maxRows: 8 }}
            resize="none"
            rows={1}
            disabled={disabled}
            placeholder={placeholder ?? "问点什么…（Enter 发送，Shift+Enter 换行）"}
            className={cn(
              "min-h-0 flex-1 border-0 bg-transparent px-2 py-1.5 text-sm leading-6 shadow-none",
              "focus-visible:ring-0",
            )}
          />
          {busy ? (
            <button
              type="button"
              aria-label="停止生成"
              onClick={onStop}
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-text-primary text-bg-base transition-opacity hover:opacity-90"
            >
              <Square aria-hidden="true" className="size-3.5 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              aria-label="发送"
              disabled={!canSend}
              onClick={submit}
              className={cn(
                "flex size-8 shrink-0 items-center justify-center rounded-full transition-colors",
                canSend
                  ? "bg-text-primary text-bg-base hover:opacity-90"
                  : "bg-bg-card text-text-disabled",
              )}
            >
              <ArrowUp aria-hidden="true" className="size-4" />
            </button>
          )}
        </div>
        <div className="hidden px-2 pt-1.5 text-2xs text-text-secondary sm:block">
          答案由抽取式生成层拼装，每条角标都能点开核对原文；检索范围在左侧面板顶部切换。
        </div>
      </div>
    </div>
  );
}
