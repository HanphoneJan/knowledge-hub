/**
 * 调试面板纯逻辑的单测（`lib/debug.ts`）。
 *
 * 重点是**会把用户引向错误结论**的四处：
 * skipped 的腿被当成"没命中"、单腿说成多路、RRF 分当置信度、
 * 共识度（几条腿捞到）算错。
 */

import { describe, expect, it } from "vitest";

import type { LegName } from "./debug";
import {
  consensusText,
  emptyDebugHint,
  filteredNote,
  fusionSummary,
  hitConsensus,
  legState,
  legViews,
  LEG_ORDER,
  rerankText,
  routeText,
  rrfScoreText,
  slowestLeg,
} from "./debug";

describe("legState：skipped 不等于 empty", () => {
  it("被跳过的腿是 skipped（🔑 不能判成 empty）", () => {
    expect(legState({ n: 0, skipped: "视觉编码只在 worker 进程加载" })).toBe("skipped");
    expect(legState({ n: 0, skipped: "" })).toBe("empty");   // 空串不算跳过
  });

  it("跑了但没命中是 empty", () => {
    expect(legState({ n: 0, skipped: null })).toBe("empty");
  });

  it("有命中是 ok", () => {
    expect(legState({ n: 3, skipped: null })).toBe("ok");
  });

  it("🔑 缺席的腿是 empty 而不是 skipped（没这个键 ≠ 被跳过）", () => {
    expect(legState(undefined)).toBe("empty");
  });
});

describe("legViews", () => {
  it("按固定顺序输出四条腿，缺的补上", () => {
    const views = legViews({ sparse: { n: 2 } });
    expect(views.map((v) => v.name)).toEqual(LEG_ORDER);
    expect(views.find((v) => v.name === "sparse")?.state).toBe("ok");
    expect(views.find((v) => v.name === "dense")?.state).toBe("empty");
  });

  it("保留 skipped 的原因文本", () => {
    const views = legViews({ visual: { n: 0, skipped: "D50 只在 worker 加载" } });
    expect(views.find((v) => v.name === "visual")?.reason).toBe("D50 只在 worker 加载");
  });

  it("空输入", () => {
    expect(legViews({})).toHaveLength(4);
    expect(legViews(undefined as never)).toHaveLength(4);
  });
});

describe("fusionSummary", () => {
  // ⚠️ `name` 必须标注成 `LegName`：LegView.name 收窄成联合类型后，
  //   这里若让它推成 `string` 就构造不出 LegView（tsc 挡住拼错的腿名）。
  const v = (name: LegName, state: "ok" | "empty" | "skipped") => ({
    name,
    label: { dense: "向量（语义）", sparse: "BM25（关键词）", trgm: "子串", visual: "视觉" }[name]!,
    state,
    n: state === "ok" ? 1 : 0,
    ms: 1,
    reason: state === "skipped" ? "只在 worker 加载" : null,
  });

  it("🔑 只有一条腿参与 → 明说「单腿」", () => {
    const s = fusionSummary([v("dense", "ok"), v("sparse", "skipped"), v("trgm", "skipped")]);
    expect(s).toContain("单腿检索");
    expect(s).not.toContain("多路融合");
  });

  it("两条腿以上 → 说多路融合并列名字", () => {
    const s = fusionSummary([v("dense", "ok"), v("sparse", "ok"), v("visual", "skipped")]);
    expect(s).toContain("多路融合 2 条腿");
    expect(s).toContain("跳过 1 条");
  });

  it("全被跳过 → 说清是环境问题", () => {
    const s = fusionSummary([
      v("dense", "skipped"), v("sparse", "skipped"), v("trgm", "skipped"), v("visual", "skipped"),
    ]);
    expect(s).toBe("没有腿参与检索");
  });

  it("跑了都没命中 → 带上「没有腿命中」", () => {
    const s = fusionSummary([v("dense", "empty"), v("sparse", "empty"), v("visual", "skipped")]);
    expect(s).toContain("没有腿命中");
  });
});

describe("共识度（几条腿捞到）", () => {
  it("数 legs 的键数", () => {
    expect(hitConsensus({ dense: 1, sparse: 2 })).toBe(2);
    expect(hitConsensus({ dense: 1 })).toBe(1);
    expect(hitConsensus({})).toBe(0);
    expect(hitConsensus(undefined)).toBe(0);
  });

  it("🔑 单腿要说「可能是巧合」，不夸大", () => {
    expect(consensusText({ dense: 1 })).toContain("巧合");
    expect(consensusText({ dense: 1, sparse: 2 })).toContain("2 条腿共同捞到");
    expect(consensusText({})).toBe("");
  });
});

describe("RRF 分不是置信度", () => {
  it("保留 4 位小数、不给百分比", () => {
    expect(rrfScoreText(0.0326)).toBe("0.0326");
    expect(rrfScoreText(0.0326)).not.toContain("%");
  });

  it("空/NaN", () => {
    expect(rrfScoreText(undefined)).toBe("-");
    expect(rrfScoreText(Number.NaN)).toBe("-");
  });
});

describe("slowestLeg", () => {
  it("排除 total_ms，取最慢那条", () => {
    expect(slowestLeg({ dense: 88, sparse: 30, total_ms: 312 })).toEqual({
      name: "dense",
      ms: 88,
    });
  });

  it("空输入给 null", () => {
    expect(slowestLeg({})).toBeNull();
    expect(slowestLeg(undefined)).toBeNull();
  });
});

describe("routeText / rerankText", () => {
  it("无路由 → 说等权", () => {
    expect(routeText(null)).toBe("未路由（等权 RRF）");
  });

  it("有路由 → 判定 + 权重 + why（不能是黑盒）", () => {
    const s = routeText({ intent: "photo", weights: { visual: 3 }, why: "有照片" });
    expect(s).toContain("photo");
    expect(s).toContain("视觉×3");
    expect(s).toContain("有照片");
  });

  it("未配模型 → 说未开启；配了但 skipped → 说原因", () => {
    expect(rerankText(null)).toContain("未开启精排");
    expect(rerankText({ model: "v2-m3", applied: false, skipped: "没有候选" })).toContain(
      "未生效：没有候选",
    );
    expect(rerankText({ model: "v2-m3", applied: true, skipped: null })).toContain("已生效");
  });
});

describe("filteredNote / emptyDebugHint", () => {
  it("软删过滤数 >0 要说（否则索引里的幽灵结果无人解释）", () => {
    expect(filteredNote(3)).toContain("3 条");
    expect(filteredNote(0)).toBe("");
    expect(filteredNote(undefined)).toBe("");
  });

  it("有结果时不显示空提示", () => {
    expect(emptyDebugHint(legViews({ dense: { n: 1 } }), 1)).toBe("");
  });

  it("🔑 全腿被跳过 → 说是环境问题而不是「没匹配」", () => {
    const all = Object.fromEntries(
      LEG_ORDER.map((n) => [n, { n: 0, skipped: "只在 worker 加载" }]),
    );
    const h = emptyDebugHint(legViews(all), 0);
    expect(h).toContain("环境问题");
    // ⚠️ 不能断言 `not.toContain("没匹配")`：文案里**故意**引用了这两个字
    //   来消歧（"不是「没匹配」"）。要断言的是那半句消歧在不在。
    expect(h).toContain("不是「没匹配」");
  });

  it("🔑 混合（几条没命中 + 一条被跳过）→ 两件事都要说清", () => {
    // ⚠️ 这是最常见的情况：**缺席**的腿判成 `empty`，只有显式 `skipped`
    //   的那条才是 skipped。第一版用 `every(skipped)` 判，于是这里落进兜底
    //   只显示"没有可用的检索腿" —— 真相（3 条跑了没命中、视觉压根没跑）被吞了。
    const views = legViews({
      dense: { n: 0 },
      sparse: { n: 0 },
      visual: { n: 0, skipped: "视觉编码只在 worker 进程加载（D50）" },
    });
    const h = emptyDebugHint(views, 0);
    expect(h).toContain("3 条腿跑了都没匹配上");
    expect(h).toContain("1 条腿被跳过");
    // ⚠️ 要显示**为什么**被跳过，不能只说跳过了
    expect(h).toContain("D50");
    expect(h).not.toContain("没有可用的检索腿");
  });

  it("腿跑了但没命中 → 给可操作建议", () => {
    const views = legViews({ dense: { n: 0 }, sparse: { n: 0 } });
    expect(emptyDebugHint(views, 0)).toContain("关键词");
  });
});