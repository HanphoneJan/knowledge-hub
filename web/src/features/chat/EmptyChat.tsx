/**
 * 对话空态：**品牌标 + 大标题 + 一句说明 + 建议卡网格**（Open WebUI 的观感）。
 * 整体在内容区**垂直居中**。
 */
import { ArrowUpRight } from "lucide-react";
import * as React from "react";

import { useScope } from "../../app/scope";

/** 建议问题：带一点上下文（当前范围里有几个库），点一下直接发出去。 */
export function suggestions(label: string): Array<{ title: string; body: string }> {
  return [
    { title: "总结一下这些资料", body: `在「${label}」范围内，把主要结论用一段话总结出来。` },
    { title: "找关键结论", body: "有哪些文档提到了「验收标准」？把原文片段列出来。" },
    { title: "按时间梳理", body: "按时间顺序列出最近的重要事项，并标注来自哪份文件。" },
    { title: "对比两个说法", body: "这些资料里对同一个问题的说法有没有互相矛盾的地方？" },
  ];
}

export function EmptyChat({ onPick }: { onPick: (q: string) => void }) {
  const { label } = useScope();
  const cards = React.useMemo(() => suggestions(label), [label]);

  return (
    <div className="flex flex-1 items-center justify-center px-4 py-10">
      <div className="flex w-full max-w-[58rem] flex-col items-center gap-6">
        <div className="flex size-14 items-center justify-center rounded-2xl bg-text-primary text-bg-base">
          <svg aria-hidden="true" viewBox="0 0 24 24" className="size-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 1 4 16z" />
            <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5h5A1.5 1.5 0 0 0 20 16z" />
          </svg>
        </div>
        <div className="flex flex-col items-center gap-2 text-center">
          <h1 className="text-2xl font-semibold text-text-primary">有什么可以帮你查的？</h1>
          <p className="text-sm text-text-secondary">
            答案只来自你选中的知识库，并且每一条引用都能点开核对原文。
          </p>
        </div>
        <div className="grid w-full grid-cols-1 gap-2.5 sm:grid-cols-2">
          {cards.map((c) => (
            <button
              key={c.title}
              type="button"
              onClick={() => onPick(c.body)}
              className="group flex flex-col items-start gap-1 rounded-2xl border-[0.5px] border-border-button bg-bg-component p-3.5 text-start transition-colors hover:border-border-default hover:bg-bg-card"
            >
              <div className="flex w-full items-center justify-between gap-2">
                <span className="text-sm font-medium text-text-primary">{c.title}</span>
                <ArrowUpRight
                  aria-hidden="true"
                  className="size-4 shrink-0 text-text-disabled transition-colors group-hover:text-text-primary"
                />
              </div>
              <span className="text-13 text-text-secondary">{c.body}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
