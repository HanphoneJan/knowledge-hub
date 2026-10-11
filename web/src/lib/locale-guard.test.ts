/**
 * 守卫：`src/lib/**` 里**不许出现不传 locale 的 `localeCompare`**。
 *
 * ## 为什么要有这条（真事，不是假想）
 *
 * `speakerLine({乙: 5, 甲: 5})` 断言同条数时按名字排序。原来那行是
 * `a[0].localeCompare(b[0])` —— **不传 locale 时用的是"当前环境的默认 locale"**：
 *   - 本机 zh_CN：按拼音 ⇒ `甲`(jiǎ) < `乙`(yǐ) ⇒ `"甲 5 · 乙 5"` ✅
 *   - CI `ubuntu-latest`（`LANG=C`）：按码位 ⇒ `乙`(U+4E59) < `甲`(U+7532) ⇒ `"乙 5 · 甲 5"` ❌
 * 于是「本机全绿、CI 红」——而失败信息只说排序不对，完全指不到 locale 上。
 *
 * 修法**不是**改断言迁就环境（那只是把 bug 藏起来），而是让排序**与环境无关**：
 * 要么显式钉 collator（`new Intl.Collator("zh-Hans-CN")`，中文界面要拼音序就用它），
 * 要么用码位比较（ISO 日期、ASCII 标识符这类与语言无关的键）。
 *
 * ## 判据为什么是"扫源码"而不是"跑一遍多语言"
 *
 * vitest 一个进程只有一种 locale，测试里改不了 —— 靠断言只能覆盖**当前**环境，
 * 而这类 bug 的本质恰恰是"换台机器就变"。所以这里扫文本：
 * 它不证明行为对，只保证**没人再写出依赖默认 locale 的比较**（接线守卫，文本层）。
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const LIB = dirname(fileURLToPath(import.meta.url));

/** `localeCompare(x)` 且**没有第二个参数**的调用（`localeCompare(x, "zh")` 是允许的）。 */
const UNPINNED = /\.localeCompare\(\s*[^,()]*(?:\([^)]*\))?[^,()]*\)/;

describe("locale 无关性", () => {
  it("src/lib 里没有不传 locale 的 localeCompare", () => {
    const offenders = readdirSync(LIB)
      .filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts"))
      .filter((f) => UNPINNED.test(readFileSync(join(LIB, f), "utf8")))
      .sort();

    expect(
      offenders,
      "这些文件里用了不传 locale 的 `localeCompare` —— 排序会随运行环境的 locale 变" +
        "（本机 zh_CN 与 CI 的 LANG=C 结果不同）。请改成 `new Intl.Collator(\"zh-Hans-CN\")`" +
        "（中文界面）或码位比较（ISO 日期/ASCII 标识符）。",
    ).toEqual([]);
  });

  it("守卫本身认得出坏写法、也放得过好写法（否则它是假绿）", () => {
    // 🔑 §5.114/§5.151c：守卫必须**被合成输入验过** ——
    //    正则写松了（比如匹配 `localeCompare(` 就算命中）会让好写法也报错，
    //    写紧了（只认 `.localeCompare(x)` 这一种形态）会漏掉带括号/空格的写法。
    expect(UNPINNED.test('a[0].localeCompare(b[0])')).toBe(true);
    expect(UNPINNED.test('x.localeCompare(y)')).toBe(true);
    expect(UNPINNED.test('(b.date ?? "").localeCompare(a.date ?? "")')).toBe(true);
    expect(UNPINNED.test("a.type.localeCompare( b.type )")).toBe(true);
    expect(UNPINNED.test('a.localeCompare(b, "zh-Hans-CN")')).toBe(false);
    expect(UNPINNED.test("ZH_COLLATOR.compare(a[0], b[0])")).toBe(false);
    expect(UNPINNED.test("a < b ? 1 : a > b ? -1 : 0")).toBe(false);
    expect(UNPINNED.test("// 曾经用 localeCompare(x) 比较过")).toBe(false);
  });
});
