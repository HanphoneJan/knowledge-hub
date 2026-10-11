/**
 * 批量汇总进度的单测。
 *
 * fixture 里的百分比不是编的 —— 是一次真实摄取采样到的序列
 * （`eval/api_1c_check.py` 同一套代码路径）：
 * `0 → 0.02 → 0.45 → 0.56 → 0.67 → 0.79 → 1.00`
 */

import { describe, expect, it } from "vitest";

import {
  batchLabel,
  batchProgress,
  batchSettled,
  splitBatches,
  unsubmittedAfter,
  uploadPhaseLabel,
  type BatchItem,
} from "./upload";
import type { ProgressView } from "./progress";

function view(over: Partial<ProgressView> = {}): ProgressView {
  return { pct: 0, label: "排队中", tone: "working", showBar: false, ...over };
}

function item(docId: number, v: ProgressView | null, name = `d${docId}.pdf`): BatchItem {
  return { docId, name, view: v };
}

const RUNNING = view({ pct: 56, label: "解析入库 56%", tone: "working", showBar: true });
const QUEUED = view({ pct: 0, label: "排队中", tone: "working", showBar: false });
const DONE = view({ pct: 100, label: "解析入库完成", tone: "ready", showBar: false });
const FAILED = view({ pct: 67, label: "解析入库失败：boom", tone: "failed", showBar: false });
const CANCELLED = view({ pct: 0, label: "解析入库已取消", tone: "cancelled", showBar: false });

describe("splitBatches（D45：单请求 ≤20 个）", () => {
  it("按 20 一批切", () => {
    expect(splitBatches(0)).toBe(0);
    expect(splitBatches(1)).toBe(1);
    expect(splitBatches(20)).toBe(1);
    expect(splitBatches(21)).toBe(2);
    expect(splitBatches(52)).toBe(3);
  });
});

describe("uploadPhaseLabel（上传阶段不给百分比）", () => {
  it("⚠️ 绝不显示百分比 —— fetch 拿不到上传进度，显示就是编", () => {
    const l = uploadPhaseLabel({ total: 5, submitted: 2, batches: 1, batchesDone: 0 });
    expect(l).not.toMatch(/\d+%/);
  });

  it("单批时只说份数", () => {
    expect(uploadPhaseLabel({ total: 5, submitted: 2, batches: 1, batchesDone: 0 })).toBe(
      "正在上传 2/5 份…",
    );
  });

  it("多批时说清第几批（否则 20 份的用户不知道要等多久）", () => {
    expect(
      uploadPhaseLabel({ total: 52, submitted: 20, batches: 3, batchesDone: 1 }),
    ).toBe("正在上传第 2/3 批（已提交 20/52 份）");
  });

  it("全部提交完 → 切到「等摄取」（两件事不要混成一句）", () => {
    expect(
      uploadPhaseLabel({ total: 52, submitted: 52, batches: 3, batchesDone: 3 }),
    ).toBe("已提交 52 份，等摄取…");
  });

  it("批号不会算出第 4/3 批（边界）", () => {
    const l = uploadPhaseLabel({ total: 60, submitted: 59, batches: 3, batchesDone: 3 });
    expect(l).toContain("3/3");
  });

  it("空 → 空字符串（不显示「上传中」）", () => {
    expect(uploadPhaseLabel({ total: 0, submitted: 0, batches: 0, batchesDone: 0 })).toBe("");
  });
});

describe("batchProgress", () => {
  it("真实序列：4 份分别在不同阶段", () => {
    const bp = batchProgress([
      item(1, DONE),
      item(2, RUNNING), // 56%
      item(3, QUEUED), // 0%
      item(4, FAILED), // 不进平均
    ]);
    expect(bp).toMatchObject({ total: 4, done: 1, failed: 1, active: 2, remaining: 2 });
    // (100 + 56 + 0) / 3 = 52
    expect(bp.avgPct).toBe(52);
  });

  it("🔑 失败文档不进平均（否则进度条永远卡在 90%）", () => {
    const items = [
      ...Array.from({ length: 9 }, () => item(0, DONE)),
      item(99, FAILED),
    ];
    const bp = batchProgress(items);
    expect(bp.failed).toBe(1);
    expect(bp.avgPct).toBe(100); // 不是 90 —— 那正是"卡住"的观感
    expect(bp.remaining).toBe(0); // 10 份里 9 成 1 败，没有还在等的
    expect(bp.hasFailed).toBe(true);
  });

  it("没有任务行的文档**不算 0%**（没数据 ≠ 进度为零）", () => {
    const bp = batchProgress([item(1, RUNNING), item(2, null)]);
    expect(bp.total).toBe(2);
    // 只有一份有数据 → 平均就是它自己的 56
    expect(bp.avgPct).toBe(56);
    expect(bp.active).toBe(1);
  });

  it("cancelled 既不算完成也不算失败（被覆盖的旧版本）", () => {
    const bp = batchProgress([item(1, DONE), item(2, CANCELLED)]);
    expect(bp.done).toBe(1);
    expect(bp.failed).toBe(0);
    expect(bp.avgPct).toBe(100);
    // 它仍在 remaining 里 —— 因为"这次上传的这份"确实没成
    expect(bp.remaining).toBe(1);
  });

  it("空列表", () => {
    expect(batchProgress([])).toMatchObject({ total: 0, done: 0, avgPct: 0, remaining: 0 });
    expect(batchSettled(batchProgress([]))).toBe(false); // 空不算"已结束"
  });
});

describe("batchLabel", () => {
  it("进行中：几份完成 + 百分比 + 进行中份数", () => {
    const l = batchLabel(batchProgress([item(1, DONE), item(2, RUNNING), item(3, QUEUED)]));
    expect(l).toContain("1/3 份完成");
    expect(l).toContain("52%");
    expect(l).toContain("2 份进行中");
  });

  it("🔑 全结束且有失败时不能说「全部完成」", () => {
    const l = batchLabel(batchProgress([item(1, DONE), item(2, FAILED)]));
    expect(l).toContain("1 份失败");
    expect(l).toMatch(/全部结束/);
    expect(l).not.toContain("全部完成");
  });

  it("全成功", () => {
    expect(batchLabel(batchProgress([item(1, DONE), item(2, DONE)]))).toBe("全部结束：2 份完成");
  });

  it("空 → 空字符串", () => {
    expect(batchLabel(batchProgress([]))).toBe("");
  });
});

describe("batchSettled", () => {
  it("全 done → true（可以停轮询）", () => {
    expect(batchSettled(batchProgress([item(1, DONE)]))).toBe(true);
  });

  it("有失败也算结束（失败不会自己变好，继续轮询是白费）", () => {
    expect(batchSettled(batchProgress([item(1, DONE), item(2, FAILED)]))).toBe(true);
  });

  it("还有进行中 → false", () => {
    expect(batchSettled(batchProgress([item(1, DONE), item(2, RUNNING)]))).toBe(false);
  });
});

describe("unsubmittedAfter（上传中断时）", () => {
  it("报「还有几份没提交」，否则用户会重传产生重复文档", () => {
    expect(unsubmittedAfter(52, 20)).toBe(32);
    expect(unsubmittedAfter(52, 52)).toBe(0);
    expect(unsubmittedAfter(5, 0)).toBe(5);
    // 防御：submitted 大于 total 时不返回负数
    expect(unsubmittedAfter(5, 9)).toBe(0);
  });
});