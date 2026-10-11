/**
 * 引用抽屉 —— **非模态**（CONVENTIONS §10.5：`fixed` 的 `aside`，不加遮罩、
 * 不加焦点陷阱）⇒ 打开时后面仍要能操作（继续追问、翻检索结果）。
 *
 * 内容：证据列表 → 选中一条 → 页图 + 框（`PageOverlay`）+ 原文 + 元信息。
 *
 * ⚠️ 历史消息没有来源内容（`chat_messages` 只存角标编号）⇒ 抽屉里如实说明，
 * 不给一个"看起来能点但点不开"的空壳。
 */
import { BookOpen, ExternalLink, X } from "lucide-react";
import * as React from "react";

import { api, errText } from "../../lib/api";
import { Alert } from "../../components/ui/alert";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import type { SourceItem } from "../../types/api";
import { PageOverlay } from "./PageOverlay";
import { sourceDate, toRefs } from "./citations";

export function CitationDrawer({
  open,
  onClose,
  sources,
  focusIdx,
  history,
}: {
  open: boolean;
  onClose: () => void;
  sources?: SourceItem[];
  /** 点角标时高亮/滚动到的那一条。 */
  focusIdx: number | null;
  /** 历史消息：没有来源（只能说明）。 */
  history?: boolean;
}) {
  const refs = React.useMemo(() => toRefs(sources), [sources]);
  const [active, setActive] = React.useState<number | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setActive(focusIdx ?? refs[0]?.idx ?? null);
  }, [open, focusIdx, refs]);

  if (!open) return null;

  const current = refs.find((r) => r.idx === active)?.source ?? null;

  return (
    <aside
      aria-label="引用来源"
      className="fixed inset-y-0 end-0 z-40 flex w-[min(420px,92vw)] flex-col border-s-[0.5px] border-border-button bg-bg-component shadow-lg"
    >
      <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b-[0.5px] border-border-button px-3">
        <div className="flex items-center gap-2 text-sm text-text-primary">
          <BookOpen aria-hidden="true" className="size-4 text-text-secondary" />
          引用来源
          {refs.length > 0 && <span className="text-text-secondary">（{refs.length}）</span>}
        </div>
        <Button variant="ghost" size="icon-sm" aria-label="关闭引用" onClick={onClose}>
          <X aria-hidden="true" className="size-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {history && (
          <Alert tone="info" className="mb-3">
            这是历史消息：后端只保存了角标编号（chat_messages.citations），没有存来源身份 ——
            所以这里点不开原文。要核验请重新提问一次。
          </Alert>
        )}

        {!history && refs.length === 0 && (
          <EmptyState
            icon={BookOpen}
            title="这一轮没有引用"
            description="模型这次没有标注角标，或者检索没有召回证据。"
          />
        )}

        {refs.length > 0 && (
          <div className="flex flex-col gap-1.5">
            {refs.map(({ idx, source }) => (
              <button
                key={idx}
                type="button"
                onClick={() => setActive(idx)}
                className={
                  "flex w-full flex-col gap-1 rounded-lg border-[0.5px] px-2.5 py-2 text-start transition-colors " +
                  (active === idx
                    ? "border-border-default bg-bg-card"
                    : "border-transparent hover:bg-bg-card")
                }
              >
                <div className="flex items-center gap-2">
                  <span className="flex size-[18px] shrink-0 items-center justify-center rounded-full bg-cite/15 text-[11px] font-medium text-cite">
                    {idx}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-13 text-text-primary">
                    {source.title || `文档 #${source.doc_id}`}
                  </span>
                  <Badge variant="secondary" size="sm">
                    {source.kind}
                  </Badge>
                </div>
                <div className="line-clamp-2 text-2xs text-text-secondary">
                  {source.text?.slice(0, 160) || "（没有正文）"}
                </div>
              </button>
            ))}
          </div>
        )}

        {current && (
          <div className="mt-3 flex flex-col gap-3 border-t-[0.5px] border-border-button pt-3">
            <div className="flex flex-wrap items-center gap-2 text-2xs text-text-secondary">
              <span className="truncate">{current.title || `文档 #${current.doc_id}`}</span>
              <span>· chunk #{current.chunk_index}</span>
              {sourceDate(current) && <span>· {sourceDate(current)}</span>}
              <span>· score {current.score.toFixed(4)}</span>
            </div>

            <PageOverlay
              docId={current.doc_id}
              kind={current.kind}
              locations={current.locations}
              fileName={current.title || `文档 #${current.doc_id}`}
            />

            <div className="flex flex-col gap-1">
              <div className="text-2xs text-text-secondary">命中的正文（≤800 字，D77）</div>
              <div className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg bg-bg-card p-2.5 text-13 leading-relaxed text-text-primary">
                {current.text || "（后端没有返回正文）"}
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  // ⚠️ 不能直接 `window.open('/api/v1/documents/…/preview')`：
                  // 新标签页带不了 Bearer 头 ⇒ 只会开到一张 401 页。
                  // 必须先在当前上下文 fetch 成 blob，再把 objectURL 开出去。
                  void api
                    .photoBlob(current.doc_id)
                    .then((blob) => {
                      const url = URL.createObjectURL(blob);
                      window.open(url, "_blank", "noopener");
                      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
                    })
                    .catch((e: unknown) => window.alert(errText(e)));
                }}
              >
                <ExternalLink aria-hidden="true" className="size-3.5" />
                在新标签页打开原件
              </Button>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}
