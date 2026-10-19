import { describe, expect, it } from "vitest";

import {
  decodeScope,
  encodeScope,
  isAll,
  isSelected,
  loadStoredScope,
  saveStoredScope,
  scopeLabel,
  scopeToKbIds,
  toggleScope,
} from "./kbScope";
import type { Kb } from "../types/api";

function kb(id: number, name = `库${id}`): Kb {
  return {
    id,
    workspace_id: 1,
    name,
    description: null,
    visibility: "private",
    source_kind: "file",
    embed_model: "bge-small-zh-v1.5",
    chunk_size: 500,
    chunk_overlap: 50,
    generate_image_desc: false,
    pii_mask: false,
    rerank_model: null,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

/** 内存版 Storage，避开 jsdom（单测跑在 node 环境）。 */
function memStore(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
  };
}

describe("decodeScope", () => {
  it("缺省 → undefined（继承全局，别当成 all）", () => {
    expect(decodeScope(null)).toBeUndefined();
    expect(decodeScope(undefined)).toBeUndefined();
  });

  it("'all' / 空串 → null（全部有权限的库）", () => {
    expect(decodeScope("all")).toBeNull();
    expect(decodeScope("")).toBeNull();
  });

  it("逗号列表 → 去重、去非法、保序", () => {
    expect(decodeScope("3,1,3, 2")).toEqual([3, 1, 2]);
    expect(decodeScope("0,-1,abc")).toBeNull(); // 全非法 → 全部，不是空集
  });

  it("全是非法 id 时退回 null 而不是空集（空集会看起来像「库是空的」）", () => {
    expect(decodeScope("x,y")).toBeNull();
  });
});

describe("encodeScope", () => {
  it("round-trip", () => {
    expect(encodeScope(null)).toBe("all");
    expect(encodeScope([2, 4])).toBe("2,4");
    expect(decodeScope(encodeScope([2, 4]))).toEqual([2, 4]);
    expect(decodeScope(encodeScope(null))).toBeNull();
  });
});

describe("loadStoredScope / saveStoredScope", () => {
  it("新键优先", () => {
    expect(loadStoredScope(memStore({ "pkh.kbs": "5,6" }))).toEqual([5, 6]);
    expect(loadStoredScope(memStore({ "pkh.kbs": "all" }))).toBeNull();
  });

  it("没有新键时回退旧的单选键（pkh.kb）用于迁移", () => {
    expect(loadStoredScope(memStore({ "pkh.kb": "7" }))).toEqual([7]);
  });

  it("两个键都没有 → 全部", () => {
    expect(loadStoredScope(memStore())).toBeNull();
  });

  it("save 写进新键", () => {
    const s = memStore();
    saveStoredScope([1, 2], s);
    expect(s.getItem("pkh.kbs")).toBe("1,2");
  });
});

describe("toggleScope", () => {
  it("'全部'下取消一个 → 展开成其余全部", () => {
    expect(toggleScope(null, 2, [1, 2, 3])).toEqual([1, 3]);
  });

  it("已选中再点 → 去掉", () => {
    expect(toggleScope([1, 2, 3], 2, [1, 2, 3])).toEqual([1, 3]);
  });

  it("未选中再点 → 加上", () => {
    expect(toggleScope([1], 3, [1, 2, 3])).toEqual([1, 3]);
  });

  it("取消到最后一个 → 回到 null（全部），不是空集", () => {
    expect(toggleScope([1], 1, [1, 2, 3])).toBeNull();
  });
});

describe("isSelected / isAll", () => {
  it("null 视为全选", () => {
    expect(isSelected(null, 9)).toBe(true);
    expect(isAll(null, [1, 2])).toBe(true);
  });
  it("显式列表全含 → isAll", () => {
    expect(isAll([1, 2], [1, 2])).toBe(true);
    expect(isAll([1], [1, 2])).toBe(false);
  });
});

describe("scopeLabel", () => {
  it("全部时带库数量", () => {
    expect(scopeLabel(null, [kb(1), kb(2)])).toBe("全部库（2）");
  });
  it("单个时显示库名", () => {
    expect(scopeLabel([2], [kb(1), kb(2, "我的文档")])).toBe("我的文档");
  });
  it("多个时显示数量", () => {
    expect(scopeLabel([1, 2], [kb(1), kb(2), kb(3)])).toBe("2 个库");
  });
});

describe("scopeToKbIds", () => {
  it("全部 → null（让服务端按 readable_kb_ids 推导，D8）", () => {
    expect(scopeToKbIds(null)).toBeNull();
  });
  it("显式 → 去重列表", () => {
    expect(scopeToKbIds([1, 1, 2])).toEqual([1, 2]);
  });
});
