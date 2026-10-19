/**
 * 页图 + 命中框（L3 引用定位的唯一渲染点）。
 *
 * ## 三条踩过的坑（`lib/boxes.ts` 的注释里都有，这里只落地）
 *
 * 1. **`bbox` 是归一化的 0~1**（§6.1）：`toRect` 乘回**页图的实际像素尺寸**，
 *    再由 CSS 用百分比铺回显示尺寸 ⇒ 显示大小与坐标解耦，
 *    不会因为容器宽度变了就整体偏。
 * 2. **按 `locations` 的形状分派，不按 `kind`**（D58）：表格 chunk 的
 *    `kind` 是 `"text"`，但它的坐标带 `row`/`col`。`planOverlay` 负责这件事。
 * 3. **不能用 `<img src>`**（§6.8）：端点要 Bearer ⇒ 走 blob（`useBlobUrl`）。
 */
import * as React from "react";

import { api } from "../../lib/api";
import { GRID_INLINE_LIMIT, planOverlay, pagesOf, type Rect } from "../../lib/boxes";
import type { CitationKind, Location } from "../../types/api";
import { Spinner } from "../../components/ui/spinner";
import { Alert } from "../../components/ui/alert";
import { useBlobUrl } from "./useBlobUrl";

export function PageOverlay({
  docId,
  kind,
  locations,
  fileName,
}: {
  docId: number;
  kind: CitationKind;
  locations: Location[] | null;
  fileName: string;
}) {
  const pages = React.useMemo(() => (locations ? pagesOf(locations) : []), [locations]);
  const [manualPage, setManualPage] = React.useState<number | null>(null);
  const page = manualPage ?? pages[0] ?? 1;

  // `kind="image"`（照片 / 视觉腿）走**原图**：它不是 PDF，没有页图端点
  // （`api.photoBlob` 的注释里写了为什么不用缩略图）。
  const isImage = kind === "image";
  const needImage = isImage || pages.length > 0;

  const blob = useBlobUrl(
    needImage ? () => (isImage ? api.photoBlob(docId) : api.pageImage(docId, page, 1400)) : null,
    [docId, page, isImage, needImage],
  );

  const [size, setSize] = React.useState<{ w: number; h: number } | null>(null);
  const [expandGrid, setExpandGrid] = React.useState(false);

  // 换页/换图要重置量到的尺寸与"展开逐格"（否则会拿上一页的框画这一页）
  React.useEffect(() => {
    setSize(null);
    setExpandGrid(false);
  }, [page, docId, kind]);

  const plan = React.useMemo(() => {
    // 有图时等量到真实像素再算（§6.1：框必须乘回渲染尺寸）。
    if (needImage && !size) return null;
    return planOverlay({
      kind,
      locations,
      // ⚠️ 没有坐标时**不能**传一个默认页码：那等于声称"我知道是哪一页"（D60）
      page: pages.length ? page : null,
      // ⚠️ 1×1 只是给"没有坐标"那条分支用的占位：那种情况**本来就不画框**，
      //    但要**留下原因**（D72：静默不显示会让人以为"这个库没有照片"）。
      //    真正画框的分支（single/grid/page）一定 `needImage=true`，用的是真实尺寸。
      pxW: size?.w ?? 1,
      pxH: size?.h ?? 1,
    });
  }, [kind, locations, page, pages.length, size, needImage]);

  const pct = (r: Rect) => ({
    left: `${(r.x / (size?.w ?? 1)) * 100}%`,
    top: `${(r.y / (size?.h ?? 1)) * 100}%`,
    width: `${(r.w / (size?.w ?? 1)) * 100}%`,
    height: `${(r.h / (size?.h ?? 1)) * 100}%`,
  });

  return (
    <div className="flex flex-col gap-2">
      {plan && plan.note && (
        <div className="text-2xs text-text-secondary">{plan.note}</div>
      )}

      {pages.length > 1 && plan?.page != null && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-2xs text-text-secondary">这一段的框分布在 {pages.length} 页：</span>
          {pages.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setManualPage(p)}
              className={
                "rounded-md px-1.5 py-0.5 text-2xs transition-colors " +
                (p === page ? "bg-text-primary text-bg-base" : "bg-bg-card text-text-secondary hover:text-text-primary")
              }
            >
              第 {p} 页
            </button>
          ))}
        </div>
      )}

      {plan?.mode === "grid" && plan.overflow && (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setExpandGrid((v) => !v)}
            className="rounded-md bg-bg-card px-2 py-1 text-2xs text-text-primary transition-colors hover:bg-border-button"
          >
            {expandGrid ? "只看表格外框" : `展开逐格（${plan.cellCount} 个非空格）`}
          </button>
          <span className="text-2xs text-text-secondary">
            超过 {GRID_INLINE_LIMIT} 个格子默认只画外框（D59）
          </span>
        </div>
      )}

      {blob.error && <Alert tone="danger">页图加载失败：{blob.error}</Alert>}

      {needImage && !blob.error && (
        <div className="relative w-full overflow-hidden rounded-lg border-[0.5px] border-border-button bg-paper">
          {blob.loading && (
            <div className="flex h-40 items-center justify-center">
              <Spinner label="正在取页图…" />
            </div>
          )}
          {blob.url && (
            <div className="relative w-full">
              <img
                src={blob.url}
                alt={`${fileName} 第 ${page} 页`}
                className="block w-full"
                onLoad={(e) => {
                  const el = e.currentTarget;
                  if (el.naturalWidth && el.naturalHeight) {
                    setSize({ w: el.naturalWidth, h: el.naturalHeight });
                  }
                }}
              />
              {plan?.mode === "page" && (
                <div
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-0 border-2 border-box bg-box/10"
                />
              )}
              {plan?.mode === "single" &&
                plan.rects.map((r, i) => (
                  <div
                    key={i}
                    aria-hidden="true"
                    className="pointer-events-none absolute rounded-[2px] border-2 border-box bg-box/15"
                    style={pct(r)}
                  />
                ))}
              {plan?.mode === "grid" &&
                (expandGrid
                  ? plan.rows.flatMap((row) =>
                      row.cells.map((c) => (
                        <div
                          key={`${row.row}-${c.col}`}
                          aria-hidden="true"
                          className="pointer-events-none absolute rounded-[2px] border border-box bg-box/10"
                          style={pct(c.rect)}
                        />
                      )),
                    )
                  : plan.rects.map((r, i) => (
                      <div
                        key={i}
                        aria-hidden="true"
                        className="pointer-events-none absolute rounded-[2px] border-2 border-box bg-box/10"
                        style={pct(r)}
                      />
                    )))}
            </div>
          )}
        </div>
      )}

      {!needImage && plan && plan.mode === "none" && !blob.error && (
        <div className="rounded-lg border-[0.5px] border-dashed border-border-button px-3 py-6 text-center text-2xs text-text-secondary">
          {plan.note || "没有可渲染的坐标"}
        </div>
      )}
    </div>
  );
}
