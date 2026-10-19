/**
 * 🎯 L3 画框的几何层 —— 1e 里**唯一会静默出错**的一层。
 *
 * 页面壳子错了立刻看得见（白屏 / 404 / 框根本没画）；
 * 这里的错了**看不见**：框会画在页面上，只是整体偏了半行。
 * 所以这一层写成纯函数（无 React、无 DOM），并由 `boxes.test.ts` 钉住。
 *
 * ## 三条硬规矩（CONVENTIONS §6）
 *
 * ### §6.1 归一化坐标必须乘回渲染空间
 *
 * `bbox` 是**归一化到 0~1** 的（相对页面尺寸，与 DPI 无关，D33）。
 * 直接把它当像素用 → **框整体下移约半行**，第一个框盖住第 2 行却漏掉第 1 行。
 * pymupdf 那边栽过一次（`PROGRESS.md` §5.19），前端是同一个坑的另一次机会：
 * 乘回**当前渲染出来的页图**的像素尺寸即可（`toRect`）。
 *
 * ### D58 表格 chunk 的多框要按 row/col 排成网格
 *
 * 一张登记表实测 40 个非空单元格（D59 上限 200）。
 * 不分形态地"全部画出来"会糊成一片 —— 不是因为框位置算错，
 * 而是因为**没有分主次**：用户要看的是"这张表"，
 * 逐格描边只是辅助。所以超过 `GRID_INLINE_LIMIT` 个格子时默认只画**外框**，
 * 并给一个开关展开逐格（D59 的"超出则只画外框"就是这条）。
 *
 * ### D60 扫描件只高亮整页
 *
 * 无文本层 → colSmol 只给视觉向量、**拿不到坐标** → 不做精确框。
 *
 * ## 为什么按 `locations` 的形状分派，而不是按 `kind`
 *
 * `/search` 的 `SearchHit.kind` 与 SSE `sources[].kind` 是**引用类型**
 * （`text` / `page` / `image`），**不是** `chunks.kind`
 * （`text` / `table` / `page` / `chat`）。表格 chunk 在 `/search` 里
 * 长成 `kind="text"` + 40 个带 `row`/`col` 的框。
 *
 * → 按 `kind` 分派会把表格当正文画，把单框当网格画。
 * → 真正可靠的信号是 `locations` 的形状（有没有 `row`/`col`、几个框）。
 */

import type { CitationKind, Location } from "../types/api";

/** 归一化 bbox：`[x0, y0, x1, y1]`，取值 0~1（CONVENTIONS §6）。 */
export type Bbox = [number, number, number, number];

/** 像素矩形，原点在左上角（浏览器坐标系）。 */
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GridCell {
  col: number;
  rect: Rect;
}

export interface GridRow {
  row: number;
  cells: GridCell[];
}

export type OverlayMode = "none" | "page" | "single" | "grid";

export interface OverlayPlan {
  mode: OverlayMode;
  /** 要渲染的页码（1-based）。`none` 时为 null。 */
  page: number | null;
  /** `page` / `single` 模式直接画这些；`grid` 模式画 `outer`（见 `overflow`）。 */
  rects: Rect[];
  rows: GridRow[];
  /** 表格模式下被折叠的格子数（`overflow=true` 时默认只画外框）。 */
  overflow: boolean;
  /** 所有非空格子/框的总数（用于 UI 显示"表格 9×7，40 个非空格"）。 */
  cellCount: number;
  rowsTotal: number;
  colsTotal: number;
  /** 这个 chunk 的框分布在几页上（>1 时 UI 要给翻页器）。 */
  pageCount: number;
  /** 直接显示给用户的一句话原因。**不许静默不画** —— 那会让人以为"没有坐标"。 */
  note: string;
}

/** 超过这个格子数就默认只画外框（D59）。 */
export const GRID_INLINE_LIMIT = 24;

/**
 * §6.1：归一化 bbox → 当前渲染页图的像素矩形。
 *
 * @param pxW / pxH **当前渲染出来的页图**的像素尺寸。
 *   ⚠️ 传归一化值进来 = §6.1 那个坑。调用方必须先量到图的实际尺寸。
 * @returns 非法 bbox 返回 null（而不是画一个 0×0 的框糊弄过去）。
 */
export function toRect(bbox: Bbox, pxW: number, pxH: number): Rect | null {
  if (!validBbox(bbox)) return null;
  const [x0, y0, x1, y1] = bbox;
  const r: Rect = {
    x: x0 * pxW,
    y: y0 * pxH,
    w: (x1 - x0) * pxW,
    h: (y1 - y0) * pxH,
  };
  return r.w > 0 && r.h > 0 ? r : null;
}

/**
 * bbox 合法性。判定条件与 `services/ingest/preview.py::_valid_bbox` 对齐 ——
 * 服务端不画的框，前端也不该画（否则两边对不上，很难查）。
 */
export function validBbox(bbox: unknown): bbox is Bbox {
  if (!Array.isArray(bbox) || bbox.length !== 4) return false;
  for (const v of bbox) {
    if (typeof v !== "number" || !Number.isFinite(v)) return false;
    // 允许一点点越界（浮点/裁剪误差），但不允许离谱的值
    if (v < -0.01 || v > 1.01) return false;
  }
  const [x0, y0, x1, y1] = bbox as number[];
  return x1 > x0 && y1 > y0;
}

/** 若干矩形的并集（外框）。空数组返回 null。 */
export function unionRect(rects: Rect[]): Rect | null {
  if (rects.length === 0) return null;
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const r of rects) {
    x0 = Math.min(x0, r.x);
    y0 = Math.min(y0, r.y);
    x1 = Math.max(x1, r.x + r.w);
    y1 = Math.max(y1, r.y + r.h);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** 出现过的页码，升序去重。 */
export function pagesOf(locs: Location[]): number[] {
  return [...new Set(locs.map((l) => l.page))].sort((a, b) => a - b);
}

/** 某页上的框。 */
export function locsOnPage(locs: Location[], page: number): Location[] {
  return locs.filter((l) => l.page === page);
}

/**
 * 是不是表格坐标（D58）。判据是**有没有 row/col**，
 * 不是 `kind` —— 见模块 docstring 末尾那段。
 */
export function isTableLocs(locs: Location[]): boolean {
  return locs.some((l) => typeof l.row === "number" && typeof l.col === "number");
}

/** 按 row/col 排成网格（D58）。row 升序、col 升序，缺坐标的框按 0 处理。 */
export function groupByRow(
  locs: Location[],
  pxW: number,
  pxH: number,
): { rows: GridRow[]; rects: Rect[] } {
  const byRow = new Map<number, GridCell[]>();
  const rects: Rect[] = [];
  for (const loc of locs) {
    const r = toRect(loc.bbox, pxW, pxH);
    if (!r) continue;
    rects.push(r);
    const row = loc.row ?? 0;
    const col = loc.col ?? 0;
    const bucket = byRow.get(row);
    if (bucket) bucket.push({ col, rect: r });
    else byRow.set(row, [{ col, rect: r }]);
  }
  const rows: GridRow[] = [...byRow.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([row, cells]) => ({
      row,
      cells: cells.sort((a, b) => a.col - b.col),
    }));
  return { rows, rects };
}

export interface PlanInput {
  /** 契约的引用类型（`/search` 的 `kind`）。 */
  kind?: CitationKind | null;
  locations?: Location[] | null;
  /** 调用方已经选定的页码（如翻页器）。不给就用框里出现的第一页。 */
  page?: number | null;
  /** 当前页图的像素尺寸（§6.1 的另一半）。 */
  pxW: number;
  pxH: number;
}

/**
 * 画框方案的**唯一**决策点。UI 只负责把返回的 rect 画出来。
 *
 * 分派顺序（有意为之，不要重排）：
 * 1. `kind === "page"` → 整页高亮（D60）。扫描件没有精确坐标，
 *    硬画就是画一个假框，比不画更糟。
 * 2. 没有 locations → 不画，且**必须给出原因**（D72 同一个道理：
 *    静默不显示会让人以为"这个库没有照片"）。
 * 3. 带 `row`/`col` → 表格网格（D58）。
 * 4. 其余 → 直接画（正文 chunk 恰好 1 个框；多个就都画）。
 */
export function planOverlay(input: PlanInput): OverlayPlan {
  const { kind, pxW, pxH } = input;
  const locs = (input.locations ?? []).filter((l) => validBbox(l.bbox));

  const base: OverlayPlan = {
    mode: "none",
    page: null,
    rects: [],
    rows: [],
    overflow: false,
    cellCount: 0,
    rowsTotal: 0,
    colsTotal: 0,
    pageCount: 0,
    note: "",
  };

  // ① D60：扫描件只高亮整页
  if (kind === "page") {
    const pages = pagesOf(locs);
    // 页码只能来自 locations 或调用方。两者都没有 → 不画：
    // 画一个"整页框"却不知道是哪一页，比不画更糟（用户会以为框画错了位置）。
    const page = input.page ?? pages[0] ?? null;
    if (page == null) {
      return {
        ...base,
        note:
          "这一条是扫描件引用，按 D60 只做整页高亮；但该 chunk 没有页码，" +
          "无法定位到具体页（扫描页目前走视觉腿，API 进程恒 skipped，见 D72）。",
      };
    }
    return {
      ...base,
      mode: "page",
      page,
      rects: [{ x: 0, y: 0, w: pxW, h: pxH }],
      cellCount: 1,
      pageCount: pages.length || 1,
      note: "扫描件无文本层，拿不到精确坐标 —— 按 D60 只高亮整页。",
    };
  }

  // ② 没有坐标：如实说明，不静默
  if (locs.length === 0) {
    return {
      ...base,
      note:
        kind === "image"
          ? "图片引用没有文本层坐标，显示原文与文件名。"
          : "这条结果没有坐标信息：聊天片段与图片是纯文本块，Office 文档的坐标仍在补齐中。",
    };
  }

  const pages = pagesOf(locs);
  const page = pickPage(input, pages);
  if (page == null) return { ...base, note: "坐标里没有页码，无法渲染。" };

  const onPage = locsOnPage(locs, page);

  // ③ 表格：按 row/col 排网格（D58）
  if (isTableLocs(onPage)) {
    const { rows, rects } = groupByRow(onPage, pxW, pxH);
    const outer = unionRect(rects);
    const colsTotal = rows.reduce((n, r) => Math.max(n, ...r.cells.map((c) => c.col)), -1) + 1;
    const overflow = rects.length > GRID_INLINE_LIMIT;
    return {
      ...base,
      mode: "grid",
      page,
      // ⚠️ 格子不多时**逐格画**（那就是表格轮廓，正是 D58 想要的"排成网格"）；
      // 超阈值才退化成外框（D59）—— 否则小表格只给一个外框，
      // 用户既看不到行列、又因为没到阈值而没有展开入口（e2e 实测到这个缺口）。
      rects: overflow ? (outer ? [outer] : []) : rects,
      rows,
      overflow,
      cellCount: rects.length,
      rowsTotal: rows.length,
      colsTotal: Math.max(0, colsTotal),
      pageCount: pages.length,
      note:
        `表格 ${rows.length}×${Math.max(0, colsTotal)}，${rects.length} 个非空单元格（D58：按 row/col 排网格）。` +
        (overflow
          ? ` 超过 ${GRID_INLINE_LIMIT} 个，默认只画外框，可展开逐格。`
          : ""),
    };
  }

  // ④ 正文：直接画（实测恰好 1 个框；多个就都画）
  const rects = onPage
    .map((l) => toRect(l.bbox, pxW, pxH))
    .filter((r): r is Rect => r !== null);
  return {
    ...base,
    mode: rects.length ? "single" : "none",
    page,
    rects,
    cellCount: rects.length,
    pageCount: pages.length,
    note:
      pages.length > 1
        ? `这一段的框分布在 ${pages.length} 页上，用上面的页码切换。`
        : "正文段落框（CONVENTIONS §6.2：正文 chunk 恰好 1 个框）。",
  };
}

function pickPage(input: PlanInput, pages: number[]): number | null {
  // 调用方指定的页优先（翻页器），但必须是**真的有框的那一页** ——
  // 否则会画出一个"空白页 + 一个框"，看起来像框错位置。
  if (input.page != null && (pages.length === 0 || pages.includes(input.page))) {
    return input.page;
  }
  return pages.length ? pages[0] : null;
}