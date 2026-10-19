/**
 * 进度展示的决策层。
 *
 * fixture 不是编的 —— 是 `eval/api_1c_check.py` 跑一次真实摄取
 * （15 页 PDF / 74 chunk）时从 `GET /jobs?doc_id=…` 轮询下来的**实际序列**：
 *
 * ```text
 * progress 0.00 → 0.02 → 0.45 → 0.56 → 0.67 → 0.79 → 1.00
 * status   pending running running running running running done
 * ```
 *
 * 用真实序列才能覆盖"0.45 之后连续三个 0.1x 的台阶"这种形状 ——
 * 编出来的 `[0, 0.5, 1]` 会漏掉"进度卡在 0.45 不动"这类问题。
 */

import { describe, expect, it } from "vitest";

import {
  clampPct,
  groupJobsByDoc,
  hasActiveJob,
  jobView,
  pickDocJob,
} from "./progress";
import type { Job } from "../types/api";

const T0 = "2026-10-03T10:00:00+00:00";

function job(over: Partial<Job> = {}): Job {
  return {
    id: 501,
    kind: "ingest",
    doc_id: 123,
    kb_id: 88,
    status: "running",
    progress: 0.45,
    attempts: 1,
    error: null,
    payload: null,
    created_at: T0,
    updated_at: T0,
    ...over,
  };
}

/** 真实采样序列（见文件头）。 */
const REAL_SEQUENCE: Array<[number, Job["status"]]> = [
  [0.0, "pending"],
  [0.02, "running"],
  [0.45, "running"],
  [0.56, "running"],
  [0.67, "running"],
  [0.79, "running"],
  [1.0, "done"],
];

describe("clampPct", () => {
  it("0~1 → 0~100 的整数", () => {
    expect(clampPct(0)).toBe(0);
    expect(clampPct(0.02)).toBe(2);
    expect(clampPct(0.455)).toBe(46); // 四舍五入，不是截断
    expect(clampPct(1)).toBe(100);
  });

  it("越界与非数都要夹住（DB 里出现 1.3 时不能显示 130%）", () => {
    expect(clampPct(1.3)).toBe(100);
    expect(clampPct(-0.2)).toBe(0);
    expect(clampPct(Number.NaN)).toBe(0);
    expect(clampPct(Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe("jobView", () => {
  it("没有任务 → null（不显示进度条）", () => {
    // ⚠️ 这条是"不许编数字"的第一道闸：没有 jobs 行时不能显示 0%
    expect(jobView(null)).toBeNull();
  });

  it("真实序列：每一帧都能显示，且百分比单调不减", () => {
    const pcts = REAL_SEQUENCE.map(([p, s]) => {
      const v = jobView(job({ progress: p, status: s }));
      expect(v).not.toBeNull();
      return v!.pct;
    });
    expect(pcts).toEqual([0, 2, 45, 56, 67, 79, 100]);
    for (let i = 1; i < pcts.length; i++) {
      expect(pcts[i]).toBeGreaterThanOrEqual(pcts[i - 1]);
    }
  });

  it("排队中（pending 且 progress=0）说人话，不显示 0%", () => {
    const v = jobView(job({ status: "pending", progress: 0 }));
    expect(v!.label).toBe("排队中");
    expect(v!.showBar).toBe(false);
  });

  it("pending 但已有进度 → 显示百分比（worker 已领走）", () => {
    // 重试场景：attempts>1 时 worker 会把 status 放回 pending，而 progress 保留
    const v = jobView(job({ status: "pending", progress: 0.45, attempts: 2 }));
    expect(v!.pct).toBe(45);
    expect(v!.label).toContain("45%");
    expect(v!.showBar).toBe(true);
  });

  it("running → 有条有字", () => {
    const v = jobView(job({ status: "running", progress: 0.56 }));
    expect(v).toMatchObject({ pct: 56, tone: "working", showBar: true });
    expect(v!.label).toContain("解析入库");
  });

  it("done → 100% 且不画条", () => {
    const v = jobView(job({ status: "done", progress: 1 }));
    expect(v).toMatchObject({ pct: 100, tone: "ready", showBar: false });
  });

  it("failed → 显示原因，不画条（停在 67% 的条会被读成「再等等」）", () => {
    const v = jobView(
      job({ status: "failed", progress: 0.67, error: "UnsupportedFormat: x.doc" }),
    );
    expect(v!.tone).toBe("failed");
    expect(v!.showBar).toBe(false);
    expect(v!.label).toContain("UnsupportedFormat");
  });

  it("failed 且无 error → 也要说「失败」，不能只显示一个空徽章", () => {
    const v = jobView(job({ status: "failed", progress: 0.2, error: null }));
    expect(v!.label).toContain("失败");
  });

  it("cancelled 与 failed 不能混（前者是被删/被覆盖，不是出错）", () => {
    const v = jobView(job({ status: "cancelled", progress: 0.3 }));
    expect(v!.tone).toBe("cancelled");
    expect(v!.label).toContain("取消");
    expect(v!.tone).not.toBe("failed");
  });

  it("文档已 ready 而任务行还没收尾 → 以文档状态为准，不显示 99%", () => {
    // D14 允许这个几毫秒的窗口；显示"99%"是在说假话
    const v = jobView(job({ status: "running", progress: 0.97 }), "ready");
    expect(v).toMatchObject({ pct: 100, label: "完成", tone: "ready", showBar: false });
  });

  it("视觉编码任务有自己的措辞（不是「解析入库」）", () => {
    const v = jobView(job({ kind: "embed_visual", progress: 0.3 }));
    expect(v!.label).toContain("视觉编码");
  });

  it("未知 kind 也显示得出（不能白屏或显示 undefined）", () => {
    const v = jobView(job({ kind: "brand_new_kind" as Job["kind"], progress: 0.5 }));
    expect(v!.label).toContain("brand_new_kind");
  });

  it("error 很长时截断（一行塞不下整段堆栈）", () => {
    const v = jobView(job({ status: "failed", error: "x".repeat(500) }));
    expect(v!.label.length).toBeLessThan(100);
  });
});

describe("pickDocJob", () => {
  it("优先选 ingest（视觉腿不是「这件事在推进」的代表）", () => {
    const jobs = [
      job({ id: 1, kind: "embed_visual", progress: 0.9 }),
      job({ id: 2, kind: "ingest", progress: 0.4 }),
    ];
    expect(pickDocJob(jobs, 123)?.kind).toBe("ingest");
  });

  it("没有 ingest 时取 id 最大的（视觉腿按页拆成多条）", () => {
    const jobs = [
      job({ id: 7, kind: "embed_visual", progress: 0.1 }),
      job({ id: 9, kind: "embed_visual", progress: 0.3 }),
      job({ id: 8, kind: "embed_visual", progress: 0.2 }),
    ];
    expect(pickDocJob(jobs, 123)?.id).toBe(9);
  });

  it("别的文档的任务不能被选中", () => {
    expect(pickDocJob([job({ doc_id: 999 })], 123)).toBeNull();
  });

  it("doc_id 为 null 的任务（运维类）不落到任何文档上", () => {
    // cleanup / reindex 的 kb_id 与 doc_id 都是 NULL —— 它们不该出现在文档行上。
    // ⚠️ 不再用 -1 这种假 key 兜底：那会让"没有文档的任务"看起来像属于某个文档。
    const jobs = [job({ id: 1, doc_id: null, kind: "cleanup" })];
    expect(pickDocJob(jobs, 123)).toBeNull();
    expect(groupJobsByDoc(jobs).size).toBe(0);
  });

  it("分组与单挑的规则必须一致（否则列表页与详情页显示不同任务）", () => {
    // 这条是本文件里最有价值的一条：两个函数是同一件事的两种问法，
    // 规则一漂移，"数据对但显示在骗人"就发生了。
    const jobs = [
      job({ id: 1, doc_id: 7, kind: "embed_visual", progress: 0.9 }),
      job({ id: 2, doc_id: 7, kind: "ingest", progress: 0.4 }),
      job({ id: 3, doc_id: 8, kind: "embed_visual", progress: 0.2 }),
      job({ id: 4, doc_id: 8, kind: "embed_visual", progress: 0.5 }),
    ];
    const grouped = groupJobsByDoc(jobs);
    for (const docId of [7, 8]) {
      expect(grouped.get(docId)?.id).toBe(pickDocJob(jobs, docId)?.id);
    }
    expect(grouped.get(7)?.kind).toBe("ingest");
    expect(grouped.get(8)?.id).toBe(4); // 没有 ingest → id 最大
  });
});

describe("groupJobsByDoc", () => {
  it("每个文档只留一条代表任务", () => {
    const map = groupJobsByDoc([
      job({ id: 1, doc_id: 1, kind: "ingest", status: "done" }),
      job({ id: 2, doc_id: 2, kind: "ingest", status: "running" }),
      job({ id: 3, doc_id: 1, kind: "embed_visual", status: "running" }),
    ]);
    expect(map.size).toBe(2);
    expect(map.get(1)?.kind).toBe("ingest");
    expect(map.get(2)?.status).toBe("running");
  });
});

describe("hasActiveJob", () => {
  it("全终态 → false（可以停轮询）", () => {
    expect(
      hasActiveJob([
        job({ status: "done" }),
        job({ id: 2, status: "failed" }),
        job({ id: 3, status: "cancelled" }),
      ]),
    ).toBe(false);
  });

  it("还有 pending/running → true（不能提前收工）", () => {
    expect(hasActiveJob([job({ status: "done" }), job({ id: 2, status: "running" })])).toBe(
      true,
    );
    expect(hasActiveJob([job({ status: "pending", progress: 0 })])).toBe(true);
  });

  it("空列表 → false", () => {
    expect(hasActiveJob([])).toBe(false);
  });
});