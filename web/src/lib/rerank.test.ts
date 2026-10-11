import { describe, expect, it } from "vitest";
import {
  RERANK_DISABLE_PAYLOAD,
  RERANK_HINT,
  rerankEnablePayload,
  rerankState,
  rerankText,
} from "./rerank";

describe("rerankState（D90：null/空串都是关）", () => {
  it("null → 关", () => {
    expect(rerankState({ rerank_model: null })).toEqual({ on: false, model: null });
  });

  it("undefined（老后端没返回这个字段）→ 关，不许当成开", () => {
    // ⚠️ 后端加字段之前，Kb 里根本没这个属性。
    // 如果这里默认成 on，等于"没配置"被解释成"已开启"。
    expect(rerankState({})).toEqual({ on: false, model: null });
  });

  it("空串/纯空白 → 关", () => {
    expect(rerankState({ rerank_model: "" }).on).toBe(false);
    expect(rerankState({ rerank_model: "   " }).on).toBe(false);
  });

  it("模型名 → 开，并去掉首尾空白", () => {
    expect(rerankState({ rerank_model: " BAAI/bge-reranker-v2-m3 " })).toEqual({
      on: true,
      model: "BAAI/bge-reranker-v2-m3",
    });
  });

  it("整个 kb 为 null（还没加载）→ 关", () => {
    expect(rerankState(null).on).toBe(false);
  });
});

describe("rerankText", () => {
  it("关着时要说清是关着（不能只显示空白）", () => {
    expect(rerankText({ on: false, model: null })).toContain("未开启");
  });

  it("开着时带模型名", () => {
    expect(rerankText({ on: true, model: "m" })).toContain("m");
  });
});

describe("payload", () => {
  it("开启用默认模型名，不是空串", () => {
    // 空串在后端 = 关闭 → 用空串"开启"会静默失败
    expect(rerankEnablePayload(" BAAI/x ")).toBe("BAAI/x");
    expect(rerankEnablePayload("BAAI/x")).not.toBe(RERANK_DISABLE_PAYLOAD);
  });

  it("关闭用空串", () => {
    expect(RERANK_DISABLE_PAYLOAD).toBe("");
  });
});

describe("文案纪律", () => {
  it("提醒里必须说『会改变排序』", () => {
    // 精排不是"更准一点"，是结果会变 —— 用户找不到旧习惯的那条会以为坏了
    expect(RERANK_HINT).toContain("重新排序");
    expect(RERANK_HINT).toContain("5 秒");
  });
});
