/**
 * 照片 tab（D20 照片墙）—— 按月分组 + 分页（页码以 **URL** 为唯一真相）。
 *
 * ## 两条纪律
 *
 * 1. 🔑 **无日期的照片不许被过滤掉**：它们挂在"未标注日期"那一档、排在最后
 *    （`groupPhotosByMonth`）—— 过滤掉等于让用户以为照片丢了。
 * 2. 🔑 **分页在"按月分组"之前做**（服务端分页），`page` 从 URL 读、
 *    非法值回落 1（`parsePhotoPage`）；首次加载**不夹**页码，
 *    否则合法深链 `?page=3` 会被夹成第 1 页（`shownPhotoPage` 的注释）。
 */
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ImageOff } from "lucide-react";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { Alert } from "../../../components/ui/alert";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog";
import { Button } from "../../../components/ui/button";
import { EmptyState } from "../../../components/ui/empty-state";
import { Skeleton } from "../../../components/ui/skeleton";
import { api, errText } from "../../../lib/api";
import { formatBytes } from "../../../lib/trash";
import {
  PHOTO_PAGE_SIZE,
  parsePhotoPage,
  photoPages,
  shownPhotoPage,
} from "../../../lib/photos";
// ⚠️ 按月分组 / 无日期那档的文案在 `lib/timeline.ts`（照片墙与时间线共用同一套判定）
import { groupPhotosByMonth, undatedLabel } from "../../../lib/timeline";
import { Keys } from "../../../app/queryKeys";
import { useBlobUrl } from "../../chat/useBlobUrl";
import type { KbDetail, PhotoRow } from "../../../types/api";

export function PhotosTab({ kb }: { kb: KbDetail }) {
  const [params, setParams] = useSearchParams();
  const urlPage = parsePhotoPage(params.get("page"));
  const [lightbox, setLightbox] = React.useState<PhotoRow | null>(null);
  const [firstLoadDone, setFirstLoadDone] = React.useState(false);

  const q = useQuery({
    queryKey: Keys.photos(kb.id, { page: urlPage, pageSize: PHOTO_PAGE_SIZE }),
    queryFn: () => api.photos(kb.id, { page: urlPage, pageSize: PHOTO_PAGE_SIZE }),
  });

  React.useEffect(() => {
    if (q.data) setFirstLoadDone(true);
  }, [q.data]);

  const total = q.data?.total ?? 0;
  const page = shownPhotoPage(urlPage, total, firstLoadDone);
  const pages = photoPages(total);
  const rows = q.data?.items ?? [];
  const groups = React.useMemo(() => groupPhotosByMonth(rows), [rows]);
  /** `groupPhotosByMonth` 的类型只保留 doc_id/file_name/date（那是分组的输入面），
   *  灯箱要 `size_bytes`/`mime_type` ⇒ 用一张表拿回完整的行（**不重写分组逻辑**）。 */
  const byId = React.useMemo(() => {
    const m = new Map<number, PhotoRow>();
    for (const r of rows) m.set(r.doc_id, r);
    return m;
  }, [rows]);

  const go = React.useCallback(
    (p: number) => {
      const next = new URLSearchParams(params);
      next.set("page", String(Math.min(Math.max(1, p), pages)));
      setParams(next, { replace: true });
    },
    [params, setParams, pages],
  );

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-2xs text-text-secondary">
          {total} 张照片 · 第 {page} / {pages} 页 · 每页 {PHOTO_PAGE_SIZE} 张
          （⚠️ 分页在"按月分组"之前做，所以同一个月可能跨两页）
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => go(page - 1)}>
            <ChevronLeft aria-hidden="true" className="size-3.5" />
            上一页
          </Button>
          <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => go(page + 1)}>
            下一页
            <ChevronRight aria-hidden="true" className="size-3.5" />
          </Button>
        </div>
      </div>

      {q.isError && <Alert tone="danger">{errText(q.error)}</Alert>}
      {q.isLoading && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
          {Array.from({ length: 12 }, (_, i) => (
            <Skeleton key={i} className="aspect-square w-full rounded-xl" />
          ))}
        </div>
      )}

      {!q.isLoading && rows.length === 0 && (
        <EmptyState
          icon={ImageOff}
          title="还没有照片"
          description={total === 0 ? "把图片上传到这个库（或导入照片目录），它们会带上拍摄日期。" : "这一页是空的 —— 回到第 1 页看看。"}
          actions={pages > 1 ? <Button variant="outline" onClick={() => go(1)}>回到第 1 页</Button> : undefined}
        />
      )}

      {groups.map((g) => (
        <section key={g.month ?? "undated"} className="flex flex-col gap-2">
          <div className="text-13 text-text-primary">
            {g.month ? `${g.month}` : undatedLabel(g.photos.length)}
            {g.month && <span className="ms-2 text-2xs text-text-secondary">{g.photos.length} 张</span>}
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
            {g.photos.map((p) => (
              <PhotoCell
                key={p.doc_id}
                photo={byId.get(p.doc_id) ?? { ...p, size_bytes: null, mime_type: null, status: "" }}
                onOpen={() => setLightbox(byId.get(p.doc_id) ?? null)}
              />
            ))}
          </div>
        </section>
      ))}

      <Dialog open={!!lightbox} onOpenChange={(v) => !v && setLightbox(null)}>
        <DialogContent wide className="max-w-4xl">
          <DialogHeader>
            <DialogTitle className="truncate">{lightbox?.file_name}</DialogTitle>
            <DialogDescription>
              {lightbox?.date ?? "没有日期（EXIF 与文件名里都抽不到，后端不猜）"} ·{" "}
              {formatBytes(lightbox?.size_bytes)} · {lightbox?.mime_type ?? "未知类型"}
            </DialogDescription>
          </DialogHeader>
          {lightbox && <LightboxImage docId={lightbox.doc_id} name={lightbox.file_name} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PhotoCell({ photo, onOpen }: { photo: PhotoRow; onOpen: () => void }) {
  const blob = useBlobUrl(() => api.photoBlob(photo.doc_id), [photo.doc_id]);
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group relative aspect-square overflow-hidden rounded-xl border-[0.5px] border-border-button bg-bg-card"
      title={photo.file_name}
    >
      {blob.url ? (
        <img
          src={blob.url}
          alt={photo.file_name}
          className="size-full object-cover transition-transform group-hover:scale-[1.02]"
        />
      ) : (
        <span className="flex size-full items-center justify-center text-2xs text-text-secondary">
          {blob.error ? "加载失败" : "…"}
        </span>
      )}
      {photo.date && (
        <span className="absolute bottom-0 start-0 end-0 truncate bg-scrim px-1.5 py-0.5 text-2xs text-white">
          {photo.date}
        </span>
      )}
    </button>
  );
}

function LightboxImage({ docId, name }: { docId: number; name: string }) {
  const blob = useBlobUrl(() => api.photoBlob(docId), [docId]);
  if (blob.error) return <Alert tone="danger">{blob.error}</Alert>;
  if (!blob.url) return <Skeleton className="h-64 w-full" />;
  return <img src={blob.url} alt={name} className="max-h-[70vh] w-full rounded-xl object-contain" />;
}
