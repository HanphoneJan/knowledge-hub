/** 引用角标的少量纯逻辑（颜色 / 标题），与 `AnswerText` 分开以免循环依赖。 */
import type { SourceItem } from "../../types/api";

export interface CitationRef {
  idx: number;
  source: SourceItem;
}

export function toRefs(sources: SourceItem[] | undefined): CitationRef[] {
  return (sources ?? []).map((s) => ({ idx: s.idx, source: s }));
}

/** 同一条引用在回答里出现的每一处用同一个色（便于"这句话来自第 2 条"）。 */
const HUES = [190, 262, 330, 26, 150, 215, 300, 45];

export function citationColor(idx: number): { backgroundColor: string; color: string } {
  const h = HUES[(idx - 1 + HUES.length) % HUES.length];
  return {
    backgroundColor: `hsl(${h} 70% 92%)`,
    color: `hsl(${h} 55% 30%)`,
  };
}

/** 来源卡片的标题（文件名 + 角标）。 */
export function sourceHeading(s: SourceItem): string {
  const name = s.title || s.doc_id ? s.title || `文档 #${s.doc_id}` : "来源";
  return `${s.idx}. ${name}`;
}

/** 来源的时间（可能有，可能空 —— `date` 在契约里是字符串）。 */
export function sourceDate(s: SourceItem): string | null {
  const d = (s.date ?? "").trim();
  return d || null;
}
