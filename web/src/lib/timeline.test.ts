/**
 * 时间线纯逻辑的单测（`lib/timeline.ts`）。
 *
 * 重点是**会静默出错**的地方：
 * - 月份加减跨年（JS 的 Date 在 `2025-13` 上会滚成 `2026-01`，
 *   而 `2025-00` 滚成 `2024-12` —— 看起来"能用"，其实错一年）
 * - 非法日期（`2025-02-30`）必须判否，否则日历上会出现一个不存在的格
 * - 无日期的照片不许被过滤掉
 * - "不适用"（非 chat/photo 库）与"没有记录"要能区分
 */

import { describe, expect, it } from "vitest";

import {
  anniversaryLabel,
  daysInMonth,
  dayMarks,
  emptyHint,
  groupPhotosByMonth,
  intensity,
  isDate,
  leadingBlanks,
  milestoneNote,
  monthDates,
  monthLabel,
  monthSummary,
  notApplicableHint,
  shiftMonth,
  speakerLine,
  supportsTimeline,
  todayOf,
  undatedLabel,
} from "./timeline";

describe("shiftMonth", () => {
  it("普通加减", () => {
    expect(shiftMonth("2025-02", 1)).toBe("2025-03");
    expect(shiftMonth("2025-02", -1)).toBe("2025-01");
  });

  it("🔑 跨年（12 月的下一页是次年 1 月）", () => {
    expect(shiftMonth("2025-12", 1)).toBe("2026-01");
    expect(shiftMonth("2025-01", -1)).toBe("2024-12");
  });

  it("🔑 大幅偏移（+13 / -13）", () => {
    expect(shiftMonth("2025-06", 13)).toBe("2026-07");
    expect(shiftMonth("2025-06", -13)).toBe("2024-05");
  });

  it("非法输入原样返回（不静默变成别的月份）", () => {
    for (const bad of ["", "2025", "2025-13", "2025-00", "abc", "2025-1"]) {
      expect(shiftMonth(bad, 1)).toBe(bad);
    }
  });
});

describe("daysInMonth / monthDates", () => {
  it("平年与闰年（2 月）", () => {
    expect(daysInMonth("2025-02")).toBe(28);
    expect(daysInMonth("2024-02")).toBe(29);
    expect(daysInMonth("2025-01")).toBe(31);
    expect(daysInMonth("2025-04")).toBe(30);
    expect(daysInMonth("2025-12")).toBe(31);
  });

  it("日期序列数量与天数一致", () => {
    expect(monthDates("2024-02")).toHaveLength(29);
    expect(monthDates("2025-02")).toHaveLength(28);
    expect(monthDates("2025-02")[0]).toBe("2025-02-01");
  });

  it("非法月份给 30（占位，不抛）", () => {
    expect(daysInMonth("2025-13")).toBe(30);
    expect(monthDates("bad")).toHaveLength(30);
  });
});

describe("isDate", () => {
  it("合法日期", () => {
    expect(isDate("2025-02-28")).toBe(true);
    expect(isDate("2024-02-29")).toBe(true);
  });

  it("🔑 不存在的日期判否（2 月 30 日）", () => {
    // `new Date("2025-02-30")` 会滚到 3 月 2 日，所以字符串判定更可靠
    expect(isDate("2025-02-30")).toBe(false);
    expect(isDate("2025-13-01")).toBe(false);
    expect(isDate("2025-00-10")).toBe(false);
    expect(isDate("2025-04-31")).toBe(false);
  });

  it("形状不对 / 空值", () => {
    for (const bad of ["", "2025-2-1", "2025/02/01", null, undefined, "昨天"]) {
      expect(isDate(bad)).toBe(false);
    }
  });
});

describe("leadingBlanks（月历对齐）", () => {
  it("月初是周一 → 0 个空格", () => {
    expect(leadingBlanks("2024-07")).toBe(0);   // 2024-07-01 是周一
  });

  it("🔑 月初是周日 → 6 个空格（周一起始）", () => {
    expect(leadingBlanks("2024-12")).toBe(6);   // 2024-12-01 是周日
  });

  it("月初是周三 → 2 个空格", () => {
    expect(leadingBlanks("2025-01")).toBe(2);   // 2025-01-01 是周三
  });

  it("闰年 2 月与非法输入", () => {
    expect(leadingBlanks("2024-02")).toBeGreaterThanOrEqual(0);
    expect(leadingBlanks("2024-02")).toBeLessThanOrEqual(6);
    expect(leadingBlanks("bad")).toBe(0);
  });

  it("🔑 空格数 + 天数 = 4~6 周（不越界成第 7 行）", () => {
    for (const m of ["2024-07", "2024-12", "2025-01", "2025-03", "2024-02"]) {
      const cells = leadingBlanks(m) + daysInMonth(m);
      // ⚠️ 不要求是 7 的倍数（最后一行可以是半行，那才是日历的样子）；
      //   要求的是**永远不超过 6 行** —— 超了就说明空格数算错了。
      expect(cells).toBeGreaterThan(27);
      expect(cells).toBeLessThanOrEqual(42);
    }
  });
});

describe("todayOf", () => {
  it("在本月时给出今天", () => {
    const now = new Date(2025, 1, 14);
    expect(todayOf("2025-02", now)).toBe("2025-02-14");
  });

  it("不在本月时给 null（不能高亮别的月的 14 号）", () => {
    expect(todayOf("2025-03", new Date(2025, 1, 14))).toBeNull();
  });
});

describe("monthLabel / monthSummary", () => {
  it("中文标签", () => {
    expect(monthLabel("2025-02")).toBe("2025 年 2 月");
  });

  it("摘要区分三种情况", () => {
    expect(monthSummary(0, 0)).toBe("这个月没有记录");
    expect(monthSummary(100, 0)).toBe("100 条记录");
    expect(monthSummary(100, 12)).toBe("100 条记录 · 12 天有内容");
  });
});

describe("speakerLine", () => {
  it("按条数降序，只留前 N", () => {
    expect(speakerLine({ 甲: 10, 乙: 30, 丙: 20 }, 2)).toBe("乙 30 · 丙 20");
  });

  it("同条数时按名字排序（顺序稳定，界面不跳）", () => {
    expect(speakerLine({ 乙: 5, 甲: 5 })).toBe("甲 5 · 乙 5");
  });

  it("空输入给空串（不显示 '0 · 0'）", () => {
    expect(speakerLine({})).toBe("");
    expect(speakerLine({ 甲: 0 })).toBe("");
  });
});

describe("dayMarks / intensity", () => {
  const marks = dayMarks([
    { date: "2025-02-14", total: 300, photo_count: 0 },
    { date: "2025-02-15", total: 0, photo_count: 2 },
    { date: "bad-date", total: 5, photo_count: 0 },
  ]);

  it("只收合法日期（坏数据不许进日历）", () => {
    expect(Object.keys(marks).sort()).toEqual(["2025-02-14", "2025-02-15"]);
  });

  it("强度分级：有照片或消息很多 → 3", () => {
    expect(intensity(undefined)).toBe(0);
    expect(intensity({ total: 0, photos: 0 })).toBe(0);
    expect(intensity({ total: 10, photos: 0 })).toBe(1);
    expect(intensity({ total: 60, photos: 0 })).toBe(2);
    expect(intensity({ total: 300, photos: 0 })).toBe(3);
    expect(intensity({ total: 1, photos: 1 })).toBe(3);
  });
});

describe("groupPhotosByMonth", () => {
  const photos = [
    { doc_id: 1, file_name: "a.jpg", date: "2025-02-14" },
    { doc_id: 2, file_name: "b.jpg", date: "2025-03-01" },
    { doc_id: 3, file_name: "c.jpg", date: "2025-02-20" },
    { doc_id: 4, file_name: "d.jpg", date: null },
  ];

  it("按月分组，新的月在前", () => {
    const g = groupPhotosByMonth(photos);
    expect(g.map((x) => x.month)).toEqual(["2025-03", "2025-02", null]);
  });

  it("月内按日期倒序", () => {
    const feb = groupPhotosByMonth(photos).find((g) => g.month === "2025-02");
    expect(feb?.photos.map((p) => p.doc_id)).toEqual([3, 1]);
  });

  it("🔑 无日期的照片排在最后但**不丢**（过滤掉=用户以为照片丢了）", () => {
    const g = groupPhotosByMonth(photos);
    const last = g[g.length - 1];
    expect(last.month).toBeNull();
    expect(last.photos).toHaveLength(1);
    expect(undatedLabel(1)).toContain("不参与时间线");
  });

  it("日期非法的照片也归到无日期那档", () => {
    const g = groupPhotosByMonth([{ doc_id: 9, file_name: "x.jpg", date: "2025-02-30" }]);
    expect(g).toHaveLength(1);
    expect(g[0].month).toBeNull();
  });

  it("空输入", () => {
    expect(groupPhotosByMonth([])).toEqual([]);
  });
});

describe("纪念日 / 里程碑文案", () => {
  it("纪念日突出「有照片」（契约原话）", () => {
    expect(anniversaryLabel({ date: "2024-07-31", count: 2, chat_total: 264 })).toBe(
      "2024-07-31 · 2 张照片 · 264 条消息",
    );
  });

  it("没有消息时不硬凑一段", () => {
    expect(anniversaryLabel({ date: "2024-07-31", count: 2, chat_total: 0 })).toBe(
      "2024-07-31 · 2 张照片",
    );
  });

  it("🔑 里程碑如实说「按消息量」（不写成「最有意义的一天」）", () => {
    expect(milestoneNote(743)).toBe("当天 743 条消息");
    expect(milestoneNote(0)).toBe("当天没有消息记录");
  });
});

describe("不适用 vs 没有记录", () => {
  it("只有 chat/photo 库能看时间线", () => {
    expect(supportsTimeline("chat")).toBe(true);
    expect(supportsTimeline("photo")).toBe(true);
    expect(supportsTimeline("file")).toBe(false);
    expect(supportsTimeline(null)).toBe(false);
  });

  it("🔑 提示要说清「不是没记录、是不适用」", () => {
    const hint = notApplicableHint("file");
    expect(hint).toContain("只对聊天库");
    expect(hint).toContain("file");
  });

  it("400 → 不适用；其它错误 → 加载失败；无错 → 没有记录", () => {
    expect(emptyHint({ status: 400 }, false)).toContain("只对聊天库");
    expect(emptyHint({ status: 500 }, false)).toContain("HTTP 500");
    expect(emptyHint(null, true)).toBe("这个范围内没有记录");
    expect(emptyHint(null, false)).toBe("还没有任何记录");
  });
});