/**
 * RichText —— 渲染系统提示文案里的 `**粗体**`，并**去掉装饰性 emoji**。
 *
 * 为什么需要它（CONVENTIONS §10.5）：后端把"可操作的修复指引"当自然语言
 * 返回（带 `**加粗**` 和 🔑/⚠️/🔴 这类装饰 emoji），而页面**没有** markdown
 * 渲染器 —— 直接插进 DOM 会原样显示星号。
 *
 * 只支持 `**bold**` 一种内联标记。**不引入 markdown 库**（内网自部署，
 * 一个渲染器为一条提示太贵）。
 */
import * as React from "react";

/** 装饰性 emoji（不进 UI）：🔑⚠️🔴🟡✅❌🎯⇒ 等 —— 换成 lucide 图标或纯文字的地方由调用方决定。 */
const EMOJI_RE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{20E3}]/gu;

/** 原生 `window.confirm/prompt` 里用的纯文本（去掉 markdown 标记与 emoji）。 */
export function plainText(s: string): string {
  return s.replace(/\*\*/g, "").replace(EMOJI_RE, "").replace(/[ \t]+\n/g, "\n").trim();
}

export function RichText({ text, className }: { text: string; className?: string }) {
  const parts = React.useMemo(() => splitBold(text.replace(EMOJI_RE, "")), [text]);
  return (
    <span className={className}>
      {parts.map((p, i) =>
        p.bold ? (
          <strong key={i} className="font-semibold text-text-primary">
            {p.text}
          </strong>
        ) : (
          <React.Fragment key={i}>{p.text}</React.Fragment>
        ),
      )}
    </span>
  );
}

function splitBold(text: string): Array<{ text: string; bold: boolean }> {
  const out: Array<{ text: string; bold: boolean }> = [];
  const re = /\*\*([^*]+)\*\*/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ text: text.slice(last, m.index), bold: false });
    out.push({ text: m[1], bold: true });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), bold: false });
  return out.length ? out : [{ text, bold: false }];
}
