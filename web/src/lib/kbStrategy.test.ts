import { describe, expect, it } from "vitest";

import { staleStrategyHint } from "./kbStrategy";

describe("staleStrategyHint（D181）", () => {
  it("0 不显示（干净是常态，不该弹提示）", () => {
    expect(staleStrategyHint(0)).toBeNull();
  });

  it("缺失/非有限值不显示", () => {
    expect(staleStrategyHint(undefined)).toBeNull();
    expect(staleStrategyHint(null)).toBeNull();
    expect(staleStrategyHint(Number.NaN)).toBeNull();
  });

  it(">0 时给出份数与可操作的下一步", () => {
    const s = staleStrategyHint(3);
    expect(s).not.toBeNull();
    expect(s).toContain("3 份");
    expect(s).toContain("重新解析");
  });
});
