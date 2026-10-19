/**
 * 缩略图决策的单测。fixture 是真实 URL（`/api/v1/documents/1568/thumbs/2`，
 * 来自 `eval/api_1c_check.py` 的同一条代码路径）。
 */

import { describe, expect, it } from "vitest";

import { pageOfThumbUrl, showThumb, thumbAlt, THUMB_HEIGHT } from "./thumb";

describe("showThumb", () => {
  it("没有 thumb_url → 不显示（不能给占位块）", () => {
    expect(showThumb("text", null)).toBe(false);
    expect(showThumb("page", undefined)).toBe(false);
    expect(showThumb("text", "")).toBe(false);
  });

  it("有 URL 就显示（四种引用类型都要：表格 chunk 也以 kind=text 出现）", () => {
    const u = "/api/v1/documents/345/thumbs/2";
    for (const k of ["text", "page", "image", "table"]) {
      expect(showThumb(k, u)).toBe(true);
    }
  });
});

describe("thumbAlt", () => {
  it("带页码（用户看到的是「第 3 页」，这正是点它的理由）", () => {
    expect(thumbAlt("登记表.pdf", 3)).toBe("登记表.pdf 第 3 页");
  });

  it("无页码 → 只给文件名", () => {
    expect(thumbAlt("登记表.pdf", null)).toBe("登记表.pdf");
    expect(thumbAlt("登记表.pdf", 0)).toBe("登记表.pdf");
  });

  it("文件名缺失/空白 → 不能出现空 alt（那会让读屏软件跳过）", () => {
    expect(thumbAlt(null, 1)).toBe("文档 第 1 页");
    expect(thumbAlt("   ", null)).toBe("文档");
  });
});

describe("pageOfThumbUrl", () => {
  it("正常形状", () => {
    expect(pageOfThumbUrl("/api/v1/documents/1568/thumbs/2")).toBe(2);
    expect(pageOfThumbUrl("/api/v1/documents/1/thumbs/17?w=400")).toBe(17);
  });

  it("形状不认识 → null（alt 少个页码而已，别抛异常让整张卡片挂掉）", () => {
    expect(pageOfThumbUrl("/api/v1/documents/1568/pages/2")).toBeNull();
    expect(pageOfThumbUrl("/api/v1/thumbs/abc")).toBeNull();
    expect(pageOfThumbUrl("/api/v1/documents/1/thumbs/0")).toBeNull();
    expect(pageOfThumbUrl(null)).toBeNull();
    expect(pageOfThumbUrl("")).toBeNull();
  });

  it("不能匹配到路径中间的其它数字", () => {
    // /thumbs/ 后面必须紧跟数字，不能是 /documents/345/thumbs 里的 345
    expect(pageOfThumbUrl("/api/v1/documents/345/thumbs")).toBeNull();
  });
});

describe("THUMB_HEIGHT", () => {
  it("是正数且不太高（列表页一次要显示几十张）", () => {
    expect(THUMB_HEIGHT).toBeGreaterThan(0);
    expect(THUMB_HEIGHT).toBeLessThanOrEqual(160);
  });
});