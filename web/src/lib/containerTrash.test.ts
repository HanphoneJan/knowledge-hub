/**
 * 🎯 库 / 工作区回收站的纯逻辑（D129）。
 *
 * 重点测三件事：
 * 1. **三态**（可恢复 / 已过期 / 被工作区挡住）都要能出现
 * 2. `blocked` 与 `expired` **不给能点的按钮** —— 点了就是 403 或白点
 * 3. 到期天数**复用** `trash.ts::daysLeft`，不重写一份
 */

import { describe, expect, it } from "vitest";

import {
  canRestore,
  emptyHint,
  restoreState,
  restoredHint,
  type TrashEntry,
} from "./containerTrash";
import { daysLeft } from "./trash";

const NOW = Date.parse("2026-10-04T00:00:00Z");
const daysAgo = (n: number) => new Date(NOW - n * 86_400_000).toISOString();

const kb = (over: Partial<TrashEntry> = {}): TrashEntry => ({
  id: 7,
  name: "我的库",
  deleted_at: daysAgo(3),
  workspace_id: 11,
  ...over,
});

describe("restoreState", () => {
  it("刚删的库 → 可恢复，并说清还有几天", () => {
    const s = restoreState(kb(), "kb", [], NOW);
    expect(s.kind).toBe("restore");
    expect(canRestore(s)).toBe(true);
    expect(s.hint).toContain("27 天后到期");
  });

  it("🔑 工作区在回收站里 → blocked，且**不能点**", () => {
    // ⚠️ 这是"看得见却恢复不了"的那一类：后端 GET /kb/trash 干脆不列它，
    // 所以界面上遇到它只可能是"工作区先被删了"的顺序 —— 必须说清先恢复哪个。
    const s = restoreState(kb({ workspace_id: 11 }), "kb", [11], NOW);
    expect(s.kind).toBe("blocked");
    expect(canRestore(s)).toBe(false);
    expect(s.hint).toContain("工作区");
  });

  it("工作区**不在**回收站里 → 不该被 blocked（别误伤）", () => {
    const s = restoreState(kb({ workspace_id: 12 }), "kb", [11], NOW);
    expect(s.kind).toBe("restore");
  });

  it("🔑 过期 → expired，且**不能点**", () => {
    const s = restoreState(kb({ deleted_at: daysAgo(40) }), "kb", [], NOW);
    expect(s.kind).toBe("expired");
    expect(canRestore(s)).toBe(false);
    expect(s.hint).toContain("已过期");
  });

  it("恰好到期那天（30 天）算过期", () => {
    expect(daysLeft(daysAgo(30), NOW)).toBe(0);
    expect(restoreState(kb({ deleted_at: daysAgo(30) }), "kb", [], NOW).kind)
      .toBe("expired");
  });

  it("29 天 → 还能恢复（边界另一侧）", () => {
    expect(restoreState(kb({ deleted_at: daysAgo(29) }), "kb", [], NOW).kind)
      .toBe("restore");
  });

  it("🔑 blocked 优先于 expired：先说卡在哪，别只说过期", () => {
    // 两条都为真时（工作区已删 + 自己也过期了）——
    // 先说"先恢复工作区"会让用户去点一个恢复不回来的东西。
    // ⚠️ 这里的期望是**本实现**的选择；真正重要的是它被钉住了，
    // 而不是"必须这样"（§5.114：断言要比实现的正确集合更弱）。
    const s = restoreState(
      kb({ workspace_id: 11, deleted_at: daysAgo(40) }),
      "kb",
      [11],
      NOW,
    );
    expect(s.kind).toBe("blocked");
  });

  it("deleted_at 缺失 → 当作还有 30 天（而不是「过期」）", () => {
    // ⚠️ 老数据可能没有这个字段。判成"已过期"会让用户以为东西没了，
    // 而它其实可能还能恢复 —— 那是**劝退**而不是提示。
    const s = restoreState(kb({ deleted_at: null }), "kb", [], NOW);
    expect(s.kind).toBe("restore");
    expect(s.hint).toContain("30 天后到期");
  });

  it("工作区自己不会有 blocked（它不依赖别人）", () => {
    const s = restoreState(
      { id: 3, name: "ws", deleted_at: daysAgo(1) },
      "workspace",
      [3, 4],
      NOW,
    );
    expect(s.kind).toBe("restore");
  });
});

describe("文案", () => {
  it("空回收站说的是**状态**不是评价", () => {
    expect(emptyHint()).toContain("回收站是空的");
    expect(emptyHint()).toContain("30 天");
  });

  it("🔑 恢复库的提示必须说清要重新摄取", () => {
    // D44：旧向量只留 3 天，所以恢复后文档要重新处理。
    // 不说这句，用户刷新发现文档不在列表里会以为恢复失败又点一次。
    expect(restoredHint("kb")).toContain("重新处理");
    expect(restoredHint("workspace")).toBe("工作区已恢复");
  });
});