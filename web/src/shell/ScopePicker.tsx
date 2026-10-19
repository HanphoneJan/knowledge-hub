/**
 * 库范围选择器（二级面板顶部的那一行）。
 *
 * 🔑 判定全在 `lib/kbScope.ts`（纯函数 + 20 条单测）；这里只负责渲染与点击。
 * 文案："全部库（4）"是刻意的 —— 只写"全部"时用户不知道搜了几个库
 * （`scopeLabel` 的注释里写了为什么）。
 */
import { Check, ChevronDown, Layers } from "lucide-react";
import * as React from "react";

import { useScope } from "../app/scope";
import { cn } from "../lib/cn";

export function ScopePicker() {
  const { kbs, kbsLoading, scope, setScope, toggle, isSelected, all, label } = useScope();
  const [open, setOpen] = React.useState(false);

  return (
    <div className="border-b-[0.5px] border-border-button p-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-start transition-colors hover:bg-bg-card"
      >
        <Layers aria-hidden="true" className="size-4 shrink-0 text-text-secondary" />
        <span className="min-w-0 flex-1">
          <span className="block text-2xs text-text-secondary">检索范围</span>
          <span className="block truncate text-13 text-text-primary">{label}</span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={cn("size-3.5 shrink-0 text-text-secondary transition-transform", open && "rotate-180")}
        />
      </button>

      {open && (
        <div className="mt-1 flex flex-col gap-0.5 rounded-lg border-[0.5px] border-border-button p-1">
          <button
            type="button"
            onClick={() => setScope(null)}
            className={cn(
              "flex items-center gap-2 rounded-md px-2 py-1.5 text-start text-13 transition-colors hover:bg-bg-card",
              all ? "text-text-primary" : "text-text-secondary",
            )}
          >
            <CheckBox on={all} />
            <span className="min-w-0 flex-1 truncate">全部可见知识库</span>
            <span className="text-2xs text-text-secondary">{kbs.length}</span>
          </button>
          {kbsLoading && <div className="px-2 py-1.5 text-2xs text-text-secondary">正在加载…</div>}
          {!kbsLoading && kbs.length === 0 && (
            <div className="px-2 py-1.5 text-2xs text-text-secondary">
              还没有知识库 —— 去「知识库」建一个
            </div>
          )}
          {kbs.map((k) => (
            <button
              key={k.id}
              type="button"
              onClick={() => toggle(k.id)}
              className={cn(
                "flex items-center gap-2 rounded-md px-2 py-1.5 text-start text-13 transition-colors hover:bg-bg-card",
                isSelected(k.id) ? "text-text-primary" : "text-text-secondary",
              )}
            >
              <CheckBox on={isSelected(k.id)} />
              <span className="min-w-0 flex-1 truncate">{k.name}</span>
              <span className="shrink-0 text-2xs text-text-secondary">{k.source_kind}</span>
            </button>
          ))}
          {scope !== null && scope.length > 0 && (
            <div className="px-2 pt-1 text-2xs text-text-secondary">
              ⚠️ 这里显示的只是「当前页能看到的库」；选「全部」时由服务端推导（D8）。
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CheckBox({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex size-3.5 shrink-0 items-center justify-center rounded-[4px] border-[0.5px] border-border-default",
        on ? "bg-text-primary text-bg-base" : "bg-transparent",
      )}
    >
      {on && <Check className="size-3" />}
    </span>
  );
}
