/**
 * 🎯 L3 画框几何层的单测 —— **1e 唯一会静默出错的那层**。
 *
 * 测试原则（CONVENTIONS §9）：**覆盖实际踩过的坑，不是理想路径**。
 * 这里每个 fixture 都取自真实数据（`doc_id=585` 的 15 页验收 PDF，
 * `api_1c_check.py` 摄取后的 `chunks.locations`），不是手编的理想矩形。
 *
 * ⚠️ 最重要的一条是 `§6.1 归一化坐标必须乘回像素空间`：
 * 直接拿 0~1 的值当像素用，框会**整体下移约半行** —— 页面上看得见框，
 * 肉眼看容易放过，服务端 `PROGRESS.md` §5.19 已经栽过一次。
 */

import { describe, expect, it } from "vitest";

import type { Location } from "../types/api";
import {
  GRID_INLINE_LIMIT,
  groupByRow,
  isTableLocs,
  locsOnPage,
  pagesOf,
  planOverlay,
  toRect,
  unionRect,
  validBbox,
} from "./boxes";

// ============================================================
// 真实 fixture（doc 585 =《…培养教育考察登记表填写指南-软件2025.pdf》）
// ============================================================

/** chunk_index=0：正文段落，恰好 1 个框（CONVENTIONS §6.2）。 */
const REAL_TEXT_LOC: Location = {
  page: 1,
  bbox: [0.0826, 0.1502, 0.9307, 0.2604],
};

/**
 * chunk_index=11：整张登记表，9 行 × 7 列，**40 个非空单元格**（第 3 页）。
 *
 * ⚠️ 这份数据里能看到合并单元格：`[1,1]` 横跨到 0.4885、`[7,1]` 横跨到 0.9049。
 * 也就是 `len(tbl.cells) = 40 ≠ 9×7 = 63`，差额全在合并单元格上
 * （CONVENTIONS §6.5：坐标只能从 `tbl.rows[i].cells` 取，
 * 按 `col_count` 切片还原会让合并行整体错位）。
 */
const REAL_TABLE_LOCS: Location[] = (
  [
    [0, 0, 0.0952, 0.142, 0.2214, 0.1893],
    [0, 1, 0.2214, 0.142, 0.3568, 0.1893],
    [0, 3, 0.3568, 0.142, 0.4885, 0.1893],
    [0, 4, 0.4885, 0.142, 0.6573, 0.1893],
    [0, 5, 0.6573, 0.142, 0.7739, 0.1893],
    [0, 6, 0.7739, 0.142, 0.9049, 0.1893],
    [1, 0, 0.0952, 0.1893, 0.2214, 0.2367],
    [1, 1, 0.2214, 0.1893, 0.3568, 0.2367],
    [1, 3, 0.3568, 0.1893, 0.4885, 0.2367],
    [1, 4, 0.4885, 0.1893, 0.6573, 0.2367],
    [1, 5, 0.6573, 0.1893, 0.7739, 0.2367],
    [1, 6, 0.7739, 0.1893, 0.9049, 0.2367],
    [2, 0, 0.0952, 0.2367, 0.2214, 0.2835],
    [2, 1, 0.2214, 0.2367, 0.3568, 0.2835],
    [2, 3, 0.3568, 0.2367, 0.4885, 0.2835],
    [2, 5, 0.6573, 0.2367, 0.7739, 0.2835],
    [2, 6, 0.7739, 0.2367, 0.9049, 0.2835],
    [3, 0, 0.0952, 0.2835, 0.2214, 0.3308],
    [3, 1, 0.2214, 0.2835, 0.3568, 0.3308],
    [3, 3, 0.3568, 0.2835, 0.4885, 0.3308],
    [3, 4, 0.4885, 0.2835, 0.6573, 0.3308],
    [3, 5, 0.6573, 0.2835, 0.7739, 0.3308],
    [3, 6, 0.7739, 0.2835, 0.9049, 0.3308],
    [4, 0, 0.0952, 0.3308, 0.2214, 0.3896],
    [4, 1, 0.2214, 0.3308, 0.4885, 0.3896],
    [4, 4, 0.4885, 0.3308, 0.6573, 0.3896],
    [4, 5, 0.6573, 0.3308, 0.9049, 0.3896],
    [5, 0, 0.0952, 0.3896, 0.2214, 0.4843],
    [5, 1, 0.2214, 0.3896, 0.3145, 0.4369],
    [5, 2, 0.3145, 0.3896, 0.4885, 0.4369],
    [5, 4, 0.4885, 0.3896, 0.6573, 0.4369],
    [5, 5, 0.6573, 0.3896, 0.9049, 0.4369],
    [6, 1, 0.2214, 0.4369, 0.3145, 0.4843],
    [6, 2, 0.3145, 0.4369, 0.4885, 0.4843],
    [6, 4, 0.4885, 0.4369, 0.6573, 0.4843],
    [6, 5, 0.6573, 0.4369, 0.9049, 0.4843],
    [7, 0, 0.0952, 0.4843, 0.2214, 0.7903],
    [7, 1, 0.2214, 0.4843, 0.9049, 0.7903],
    [8, 0, 0.0952, 0.7903, 0.2214, 0.8983],
    [8, 1, 0.2214, 0.7903, 0.9049, 0.8983],
  ] as const
).map(([row, col, x0, y0, x1, y1]) => ({
  page: 3,
  row,
  col,
  bbox: [x0, y0, x1, y1],
}));

// ============================================================
// §6.1 归一化 → 像素
// ============================================================

describe("§6.1 归一化坐标必须乘回渲染空间", () => {
  it("乘回当前渲染页图的像素尺寸", () => {
    // 页图 1200px 宽（A4 高宽比 595.3×841.9 → 约 1697px 高）
    const r = toRect(REAL_TEXT_LOC.bbox, 1200, 1697)!;
    expect(r.x).toBeCloseTo(0.0826 * 1200, 6); // 99.12
    expect(r.y).toBeCloseTo(0.1502 * 1697, 6); // 254.89
    expect(r.w).toBeCloseTo((0.9307 - 0.0826) * 1200, 6);
    expect(r.h).toBeCloseTo((0.2604 - 0.1502) * 1697, 6);
    // 框必须落在图内
    expect(r.x + r.w).toBeLessThanOrEqual(1200);
    expect(r.y + r.h).toBeLessThanOrEqual(1697);
  });

  it("❌ 直接把归一化值当像素用会把框挤在左上角", () => {
    // 这就是 §6.1 描述的失败：0.1502 像素 ≈ 一行都不到，框贴在页顶。
    const naive = { y: REAL_TEXT_LOC.bbox[1], h: REAL_TEXT_LOC.bbox[3] - REAL_TEXT_LOC.bbox[1] };
    const correct = toRect(REAL_TEXT_LOC.bbox, 1200, 1697)!;
    expect(naive.h).toBeLessThan(1); // 亚像素：什么都看不见
    expect(correct.h).toBeGreaterThan(100); // 真实高度（≈187px，三行正文）
  });

  it("与渲染分辨率无关：比例相同、绝对位置等比放大", () => {
    const a = toRect(REAL_TEXT_LOC.bbox, 1200, 1697)!;
    const b = toRect(REAL_TEXT_LOC.bbox, 2400, 3394)!;
    expect(b.x / a.x).toBeCloseTo(2, 6);
    expect(b.y / a.y).toBeCloseTo(2, 6);
    expect(b.w / a.w).toBeCloseTo(2, 6);
    expect(b.h / a.h).toBeCloseTo(2, 6);
  });

  it("非法 bbox 返回 null，而不是画一个 0×0 的框", () => {
    expect(toRect([0.5, 0.2, 0.5, 0.3], 1000, 1400)).toBeNull(); // 零宽
    expect(toRect([0.5, 0.2, 0.4, 0.3], 1000, 1400)).toBeNull(); // x1 < x0
    expect(toRect([0.1, 0.2, 0.4, Number.NaN], 1000, 1400)).toBeNull();
    expect(toRect([0.1, 0.2, 9.0, 0.3], 1000, 1400)).toBeNull(); // 离谱越界
    expect(toRect([0.1, 0.2, 0.4] as never, 1000, 1400)).toBeNull(); // 长度不对
  });

  it("容忍一点点浮点误差，但不容忍离谱值", () => {
    expect(validBbox([-0.004, 0.1, 0.5, 0.6])).toBe(true);
    expect(validBbox([0, 0, 1, 1])).toBe(true);
    expect(validBbox([0, 0, 1, 1.5])).toBe(false);
  });
});

// ============================================================
// 并集 / 分页 / 表格识别
// ============================================================

describe("矩形与分页", () => {
  it("unionRect 取并集，空数组返回 null", () => {
    expect(unionRect([])).toBeNull();
    expect(
      unionRect([
        { x: 10, y: 20, w: 30, h: 40 },
        { x: 100, y: 5, w: 20, h: 10 },
      ]),
    ).toEqual({ x: 10, y: 5, w: 110, h: 55 });
  });

  it("pagesOf 升序去重，locsOnPage 精确取页", () => {
    const locs: Location[] = [
      { page: 3, bbox: [0.1, 0.1, 0.2, 0.2] },
      { page: 1, bbox: [0.1, 0.1, 0.2, 0.2] },
      { page: 3, bbox: [0.3, 0.1, 0.4, 0.2] },
    ];
    expect(pagesOf(locs)).toEqual([1, 3]);
    expect(locsOnPage(locs, 3)).toHaveLength(2);
    expect(locsOnPage(locs, 2)).toHaveLength(0);
  });
});

// ============================================================
// D58 表格网格
// ============================================================

describe("D58 表格按 row/col 排网格", () => {
  it("靠 row/col 识别表格 —— 不是靠 kind", () => {
    expect(isTableLocs(REAL_TABLE_LOCS)).toBe(true);
    expect(isTableLocs([REAL_TEXT_LOC])).toBe(false);
  });

  it("分组后 row 升序、每行内 col 升序", () => {
    const { rows } = groupByRow([...REAL_TABLE_LOCS].reverse(), 1200, 1697);
    expect(rows.map((r) => r.row)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8]);
    for (const row of rows) {
      const cols = row.cells.map((c) => c.col);
      expect(cols).toEqual([...cols].sort((a, b) => a - b));
    }
  });

  it("40 个格子默认只画外框（D59：超出则只画外框）", () => {
    const plan = planOverlay({
      // ⚠️ `/search` 把表格 chunk 报成 `kind="text"`（引用类型，不是 chunks.kind）。
      // 这正是"按 kind 分派会画错"的真实形状。
      kind: "text",
      locations: REAL_TABLE_LOCS,
      pxW: 1200,
      pxH: 1697,
    });
    expect(plan.mode).toBe("grid");
    expect(plan.cellCount).toBe(40);
    expect(plan.rowsTotal).toBe(9);
    expect(plan.colsTotal).toBe(7);
    expect(plan.overflow).toBe(true);
    expect(plan.rects).toHaveLength(1); // 只有外框
    expect(plan.rows.flatMap((r) => r.cells)).toHaveLength(40); // 逐格仍在，可展开
    expect(plan.note).toContain("D58");
  });

  it("外框必须罩住所有格子", () => {
    const plan = planOverlay({
      kind: "text",
      locations: REAL_TABLE_LOCS,
      pxW: 1200,
      pxH: 1697,
    });
    const outer = plan.rects[0];
    const all = plan.rows.flatMap((r) => r.cells.map((c) => c.rect));
    for (const r of all) {
      expect(r.x).toBeGreaterThanOrEqual(outer.x - 1e-6);
      expect(r.y).toBeGreaterThanOrEqual(outer.y - 1e-6);
      expect(r.x + r.w).toBeLessThanOrEqual(outer.x + outer.w + 1e-6);
      expect(r.y + r.h).toBeLessThanOrEqual(outer.y + outer.h + 1e-6);
    }
  });

  it("格子数不多时逐格全画（那就是表格轮廓，D58）", () => {
    // e2e 实测的缺口：早先小表格也只画外框，又因为没到阈值而没有展开入口 ——
    // 用户既看不到行列也点不开。
    const few = REAL_TABLE_LOCS.slice(0, GRID_INLINE_LIMIT);
    const plan = planOverlay({ kind: "text", locations: few, pxW: 800, pxH: 1131 });
    expect(plan.mode).toBe("grid");
    expect(plan.overflow).toBe(false);
    expect(plan.rects).toHaveLength(GRID_INLINE_LIMIT); // 逐格
    // 并集仍等于所有格子的包围盒
    const u = unionRect(plan.rects)!;
    expect(u.w).toBeGreaterThan(0);
    expect(u.h).toBeGreaterThan(0);
  });

  it("外框在 overflow 两种情况下都罩住所有格子", () => {
    for (const locs of [REAL_TABLE_LOCS, REAL_TABLE_LOCS.slice(0, 5)]) {
      const plan = planOverlay({ kind: "text", locations: locs, pxW: 800, pxH: 1131 });
      const all = plan.rows.flatMap((r) => r.cells.map((c) => c.rect));
      const u = unionRect(plan.rects)!;
      for (const r of all) {
        expect(r.x).toBeGreaterThanOrEqual(u.x - 1e-6);
        expect(r.x + r.w).toBeLessThanOrEqual(u.x + u.w + 1e-6);
      }
    }
  });
});

// ============================================================
// 分派：正文 / 扫描件 / 无坐标
// ============================================================

describe("planOverlay 分派", () => {
  it("正文段落：单框", () => {
    const plan = planOverlay({
      kind: "text",
      locations: [REAL_TEXT_LOC],
      pxW: 1200,
      pxH: 1697,
    });
    expect(plan.mode).toBe("single");
    expect(plan.page).toBe(1);
    expect(plan.rects).toHaveLength(1);
  });

  it("D60 kind=page → 整页高亮，不画精确框", () => {
    // 扫描件 chunk 没有坐标，页码只能由调用方给（当前 ingest 不产生 kind=page，
    // 这里是给视觉腿接入后的路径留的）
    const plan = planOverlay({
      kind: "page",
      locations: null,
      page: 4,
      pxW: 1200,
      pxH: 1697,
    });
    expect(plan.mode).toBe("page");
    expect(plan.page).toBe(4);
    expect(plan.rects).toEqual([{ x: 0, y: 0, w: 1200, h: 1697 }]);
    expect(plan.note).toContain("D60");
  });

  it("kind=page 且没有页码时不硬画（画假框比不画更糟）", () => {
    const plan = planOverlay({ kind: "page", locations: null, pxW: 1200, pxH: 1697 });
    expect(plan.mode).toBe("none");
    expect(plan.rects).toHaveLength(0);
    expect(plan.note).toContain("整页");
  });

  it("无坐标：mode=none 但 note 必须有内容（不许静默不画）", () => {
    const plan = planOverlay({ kind: "image", locations: null, pxW: 800, pxH: 1131 });
    expect(plan.mode).toBe("none");
    expect(plan.note).toContain("图片");
    const chat = planOverlay({ kind: "text", locations: [], pxW: 800, pxH: 1131 });
    expect(chat.note).toContain("聊天");
  });

  it("框分布在多页：默认第一页，且 note 说明要翻页", () => {
    const locs: Location[] = [
      REAL_TEXT_LOC,
      { page: 2, bbox: [0.1, 0.1, 0.5, 0.2] },
      { page: 2, bbox: [0.1, 0.3, 0.5, 0.4] },
    ];
    const plan = planOverlay({ kind: "text", locations: locs, pxW: 1200, pxH: 1697 });
    expect(plan.pageCount).toBe(2);
    expect(plan.page).toBe(1);
    expect(plan.rects).toHaveLength(1); // 第 1 页只画第 1 页的框

    const p2 = planOverlay({ kind: "text", locations: locs, page: 2, pxW: 1200, pxH: 1697 });
    expect(p2.page).toBe(2);
    expect(p2.rects).toHaveLength(2);
    expect(p2.note).toContain("2 页");
  });

  it("非法坐标被剔除，不影响其余框", () => {
    const locs: Location[] = [
      REAL_TEXT_LOC,
      { page: 1, bbox: [0.9, 0.1, 0.2, 0.3] }, // x1 < x0
      { page: 1, bbox: [0.1, 0.4, 0.8, 0.45] },
    ];
    const plan = planOverlay({ kind: "text", locations: locs, pxW: 1200, pxH: 1697 });
    expect(plan.rects).toHaveLength(2);
  });
});