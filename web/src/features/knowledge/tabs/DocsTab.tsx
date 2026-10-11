/**
 * 文档 tab：上传 + 文档表 + 回收站。
 *
 * ## 轮询（进度百分比的唯一来源）
 *
 * 用 react-query 的 `refetchInterval` 而不是手写 `setInterval`：
 * 手写版有一个实测踩到的坑（§5.38②：`timer.current` 没在回调里置回 `null`
 * ⇒ 轮询只跑一次就永久停）。`refetchInterval` 把"还要不要继续"变成一个
 * 纯函数（`hasActiveJob`），没有可变计时器可忘。
 *
 * ⚠️ 停的条件是**两个都终态**：文档全终态 **且** 没有活动任务 ——
 * `finish_job` 在 handler 返回之后才跑（D14 允许），只看文档会提前收工。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Button } from "../../../components/ui/button";
import { Card } from "../../../components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog";
import { EmptyState } from "../../../components/ui/empty-state";
import { Progress } from "../../../components/ui/progress";
import { SkeletonTable } from "../../../components/ui/skeleton";
import { api, errText } from "../../../lib/api";
import { groupJobsByDoc, hasActiveJob, jobView, type ProgressView } from "../../../lib/progress";
import {
  batchLabel,
  batchProgress,
  batchSettled,
  splitBatches,
  unsubmittedAfter,
  uploadPhaseLabel,
  type BatchItem,
  type UploadPhase,
} from "../../../lib/upload";
import {
  canPurge,
  confirmText,
  freedHint,
  overwriteNotice,
  type PurgeResp,
} from "../../../lib/trash";
import { plainText } from "../../../components/ui/rich-text";
import { Keys } from "../../../app/queryKeys";
import type { DocumentRow, KbDetail } from "../../../types/api";
import { ChunksDialog } from "../ChunksDialog";
import { DocsTable } from "../DocsTable";
import { UploadZone } from "../UploadZone";

const DOC_TERMINAL = ["ready", "failed", "deleted"];

export function DocsTab({ kb }: { kb: KbDetail }) {
  const qc = useQueryClient();
  const [uploading, setUploading] = React.useState(false);
  const [phase, setPhase] = React.useState<UploadPhase | null>(null);
  const [batch, setBatch] = React.useState<BatchItem[] | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [showTrash, setShowTrash] = React.useState(false);
  const [chunksDoc, setChunksDoc] = React.useState<DocumentRow | null>(null);
  const trashBeforeRef = React.useRef<string[] | null>(null);

  const docsQ = useQuery({
    queryKey: Keys.docs(kb.id),
    queryFn: () => api.listDocs(kb.id),
    refetchInterval: (q) => {
      const rows = q.state.data ?? [];
      return rows.some((d) => !DOC_TERMINAL.includes(d.status)) ? 2500 : false;
    },
  });

  const docs = React.useMemo(() => docsQ.data ?? [], [docsQ.data]);

  const jobsQ = useQuery({
    queryKey: Keys.jobs({ kb_id: kb.id }),
    queryFn: () => api.jobs({ kb_id: kb.id }),
    refetchInterval: (q) => {
      const jobs = q.state.data?.items ?? [];
      return hasActiveJob(jobs) ? 2500 : false;
    },
  });

  const trashQ = useQuery({
    queryKey: Keys.kbTrashDocs(kb.id),
    queryFn: () => api.trash(kb.id),
  });

  const byDoc = React.useMemo(() => groupJobsByDoc(jobsQ.data?.items ?? []), [jobsQ.data]);
  const docById = React.useMemo(() => {
    const m = new Map<number, DocumentRow>();
    for (const d of docs) m.set(d.id, d);
    return m;
  }, [docs]);

  const views = React.useMemo(() => {
    const m = new Map<number, ProgressView | null>();
    for (const d of docs) m.set(d.id, jobView(byDoc.get(d.id) ?? null, d.status));
    return m;
  }, [docs, byDoc]);

  const batchItems = React.useMemo<BatchItem[]>(
    () =>
      (batch ?? []).map((it) => ({
        ...it,
        view: jobView(byDoc.get(it.docId) ?? null, docById.get(it.docId)?.status),
      })),
    [batch, byDoc, docById],
  );
  const bp = React.useMemo(() => batchProgress(batchItems), [batchItems]);

  // 覆盖式更新提示：上传完成 + 回收站重新拉取之后算一次
  React.useEffect(() => {
    if (!batch || !trashQ.data || trashBeforeRef.current == null) return;
    const n = overwriteNotice(
      trashBeforeRef.current,
      trashQ.data.map((d) => d.file_name),
    );
    if (n) setNotice(n);
    trashBeforeRef.current = null;
  }, [batch, trashQ.data]);

  const invalidateAll = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: Keys.docs(kb.id) });
    void qc.invalidateQueries({ queryKey: Keys.jobs({ kb_id: kb.id }) });
    void qc.invalidateQueries({ queryKey: Keys.kbTrashDocs(kb.id) });
    void qc.invalidateQueries({ queryKey: Keys.kb(kb.id) });
    void qc.invalidateQueries({ queryKey: Keys.kbs() });
  }, [qc, kb.id]);

  const handleFiles = React.useCallback(
    async (files: File[]) => {
      setErr(null);
      setNotice(null);
      setBatch(null);
      setUploading(true);
      trashBeforeRef.current = (trashQ.data ?? []).map((d) => d.file_name);

      const total = files.length;
      const batches = splitBatches(total);
      let submitted = 0;
      const created: DocumentRow[] = [];

      for (let i = 0; i < batches; i++) {
        setPhase({ total, submitted, batches, batchesDone: i });
        const slice = files.slice(i * 20, (i + 1) * 20);
        try {
          const resp = await api.uploadDocs(kb.id, slice);
          created.push(...resp.documents);
          submitted += resp.documents.length;
          setPhase({ total, submitted, batches, batchesDone: i + 1 });
        } catch (e) {
          const left = unsubmittedAfter(total, submitted);
          setErr(`${errText(e)} —— 还有 ${left} 份没有提交（已提交的 ${submitted} 份不受影响）`);
          break;
        }
      }

      setBatch(created.map((d) => ({ docId: d.id, name: d.file_name, view: null })));
      setUploading(false);
      setPhase(null);
      invalidateAll();
      // 回收站要延迟一点再拉：旧版本刚被移进去
      window.setTimeout(() => void trashQ.refetch(), 1200);
    },
    [kb.id, trashQ, invalidateAll],
  );

  const retryM = useMutation({
    mutationFn: (d: DocumentRow) => api.retryDoc(d.id),
    onSuccess: invalidateAll,
    onError: (e) => setErr(errText(e)),
  });
  const reparseM = useMutation({
    mutationFn: (d: DocumentRow) => api.reparseDoc(d.id),
    onSuccess: invalidateAll,
    onError: (e) => setErr(errText(e)),
  });
  const deleteM = useMutation({
    mutationFn: (d: DocumentRow) => api.deleteDoc(d.id),
    onSuccess: invalidateAll,
    onError: (e) => setErr(errText(e)),
  });
  const restoreM = useMutation({
    mutationFn: (d: DocumentRow) => api.restoreDoc(d.id),
    onSuccess: invalidateAll,
    onError: (e) => setErr(errText(e)),
  });

  const trashRows = trashQ.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      {err && <Alert tone="danger">{err}</Alert>}
      {notice && (
        <Alert tone="warning" title="覆盖式更新">
          {notice}
        </Alert>
      )}

      <UploadZone onFiles={handleFiles} busy={uploading} disabled={uploading} />

      {phase && (
        <Alert tone="info">
          {uploadPhaseLabel(phase)}
          <div className="mt-1.5">
            <Progress value={phase.batches ? phase.batchesDone / phase.batches : 0} label="上传批次进度" />
          </div>
        </Alert>
      )}

      {!uploading && batch && batchItems.length > 0 && (
        <Card className="flex flex-col gap-2 p-3.5">
          <div className="flex items-center justify-between gap-3">
            <span className="text-13 text-text-primary">本次上传：{batchLabel(bp)}</span>
            <Button variant="ghost" size="xs" onClick={() => setBatch(null)}>
              收起
            </Button>
          </div>
          {!batchSettled(bp) && <Progress value={bp.avgPct / 100} label="本次上传汇总进度" />}
          <div className="text-2xs text-text-secondary">
            汇总只统计这次上传的 {bp.total} 份；平均值排除失败的那些（CONVENTIONS §3.5）。
          </div>
        </Card>
      )}

      {docsQ.isLoading ? (
        <SkeletonTable rows={4} />
      ) : docs.length === 0 ? (
        <EmptyState
          icon={Trash2}
          title="还没有文档"
          description="拖文件到上面的框里，或者点「选择文件」。上传后会自动解析、切分、入库。"
        />
      ) : (
        <DocsTable
          rows={docs}
          views={views}
          onRetry={(d) => retryM.mutate(d)}
          onReparse={(d) => reparseM.mutate(d)}
          onDelete={(d) => {
            if (
              window.confirm(
                plainText(
                  `把「${d.file_name}」移进回收站？\n\n这是软删除：30 天内可以恢复（D26）。`,
                ),
              )
            ) {
              deleteM.mutate(d);
            }
          }}
          onChunks={(d) => setChunksDoc(d)}
        />
      )}

      {/* ---------------- 回收站 ---------------- */}
      <TrashSection
        kb={kb}
        rows={trashRows}
        show={showTrash}
        onToggle={() => setShowTrash((v) => !v)}
        onRestore={(d) => restoreM.mutate(d)}
        onPurged={invalidateAll}
        onError={(m) => setErr(m)}
      />

      <ChunksDialog doc={chunksDoc} onOpenChange={(v) => !v && setChunksDoc(null)} />
    </div>
  );
}

function TrashSection({
  kb,
  rows,
  show,
  onToggle,
  onRestore,
  onPurged,
  onError,
}: {
  kb: KbDetail;
  rows: DocumentRow[];
  show: boolean;
  onToggle: () => void;
  onRestore: (d: DocumentRow) => void;
  onPurged: () => void;
  onError: (m: string) => void;
}) {
  const [preview, setPreview] = React.useState<PurgeResp | null>(null);

  const dryM = useMutation({
    mutationFn: () => api.purgeTrash({ kbId: kb.id, dryRun: true }),
    onSuccess: (r) => setPreview(r),
    onError: (e) => onError(errText(e)),
  });
  const purgeM = useMutation({
    mutationFn: () => api.purgeTrash({ kbId: kb.id }),
    onSuccess: () => {
      setPreview(null);
      onPurged();
    },
    onError: (e) => onError(errText(e)),
  });

  return (
    <Card className="p-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button type="button" onClick={onToggle} className="flex items-center gap-1.5 text-sm text-text-primary">
          {show ? (
            <ChevronDown aria-hidden="true" className="size-4 text-text-secondary" />
          ) : (
            <ChevronRight aria-hidden="true" className="size-4 text-text-secondary" />
          )}
          回收站
          {rows.length > 0 && <span className="text-text-secondary">（{rows.length}）</span>}
        </button>
        <Button
          variant="danger"
          size="sm"
          disabled={!canPurge(rows.length)}
          onClick={() => dryM.mutate()}
        >
          <Trash2 aria-hidden="true" className="size-3.5" />
          清空回收站
        </Button>
      </div>

      <div className="mt-1 text-2xs text-text-secondary">
        软删的文档保留 30 天；「清空」是本项目唯一不可撤销的操作（会先给你一份预览）。
      </div>

      {show && rows.length > 0 && (
        <div className="mt-3">
          <DocsTable rows={rows} views={new Map()} trash onRestore={onRestore} />
        </div>
      )}
      {show && rows.length === 0 && (
        <div className="mt-3 text-2xs text-text-secondary">回收站是空的。</div>
      )}

      <Dialog open={preview != null} onOpenChange={(v) => !v && setPreview(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>清空回收站</DialogTitle>
            <DialogDescription>这一步不可撤销 —— 下面是要删掉的东西（预览来自 dry_run）。</DialogDescription>
          </DialogHeader>
          {preview && (
            <div className="flex flex-col gap-2 text-sm">
              <div className="whitespace-pre-wrap text-text-primary">
                {confirmText(preview, `「${kb.name}」`)}
              </div>
              {freedHint(preview) && (
                <div className="text-2xs text-text-secondary">{freedHint(preview)}</div>
              )}
              <div className="text-2xs text-text-secondary">
                {preview.docs} 份文档 · {preview.chunks} 个片段 · 释放 {preview.bytes_freed} 字节
                {preview.ids_truncated ? "（预览的 id 列表被截断了，总数以上面为准）" : ""}
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
    </Card>
  );
}
