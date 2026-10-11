/**
 * 回收站逻辑的单测。
 *
 * fixture 用真实数据：`DELETE /trash?dry_run=true` 的一次返回
 * （来自 `eval/api_1c_check.py` 的同一份代码路径）：
 *
 * ```json
 * {"docs":2,"chunks":0,"blobs_dropped":0,"bytes_freed":318367,
 *  "doc_ids":[1313,1314],"ids_truncated":false,"qdrant_failed":[],"dry_run":true}
 * ```
 */

import { describe, expect, it } from "vitest";

import {
  canPurge,
  confirmText,
  daysLeft,
  expiryLabel,
  formatBytes,
  freedHint,
  overwriteNotice,
  trashDeletedAt,
  type PurgeResp,
} from "./trash";

const DAY = 86_400_000;
const T0 = new Date("2026-10-03T10:00:00+08:00").getTime();

function purge(over: Partial<PurgeResp> = {}): PurgeResp {
  return {
    docs: 2,
    chunks: 0,
    blobs_dropped: 0,
    bytes_freed: 318367,
    doc_ids: [1313, 1314],
    ids_truncated: false,
    qdrant_failed: [],
    dry_run: true,
    ...over,
  };
}

describe("formatBytes", () => {
  it("三档单位，超过 100 就取整（12.3 GB 这种精度没意义）", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(318367)).toBe("311 KB"); // 310.9 → ≥100 取整
    expect(formatBytes(1024 * 42)).toBe("42.0 KB"); // <100 保留一位
    expect(formatBytes(5 * 1024 ** 2)).toBe("5.0 MB");
    expect(formatBytes(500 * 1024 ** 2)).toBe("500 MB");
    expect(formatBytes(3 * 1024 ** 3)).toBe("3.0 GB");
  });

  it("空值不显示 NaN（那会让用户以为坏了）", () => {
    expect(formatBytes(null)).toBe("-");
    expect(formatBytes(undefined)).toBe("-");
    expect(formatBytes(Number.NaN)).toBe("-");
  });
});

describe("到期时间（D26 保留 30 天）", () => {
  it("刚删 → 30 天后到期", () => {
    expect(daysLeft(new Date(T0).toISOString(), T0)).toBe(30);
    expect(expiryLabel(new Date(T0).toISOString(), T0)).toBe("30 天后到期");
  });

  it("过了 10 天 → 20 天", () => {
    const t = new Date(T0 - 10 * DAY).toISOString();
    expect(daysLeft(t, T0)).toBe(20);
    expect(expiryLabel(t, T0)).toBe("20 天后到期");
  });

  it("明天到期单独说（「1 天后到期」读起来像还有 24 小时，其实可能只剩 1 分钟）", () => {
    const t = new Date(T0 - 29 * DAY).toISOString();
    expect(daysLeft(t, T0)).toBe(1);
    expect(expiryLabel(t, T0)).toBe("明天到期");
  });

  it("已过期要说「随时会被自动清理」，不能显示 0 天", () => {
    const t = new Date(T0 - 40 * DAY).toISOString();
    expect(daysLeft(t, T0)).toBe(-10);
    expect(expiryLabel(t, T0)).toContain("已过期");
  });

  it("缺失/坏时间戳退回 30 天（不能因为脏数据显示「已过期」）", () => {
    expect(daysLeft(null, T0)).toBe(30);
    expect(daysLeft("不是时间", T0)).toBe(30);
    expect(expiryLabel(null, T0)).toBe("30 天后到期");
  });
});

describe("trashDeletedAt", () => {
  it("有值 → 本地时间", () => {
    expect(trashDeletedAt({ deleted_at: new Date(T0).toISOString() })).toContain("删除于");
  });

  it("空值要说「未知」，不能显示 1970（那看起来像 1970 年就删的）", () => {
    expect(trashDeletedAt({ deleted_at: null })).toBe("删除时间未知");
    expect(trashDeletedAt({})).toBe("删除时间未知");
    expect(trashDeletedAt({ deleted_at: "坏值" })).toBe("删除时间未知");
  });
});

describe("confirmText（不可撤销操作的三要素）", () => {
  it("必须同时有：份数 / 体积 / 不可撤销", () => {
    const t = confirmText(purge(), "「项目文档」");
    expect(t).toContain("2 份文档");
    expect(t).toContain("311 KB");
    expect(t).toContain("不可撤销");
    expect(t).toContain("「项目文档」");
  });

  it("回收站为空时不喊「确定清空」", () => {
    expect(confirmText(purge({ docs: 0, bytes_freed: 0 }), "「项目文档」")).toContain(
      "已经是空的",
    );
  });

  it("🔑 预览与真删的文案**完全一致** —— 否则用户在确认框里看不到警告", () => {
    // 确认框永远出现在真删之前，数据来自 dry_run 预览。
    // 第一版按 dry_run 标志抑制「不可撤销」→ 用户永远看不到这句警告。
    const preview = confirmText(purge({ dry_run: true }), "「项目文档」");
    const real = confirmText(purge({ dry_run: false }), "「项目文档」");
    expect(preview).toContain("不可撤销");
    expect(preview).toBe(real);
  });

  it("Qdrant 没清干净要如实说（别让用户以为索引也干净了）", () => {
    const t = confirmText(purge({ qdrant_failed: [1, 2, 3] }), "「项目文档」");
    expect(t).toContain("3 份");
    expect(t).toContain("对账");
  });
});

describe("freedHint（释放 0 B 的解释）", () => {
  it("真释放了 → 不加解释（多余的话是噪音）", () => {
    expect(freedHint({ docs: 3, bytes_freed: 1024 })).toBe("");
  });

  it("删了但 0 字节 → 解释是去重，不是「删除没生效」", () => {
    const h = freedHint({ docs: 1, bytes_freed: 0 });
    expect(h).toContain("去重");
    expect(h).toContain("没有释放磁盘");
  });

  it("什么都没删 → 什么都不说", () => {
    expect(freedHint({ docs: 0, bytes_freed: 0 })).toBe("");
  });
});

describe("canPurge", () => {
  it("空回收站禁用按钮（点出来一个 0 份的确认框更让人怀疑坏了）", () => {
    expect(canPurge(0)).toBe(false);
    expect(canPurge(1)).toBe(true);
  });
});

describe("overwriteNotice（D28 覆盖式更新）", () => {
  it("回收站多了新名字 → 提示", () => {
    expect(overwriteNotice(["a.pdf"], ["a.pdf", "b.pdf"])).toBe(
      "同名文件已有旧版本，被替换进回收站：b.pdf",
    );
  });

  it("没变 → 不提示（每次上传都弹一句会变成噪音）", () => {
    expect(overwriteNotice(["a.pdf"], ["a.pdf"])).toBeNull();
    expect(overwriteNotice([], [])).toBeNull();
    // 少了（被恢复或清空）也不算覆盖
    expect(overwriteNotice(["a.pdf", "b.pdf"], ["a.pdf"])).toBeNull();
  });

  it("多个同时替换要都列出来", () => {
    expect(overwriteNotice([], ["x.pdf", "y.pdf"])).toContain("x.pdf、y.pdf");
  });
});