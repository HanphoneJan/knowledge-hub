import { describe, expect, it } from "vitest";

import {
  docRows,
  maskOffConfirmText,
  maskOnConfirmText,
  maskState,
  reportHeadline,
  reportWarnings,
  typeLabel,
  typeRows,
} from "./pii";
import type { PiiReport } from "../types/api";

function rep(over: Partial<PiiReport> = {}): PiiReport {
  return {
    kb_id: 1,
    masking: "off",
    source_total: 0,
    source_by_type: {},
    indexed_total: 0,
    indexed_by_type: {},
    doc_count: 0,
    docs_with_pii: 0,
    docs_not_scanned: 0,
    docs_no_text: 0,
    docs: [],
    docs_truncated: false,
    scan_chars: 0,
    scan_truncated: false,
    ...over,
  };
}

describe("typeLabel", () => {
  it("认识五个已知类型", () => {
    expect(typeLabel("id_card")).toBe("身份证");
    expect(typeLabel("phone")).toBe("手机号");
    expect(typeLabel("email")).toBe("邮箱");
    expect(typeLabel("student_id")).toBe("学号");
    expect(typeLabel("bank_card")).toBe("银行卡");
  });

  // ⚠️ 后端加了新类型时前端不能显示 undefined
  it("🔑 未知类型原样显示（不能是 undefined）", () => {
    expect(typeLabel("passport")).toBe("passport");
  });
});

describe("typeRows", () => {
  it("按数量降序", () => {
    const rows = typeRows({ phone: 2, email: 5, id_card: 5 });
    expect(rows[0].n).toBe(5);
    expect(rows[1].n).toBe(5);
    expect(rows[2].n).toBe(2);
  });

  it("同数量按类型名排（顺序稳定，渲染不跳）", () => {
    const a = typeRows({ phone: 1, id_card: 1 });
    const b = typeRows({ id_card: 1, phone: 1 });
    expect(a.map((r) => r.type)).toEqual(b.map((r) => r.type));
  });

  it("空对象 → 空数组", () => {
    expect(typeRows({})).toEqual([]);
  });

  // ⚠️ 后端可能给 undefined（老数据），不能崩
  it("undefined 也不崩", () => {
    expect(typeRows(undefined as unknown as Record<string, number>)).toEqual([]);
  });
});

describe("maskState", () => {
  it("默认关闭", () => {
    expect(maskState(rep()).masking).toBe("off");
  });

  it("kb 上的开关优先于报告里的字段", () => {
    // ⚠️ 两者不一致时以 PATCH 刚返回的为准 —— 报告是旧的
    expect(maskState(rep({ masking: "off" }), true).masking).toBe("on");
    expect(maskState(rep({ masking: "on" }), false).masking).toBe("off");
  });

  it("开着且索引里还剩 → 需要回填", () => {
    const s = maskState(rep({ masking: "on", indexed_total: 3 }), true);
    expect(s.backfill_needed).toBe(true);
  });

  it("开着且索引干净 → 不需要回填", () => {
    expect(maskState(rep({ masking: "on", indexed_total: 0 }), true).backfill_needed)
      .toBe(false);
  });

  // 🔑 关着的时候索引里有 PII 是**正常**的，不是"待修"
  it("关着时索引里有 PII 不算待回填", () => {
    const s = maskState(rep({ masking: "off", indexed_total: 9 }));
    expect(s.backfill_needed).toBe(false);
  });

  it("报告还没加载 → 不崩且不算待回填", () => {
    expect(maskState(null)).toEqual({ masking: "off", backfill_needed: false });
  });
});

describe("maskOnConfirmText", () => {
  it("三句都在：代价 / 不回溯 / 原件仍在", () => {
    const txt = maskOnConfirmText(rep({ doc_count: 12, docs_with_pii: 4 }));
    expect(txt).toContain("搜不到自己的这些号码");
    expect(txt).toContain("不回溯");
    expect(txt).toContain("原文仍在");
    expect(txt).toContain("12");
    expect(txt).toContain("4");
  });

  it("🔑 报告没加载时也要能读（不能出现 undefined）", () => {
    const txt = maskOnConfirmText(null);
    expect(txt).not.toContain("undefined");
    expect(txt).toContain("搜不到自己的这些号码");
  });
});

describe("maskOffConfirmText", () => {
  it("说清「已打码的不会自动还原」", () => {
    const txt = maskOffConfirmText();
    expect(txt).toContain("不会自动还原");
    expect(txt).toContain("重新摄取");
  });
});

describe("reportHeadline", () => {
  it("🔑 两套数字都在（原文 / 索引）", () => {
    const t = reportHeadline(rep({ source_total: 7, indexed_total: 0, masking: "on" }));
    expect(t).toContain("原文检出 7 处");
    expect(t).toContain("索引里还剩 0 处");
  });

  // 🔴 曾经会写成"这个库没有敏感信息"，而真相是"原文里有、只是索引里没有"
  it("索引为 0 时**不**说成「没有敏感信息」", () => {
    const t = reportHeadline(rep({ source_total: 7, indexed_total: 0, masking: "on" }));
    expect(t).not.toContain("没有敏感信息");
  });

  it("未开掩码时点明「原文直接进索引」", () => {
    const t = reportHeadline(rep({ source_total: 3, indexed_total: 3 }));
    expect(t).toContain("未开");
    expect(t).toContain("原文直接进索引");
  });

  it("开着但没回填完 → 明说", () => {
    const t = reportHeadline(rep({ source_total: 5, indexed_total: 2, masking: "on" }));
    expect(t).toContain("还没重新摄取");
  });
});

describe("reportWarnings", () => {
  it("⚠️ 没检测过的文档必须提示（不是「干净」）", () => {
    const w = reportWarnings(rep({ doc_count: 3, docs_not_scanned: 1 }));
    expect(w.join()).toContain("没检测过");
  });

  it("🔴 没有文本层的文档（照片/扫描件）单独提示", () => {
    const w = reportWarnings(rep({ doc_count: 3, docs_no_text: 2 }));
    expect(w.join()).toContain("没有文本层");
    expect(w.join()).toContain("不代表图片里没有敏感信息");
  });

  it("🔑 明细截断时说明总数是全量", () => {
    const w = reportWarnings(rep({ source_total: 99, docs_truncated: true, docs: [] }));
    expect(w.join()).toContain("全量");
  });

  it("扫描截断时说是下限", () => {
    const w = reportWarnings(rep({ scan_truncated: true, scan_chars: 4_000_000 }));
    expect(w.join()).toContain("下限");
  });

  it("全绿时给一句确认（0 文档不该说「没检测过」）", () => {
    const w = reportWarnings(rep({ doc_count: 2, docs_not_scanned: 0 }));
    expect(w.join()).not.toContain("没检测过");
  });
});

describe("docRows", () => {
  it("逐份列出类型", () => {
    const rows = docRows(
      rep({
        docs: [
          { doc_id: 9, file_name: "a.pdf", status: "ready", total: 3, by_type: { phone: 1, id_card: 2 } },
        ],
      }),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].kinds).toContain("身份证 2");
    expect(rows[0].kinds).toContain("手机号 1");
  });

  it("空明细 → 空行", () => {
    expect(docRows(rep())).toEqual([]);
  });
});