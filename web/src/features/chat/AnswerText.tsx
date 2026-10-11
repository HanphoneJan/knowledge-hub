/**
 * 助手回答的正文渲染：把 `[n]` 角标变成**可点的小标**（Open WebUI 的 `[n]` 观感）。
 *
 * ⚠️ 句尾角标的编号是**证据序号**（1-based，指向 SSE `sources`），
 * 与 `CITATION_RE = \[(\d+)\]`（`services/retrieval/llm.py`）同一套语法 ——
 * 前端只认这一种形状，别的写法（`[1,2]`、`[1-3]`）后端不产出。
 *
 * 🔑 **不引 markdown 渲染器**：默认生成层是抽取式的，正文是纯文本句子，
 * 引一个渲染器只会带来 XSS 面与包体，换不到观感。
 */
import * as React from "react";

import { citationColor, type CitationRef } from "./citations";

const CITE_RE = /\[(\d+)\]/g;

export interface AnswerSegment {
  text: string;
  /** 这一段是角标编号；`text` 为空。 */
  cite?: number;
}

/** 把正文切成「文本 / 角标」交替的段（纯函数，便于单测推演）。 */
export function splitAnswer(text: string): AnswerSegment[] {
  const out: AnswerSegment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  CITE_RE.lastIndex = 0;
  while ((m = CITE_RE.exec(text)) !== null) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ text: "", cite: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}

export function AnswerText({
  text,
  refs,
  dangling,
  onCite,
}: {
  text: string;
  /** 可核验的来源（实时回答才有）。 */
  refs?: CitationRef[];
  dangling?: number[];
  onCite?: (idx: number) => void;
}) {
  const segs = React.useMemo(() => splitAnswer(text), [text]);
  return (
    <div className="whitespace-pre-wrap text-sm leading-[1.8] text-text-primary">
      {segs.map((s, i) =>
        s.cite == null ? (
          <React.Fragment key={i}>{s.text}</React.Fragment>
        ) : (
          <CiteChip
            key={i}
            idx={s.cite}
            refs={refs}
            dangling={dangling}
            onCite={onCite}
          />
        ),
      )}
    </div>
  );
}

function CiteChip({
  idx,
  refs,
  dangling,
  onCite,
}: {
  idx: number;
  refs?: CitationRef[];
  dangling?: number[];
  onCite?: (idx: number) => void;
}) {
  const isDangling = (dangling ?? []).includes(idx);
  const known = refs?.some((r) => r.idx === idx);
  const clickable = !isDangling && !!known && !!onCite;
  const title = isDangling
    ? `悬空引用：答案引用了不存在的第 ${idx} 条证据（后端 done 帧也报了）`
    : known
      ? `第 ${idx} 条证据`
      : `历史消息只保存了角标编号，没有来源内容（引用需重新提问核验）`;

  // 角标配色：可核验的用同一族的浅色（第 n 条 = 第 n 个色相），
  // 不可核验 / 悬空的用中性 / 危险色 —— 颜色本身就是"这条能不能点"的信号。
  const tone = isDangling
    ? "bg-state-error-5 text-state-error"
    : known
      ? "hover:brightness-95"
      : "bg-bg-card text-text-disabled";

  return (
    <button
      type="button"
      disabled={!clickable}
      title={title}
      onClick={() => clickable && onCite?.(idx)}
      style={isDangling || !known ? undefined : citationColor(idx)}
      className={
        "mx-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 align-[text-bottom] text-[11px] font-medium transition-[filter] " +
        tone
      }
    >
      {idx}
    </button>
  );
}
