/**
 * 回收站分区（D26/D42/D125/D129）—— 库与工作区的软删恢复 + 清空。
 *
 * 🔑 判定全在 `lib/containerTrash.ts`（三态：restore / expired / blocked）：
 * - `expired`（过了 30 天）**不给能点的按钮** —— 数据随时会被 cleanup 清掉，
 *   给按钮就是在骗人；
 * - `blocked`（工作区也在回收站里）说清"先恢复工作区"，而不是让用户点一下拿 403。
 *
 * 🔴 「清空」是本项目**唯一不可撤销**的端点：先 `dry_run` 拿预览，再确认。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArchiveRestore, Trash2 } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog";
import { EmptyState } from "../../../components/ui/empty-state";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { api, errText } from "../../../lib/api";
import {
  canRestore,
  emptyHint,
  restoreState,
  restoredHint,
  type TrashKind,
} from "../../../lib/containerTrash";
import { confirmText, expiryLabel, freedHint, type PurgeResp } from "../../../lib/trash";
import { Keys } from "../../../app/queryKeys";
import type { Kb, Workspace } from "../../../types/api";

export function TrashSection() {
  const qc = useQueryClient();
  const [err, setErr] = React.useState<string | null>(null);
  const [note, setNote] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<PurgeResp | null>(null);

  const kbQ = useQuery({ queryKey: Keys.kbTrash(), queryFn: () => api.kbTrash() });
  const wsQ = useQuery({ queryKey: Keys.workspaceTrash(), queryFn: () => api.workspaceTrash() });

  const kbs = kbQ.data ?? [];
  const wss = wsQ.data ?? [];
  const deletedWsIds = z(wss);

  const invalidate = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: Keys.kbTrash() });
    void qc.invalidateQueries({ queryKey: Keys.workspaceTrash() });
    void qc.invalidateQueries({ queryKey: Keys.kbs() });
    void qc.invalidateQueries({ queryKey: Keys.workspaces() });
  }, [qc]);

  const restoreKbM = useMutation({
    mutationFn: (id: number) => api.restoreKb(id),
    onSuccess: () => {
      setErr(null);
      setNote(restoredHint("kb"));
      invalidate();
    },
    onError: (e) => setErr(errText(e)),
  });

  const restoreWsM = useMutation({
    mutationFn: (id: number) => api.restoreWorkspace(id),
    onSuccess: () => {
      setErr(null);
      setNote(restoredHint("workspace"));
      invalidate();
    },
    onError: (e) => setErr(errText(e)),
  });

  const dryM = useMutation({
    mutationFn: () => api.purgeTrash({ dryRun: true }),
    onSuccess: (r) => setPreview(r),
    onError: (e) => setErr(errText(e)),
  });
  const purgeM = useMutation({
    mutationFn: () => api.purgeTrash({}),
    onSuccess: () => {
      setPreview(null);
      setNote("回收站已清空。");
      invalidate();
    },
    onError: (e) => setErr(errText(e)),
  });

  const total = kbs.length + wss.length;

  return (
    <div className="flex flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && (
        <Alert tone="success" actions={<Button variant="ghost" size="xs" onClick={() => setNote(null)}>知道了</Button>}>
          {note}
        </Alert>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2 p-4 pb-2">
          <div className="flex flex-col gap-1">
            <CardTitle>回收站</CardTitle>
            <CardDescription>
              软删的库与工作区保留 30 天（D26/D42）。恢复一个库会连带重新摄取它的文档。
            </CardDescription>
          </div>
          <Button
            variant="danger"
            disabled={total === 0}
            loading={dryM.isPending}
            onClick={() => dryM.mutate()}
          >
            <Trash2 aria-hidden="true" className="size-3.5" />
            清空回收站
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 p-4 pt-0">
          {kbQ.isLoading || wsQ.isLoading ? (
            <SkeletonRows rows={3} />
          ) : total === 0 ? (
            <EmptyState size="sm" icon={Trash2} title="回收站是空的" description={emptyHint()} />
          ) : (
            <>
              <TrashGroup
                title="知识库"
                kind="kb"
                rows={kbs}
                deletedWsIds={deletedWsIds}
                onRestore={(id) => restoreKbM.mutate(id)}
              />
              <TrashGroup
                title="工作区"
                kind="workspace"
                rows={wss}
                deletedWsIds={deletedWsIds}
                onRestore={(id) => restoreWsM.mutate(id)}
              />
            </>
          )}
          <div className="text-2xs text-text-secondary">
            ⚠️ 「清空」的范围是我能读且能写的全部库（不传 kb_id），所以预览里的份数可能比你看到的多。
          </div>
        </CardContent>
      </Card>

      <Dialog open={preview != null} onOpenChange={(v) => !v && setPreview(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>清空回收站</DialogTitle>
            <DialogDescription>
              预览来自 dry_run；这一步不可撤销 —— 30 天的反悔窗口在这里结束。
            </DialogDescription>
          </DialogHeader>
          {preview && (
            <div className="flex flex-col gap-2">
              <div className="whitespace-pre-wrap text-sm text-text-primary">
                {confirmText(preview, "全部可见范围")}
              </div>
              {freedHint(preview) && <div className="text-2xs text-text-secondary">{freedHint(preview)}</div>}
              <div className="text-2xs text-text-secondary">
                {preview.docs} 份文档 · {preview.chunks} 个片段 · 释放 {preview.bytes_freed} 字节
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPreview(null)}>
              取消
            </Button>
            <Button variant="destructive" loading={purgeM.isPending} onClick={() => purgeM.mutate()}>
              确认清空
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function z(wss: Workspace[]): number[] {
  return wss.map((w) => w.id);
}

function TrashGroup({
  title,
  kind,
  rows,
  deletedWsIds,
  onRestore,
}: {
  title: string;
  kind: TrashKind;
  rows: Array<Kb | Workspace>;
  deletedWsIds: number[];
  onRestore: (id: number) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <div className="text-13 text-text-primary">
        {title} <span className="text-2xs text-text-secondary">（{rows.length}）</span>
      </div>
      <div className="overflow-hidden rounded-2xl border-[0.5px] border-border-button">
        <table className="w-full">
          <thead className="bg-bg-title text-text-secondary">
            <tr>
              <th className="px-4 py-2.5 text-start text-2xs font-normal">名称</th>
              <th className="px-4 py-2.5 text-start text-2xs font-normal">删除于</th>
              <th className="px-4 py-2.5 text-start text-2xs font-normal">还能恢复</th>
              <th className="w-32 px-4 py-2.5 text-end text-2xs font-normal">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const st = restoreState(
                {
                  id: r.id,
                  name: r.name,
                  deleted_at: r.deleted_at ?? null,
                  workspace_id: "workspace_id" in r ? (r as Kb).workspace_id : undefined,
                },
                kind,
                deletedWsIds,
              );
              return (
                <tr key={r.id} className="border-t-[0.5px] border-border-button hover:bg-bg-card">
                  <td className="px-4 py-2.5 text-13 text-text-primary">{r.name}</td>
                  <td className="px-4 py-2.5 text-2xs text-text-secondary">
                    {r.deleted_at ? new Date(r.deleted_at).toLocaleString() : "删除时间未知"}
                  </td>
                  <td className="px-4 py-2.5 text-2xs text-text-secondary">
                    <span className={st.kind === "expired" ? "text-state-error" : ""}>
                      {expiryLabel(r.deleted_at ?? null)}
                    </span>
                    {st.kind !== "restore" && (
                      <div className="text-state-warning">{st.hint}</div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-end">
                    <Button
                      variant={canRestore(st) ? "outline" : "ghost"}
                      size="sm"
                      disabled={!canRestore(st)}
                      title={st.hint}
                      onClick={() => onRestore(r.id)}
                    >
                      <ArchiveRestore aria-hidden="true" className="size-3.5" />
                      {st.label}
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {kind === "kb" && (
        <div className="text-2xs text-text-secondary">
          提示：库所在的工作区如果也在回收站里，恢复按钮会变成「先恢复工作区」（顺序依赖，D125）。
        </div>
      )}
    </div>
  );
}
