/**
 * 文档表格 —— RAGFlow 的表格观感（表头浅底 + 行 hover + 首末列缩进）。
 *
 * 🔑 **状态与进度分开**（`lib/progress.ts`）：
 * - 状态是"成了吗"（`documents.status`）；
 * - 进度是"到哪了"（`jobs.progress`），**百分比只能来自它**；
 * - 没有 jobs 行 ⇒ 进度列什么也不显示（不是 0%）；`done`/`failed` 不画条。
 */
import { Eye, MoreHorizontal } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Hint } from "../../components/ui/tooltip";
import { Menu } from "../../components/ui/menu";
import { Progress } from "../../components/ui/progress";
import { Table, TableBody, TableCell, TableEmptyRow, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { type ProgressView } from "../../lib/progress";
import { formatBytes, expiryLabel, trashDeletedAt } from "../../lib/trash";
import { cn } from "../../lib/cn";
import type { DocumentRow } from "../../types/api";

/** 文档状态的胶囊（圆点 + 文字）。 */
export function StatusPill({ doc, view }: { doc: DocumentRow; view: ProgressView | null }) {
  const tone = view?.tone ?? (doc.status === "ready" ? "ready" : doc.status === "failed" ? "failed" : "working");
  const variant = tone === "ready" ? "success" : tone === "failed" ? "destructive" : tone === "cancelled" ? "secondary" : "warning";
  return (
    <Badge variant={variant} dot>
      {doc.status}
    </Badge>
  );
}

/** 进度格：没有 job 就**什么都不显示**；终态不画条。 */
export function ProgressCell({ view }: { view: ProgressView | null }) {
  if (!view) return <span className="text-2xs text-text-disabled">—</span>;
  return (
    <div className="flex min-w-[10rem] flex-col gap-1">
      <span
        className={cn(
          "text-2xs",
          view.tone === "failed" ? "text-state-error" : "text-text-secondary",
        )}
      >
        {view.label}
      </span>
      {view.showBar && <Progress value={view.pct / 100} tone={view.tone} label={view.label} />}
    </div>
  );
}

export function DocsTable({
  rows,
  views,
  onRetry,
  onReparse,
  onDelete,
  onChunks,
  onRestore,
  trash,
}: {
  rows: DocumentRow[];
  views: Map<number, ProgressView | null>;
  onRetry?: (d: DocumentRow) => void;
  onReparse?: (d: DocumentRow) => void;
  onDelete?: (d: DocumentRow) => void;
  onChunks?: (d: DocumentRow) => void;
  onRestore?: (d: DocumentRow) => void;
  trash?: boolean;
}) {
  const colspan = trash ? 5 : 6;
  return (
    <Table rootClassName="rounded-2xl">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>文件</TableHead>
          <TableHead className="w-[7rem]">状态</TableHead>
          {!trash && <TableHead className="w-[14rem]">进度</TableHead>}
          <TableHead className="w-[8rem]">{trash ? "删除时间" : "片段 / 页"}</TableHead>
          <TableHead className="w-[10rem]">{trash ? "还能恢复" : "上传时间"}</TableHead>
          <TableHead className="w-[4rem] text-end">操作</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.length === 0 && (
          <TableEmptyRow colSpan={colspan}>
            {trash ? "回收站是空的" : "还没有文档 —— 把文件拖到上面的虚线框里"}
          </TableEmptyRow>
        )}
        {rows.map((d) => {
          const view = views.get(d.id) ?? null;
          return (
            <TableRow key={d.id}>
              <TableCell>
                <div className="flex min-w-0 flex-col">
                  <span className="truncate text-sm text-text-primary" title={d.file_name}>
                    {d.file_name}
                  </span>
                  <span className="text-2xs text-text-secondary">
                    {d.mime_type ?? "未知类型"} · {formatBytes(d.size_bytes)}
                    {d.status_msg ? ` · ${d.status_msg}` : ""}
                  </span>
                </div>
              </TableCell>
              <TableCell>
                <StatusPill doc={d} view={view} />
              </TableCell>
              {!trash && (
                <TableCell>
                  <ProgressCell view={view} />
                </TableCell>
              )}
              <TableCell className="text-2xs text-text-secondary">
                {trash ? (
                  <span>{trashDeletedAt(d)}</span>
                ) : (
                  <span>
                    {d.chunk_count} 片段{d.page_count != null ? ` · ${d.page_count} 页` : ""}
                    {d.is_scanned ? " · 扫描件" : ""}
                  </span>
                )}
              </TableCell>
              <TableCell className="text-2xs text-text-secondary">
                {trash ? expiryLabel(d.deleted_at) : new Date(d.created_at).toLocaleString()}
              </TableCell>
              <TableCell className="text-end">
                <div className="flex items-center justify-end gap-0.5">
                  {onChunks && !trash && d.chunk_count > 0 && (
                    <Hint label="查看片段（解析结果）">
                      <button
                        type="button"
                        aria-label={`查看 ${d.file_name} 的片段`}
                        onClick={() => onChunks(d)}
                        className="flex size-7 items-center justify-center rounded-md text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
                      >
                        <Eye aria-hidden="true" className="size-3.5" />
                      </button>
                    </Hint>
                  )}
                  {trash && onRestore ? (
                    <button
                      type="button"
                      onClick={() => onRestore(d)}
                      className="rounded-md px-2 py-1 text-2xs text-text-primary transition-colors hover:bg-bg-card"
                    >
                      恢复
                    </button>
                  ) : (
                    <Menu
                      items={[
                        ...(d.status === "failed" && onRetry
                          ? [{ key: "retry", label: "重试摄取", onSelect: () => onRetry(d) }]
                          : []),
                        ...(onReparse
                          ? [{ key: "reparse", label: "重新解析", onSelect: () => onReparse(d) }]
                          : []),
                        ...(onDelete
                          ? [
                              {
                                key: "delete",
                                label: "删除（软删）",
                                danger: true,
                                onSelect: () => onDelete(d),
                              },
                            ]
                          : []),
                      ]}
                      trigger={<MoreHorizontal aria-hidden="true" className="size-4" />}
                    />
                  )}
                </div>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
