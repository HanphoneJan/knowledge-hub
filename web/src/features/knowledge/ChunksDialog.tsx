/**
 * 片段预览对话框 —— 用 `GET /documents/{id}/chunks` 看**解析结果**。
 *
 * 为什么值得有：解析质量（分栏、表格 Markdown 化、标题层级）在别处看不见，
 * 而"搜不到"十有八九是切分/解析的问题。这里能看到真实的 `chunk_index` /
 * `kind` / 坐标数，正是排查检索的起点。
 */
import { useQuery } from "@tanstack/react-query";

import { Badge } from "../../components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { EmptyState } from "../../components/ui/empty-state";
import { SkeletonRows } from "../../components/ui/skeleton";
import { api, errText } from "../../lib/api";
import { Alert } from "../../components/ui/alert";
import { Keys } from "../../app/queryKeys";
import type { DocumentRow, TableJson } from "../../types/api";

/** D203：把结构化网格渲染成**真表格**（表格 chunk 才走这条，其余仍是等宽文本）。 */
function TableGrid({ json }: { json: TableJson }) {
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-13">
        <thead>
          <tr>
            {json.header.map((h, i) => (
              <th
                key={i}
                className="border-[0.5px] border-border-button bg-bg-card px-2 py-1 text-start font-medium text-text-primary"
              >
                {h || "\u00a0"}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {json.rows.map((r, ri) => (
            <tr key={ri}>
              {r.map((cell, ci) => (
                <td
                  key={ci}
                  className="border-[0.5px] border-border-button px-2 py-1 align-top text-text-primary"
                >
                  {cell || "\u00a0"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChunksDialog({
  doc,
  onOpenChange,
}: {
  doc: DocumentRow | null;
  onOpenChange: (open: boolean) => void;
}) {
  const q = useQuery({
    queryKey: Keys.chunks(doc?.id ?? 0),
    queryFn: () => api.chunks(doc!.id, 1, 100),
    enabled: !!doc,
  });

  return (
    <Dialog open={!!doc} onOpenChange={onOpenChange}>
      <DialogContent wide className="max-h-[86vh]">
        <DialogHeader>
          <DialogTitle className="truncate">{doc?.file_name ?? "片段"}</DialogTitle>
          <DialogDescription>
            前 100 个片段（chunk_index 升序）。kind 是 chunks 的四种形态，与检索结果的引用类型不是一套。
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto pe-1">
          {q.isLoading && <SkeletonRows rows={5} />}
          {q.isError && <Alert tone="danger">{errText(q.error)}</Alert>}
          {q.data && q.data.length === 0 && (
            <EmptyState
              title="这个文档没有片段"
              description="纯扫描件 / 照片可能没有文本层 —— 它们走视觉腿，不产出 text chunk。"
            />
          )}
          {q.data && q.data.length > 0 && (
            <div className="flex flex-col gap-2">
              {q.data.map((c) => (
                <div key={c.chunk_index} className="rounded-xl border-[0.5px] border-border-button p-2.5">
                  <div className="mb-1 flex flex-wrap items-center gap-1.5">
                    <Badge variant="secondary" size="sm">
                      #{c.chunk_index}
                    </Badge>
                    <Badge variant="secondary" size="sm">
                      {c.kind}
                    </Badge>
                    {c.table_index != null && (
                      <Badge variant="secondary" size="sm">
                        表 {c.table_index}
                        {c.row_count != null && c.col_count != null
                          ? `（${c.row_count}×${c.col_count}）`
                          : ""}
                      </Badge>
                    )}
                    <span className="text-2xs text-text-secondary">
                      {c.locations?.length ?? 0} 个坐标框
                    </span>
                  </div>
                  <div className="whitespace-pre-wrap text-13 leading-relaxed text-text-primary">
                    {c.table_json ? (
                      <TableGrid json={c.table_json} />
                    ) : (
                      c.content_norm || "（空）"
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
