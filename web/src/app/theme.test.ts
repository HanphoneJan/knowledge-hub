/**
 * 守卫：`index.html` 首绘前那段内联脚本 ↔ `theme.ts`（D188/D189）。
 *
 * ## 为什么需要它
 *
 * "同一个判定写两处"是本项目最贵的失败模式（CONVENTIONS §0.6）。
 * 主题那个判定**必须**写两处（首绘前只能用内联脚本），于是这里把它钉住：
 * 存储键、写入目标、文档顺序、三态语义，四处对齐。
 *
 * ⚠️ 判据指向**性质**（键/目标/顺序/语义），不比对整段脚本文本 ——
 * 否则改一个空格就红，守卫会被人删掉。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { resolveDark, THEME_KEY, THEME_PREFS } from "./theme";

const HTML = readFileSync(
  fileURLToPath(new URL("../../index.html", import.meta.url)),
  "utf8",
);

/** 只取 `<head>` 里那段主题脚本（页面里还有别的 script 标签）。 */
function themeScript(): string {
  const m = HTML.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("index.html 里找不到普通内联 <script>（主题首绘）");
  return m[1];
}

describe("index.html 的首绘主题脚本", () => {
  it("用的是同一个存储键", () => {
    expect(themeScript()).toContain(`localStorage.getItem("${THEME_KEY}")`);
  });

  it("写入目标是 `<html class=\"dark\">` 而不是别的属性", () => {
    const s = themeScript();
    expect(s).toContain('classList.add("dark")');
    // 旧机制（data-theme + light-dark()）已废弃，不能两套并存
    expect(s).not.toContain("dataset.theme");
    expect(s).not.toContain("data-theme");
  });

  it("认可浅/暗两个字面量，且缺省 = 跟随系统", () => {
    const s = themeScript();
    expect(s).toContain('"dark"');
    expect(s).toContain('"light"');
    expect(s).toContain("prefers-color-scheme: dark");
  });

  it("是普通脚本，并排在 module 入口之前（否则首绘前跑不完）", () => {
    const plain = HTML.indexOf("<script>");
    // ⚠️ 只认真正的 module 入口标签：注释里也写着 `type="module"` 这几个字
    const module = HTML.indexOf('<script type="module"');
    expect(plain).toBeGreaterThan(-1);
    expect(module).toBeGreaterThan(-1);
    expect(plain).toBeLessThan(module);
  });

  it("resolveDark 的三态与内联脚本的表达式等价", () => {
    // 内联脚本：dark → true；light → false；其余（含缺失/非法）→ 跟随系统
    expect(resolveDark("dark", false)).toBe(true);
    expect(resolveDark("light", true)).toBe(false);
    expect(resolveDark("system", true)).toBe(true);
    expect(resolveDark("system", false)).toBe(false);
  });

  it("三态字面量与 THEME_PREFS 一致", () => {
    expect([...THEME_PREFS]).toEqual(["system", "light", "dark"]);
  });
});
