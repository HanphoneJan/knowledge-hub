/**
 * 守卫：切分策略的前端投影（D164 / D190）。
 *
 * 判据指向**性质**（默认值、覆盖完整、前提条件、必须说清的三句话），
 * 不比对文案字符串 —— 文案会改，性质不该改。
 */
import { describe, expect, it } from "vitest";

import type { ChunkStrategy } from "../../types/api";
import {
  DEFAULT_STRATEGY,
  isStrategy,
  reparseAllPlan,
  STRATEGY_META,
  STRATEGY_ORDER,
  strategyChangeNotice,
  strategyOf,
  strategyOptions,
} from "./strategy";

describe("切分策略的前端投影", () => {
  it("覆盖契约里的六个取值，不重不漏", () => {
    // 与 `services/ingest/strategies.STRATEGY_NAMES` 同集合（后端有守卫钉住那边）
    const contract: ChunkStrategy[] = [
      "fixed",
      "recursive",
      "semantic",
      "parent_child",
      "late",
      "contextual",
    ];
    expect([...STRATEGY_ORDER].sort()).toEqual([...contract].sort());
    expect(new Set(STRATEGY_ORDER).size).toBe(STRATEGY_ORDER.length);
    expect(Object.keys(STRATEGY_META).sort()).toEqual([...contract].sort());
  });

  it("默认策略是 recursive（= 现行为，不是 fixed）", () => {
    expect(DEFAULT_STRATEGY).toBe("recursive");
    expect(STRATEGY_ORDER).toContain(DEFAULT_STRATEGY);
  });

  it("strategyOf 把 undefined / 非法值都落回默认", () => {
    expect(strategyOf(null)).toBe("recursive");
    expect(strategyOf({})).toBe("recursive");
    expect(strategyOf({ chunk_strategy: undefined })).toBe("recursive");
    // 脏数据（后端有 CHECK，理论到不了这里）不该让页面拿到一个假策略
    expect(strategyOf({ chunk_strategy: "nope" as never })).toBe("recursive");
    expect(strategyOf({ chunk_strategy: "late" })).toBe("late");
  });

  it("每一项都有 label / 边界 / 效果（下拉与说明不会空着）", () => {
    for (const k of STRATEGY_ORDER) {
      const m = STRATEGY_META[k];
      expect(m.label.length).toBeGreaterThan(0);
      expect(m.boundary.length).toBeGreaterThan(0);
      expect(m.effect.length).toBeGreaterThan(0);
      expect(m.key).toBe(k);
    }
    expect(strategyOptions().map((o) => o.value)).toEqual([...STRATEGY_ORDER]);
  });

  it("只有 contextual 依赖外部大模型；只有 late 是 Late 编码", () => {
    const needsLlm = STRATEGY_ORDER.filter((k) => STRATEGY_META[k].needsLlm);
    const late = STRATEGY_ORDER.filter((k) => STRATEGY_META[k].lateEncoding);
    expect(needsLlm).toEqual(["contextual"]);
    expect(late).toEqual(["late"]);
  });

  it("isStrategy 收窄的是契约里那六个，别的都不是", () => {
    for (const k of STRATEGY_ORDER) expect(isStrategy(k)).toBe(true);
    for (const bad of ["", "Recursive", "late ", "late_chunking", null, 6, undefined]) {
      expect(isStrategy(bad)).toBe(false);
    }
  });

  describe("换策略时必须说清的三件事", () => {
    it("永远说「不回溯」+「怎么让它生效」", () => {
      for (const k of STRATEGY_ORDER) {
        const lines = strategyChangeNotice(k, { llmActive: true }).join("\n");
        expect(lines).toContain("只对之后重新摄取的文档生效");
        expect(lines).toContain("重新解析");
      }
    });

    it("LLM 没生效时，选 contextual 要**明说会降级**（不能静默降级）", () => {
      const off = strategyChangeNotice("contextual", { llmActive: false }).join("\n");
      expect(off).toContain("降级");
      expect(off).toContain("外部大模型");
      // LLM 已生效时不该吓唬人
      const on = strategyChangeNotice("contextual", { llmActive: true }).join("\n");
      expect(on).not.toContain("降级");
    });

    it("late 提醒更慢；semantic 提醒对 PDF 不生效", () => {
      expect(strategyChangeNotice("late", { llmActive: true }).join("\n")).toContain("更慢");
      expect(strategyChangeNotice("semantic", { llmActive: true }).join("\n")).toContain("PDF");
      // 默认策略没有任何额外代价，只留那两句通用的
      expect(strategyChangeNotice("recursive", { llmActive: true })).toHaveLength(2);
    });
  });

  describe("一次重解析全部", () => {
    it("没有文档时不可点，且说「还没有文档」而不是「0 份」", () => {
      const p = reparseAllPlan(0);
      expect(p.enabled).toBe(false);
      expect(p.text).toContain("还没有文档");
    });

    it("有文档时带上**真实份数**（数字只能来自服务端）", () => {
      const p = reparseAllPlan(7);
      expect(p.enabled).toBe(true);
      expect(p.text).toContain("7 份");
    });

    it("份数非法时不谎报一个数字", () => {
      expect(reparseAllPlan(-1).enabled).toBe(false);
    });
  });
});
