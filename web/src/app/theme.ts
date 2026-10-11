/**
 * 主题偏好（D188/D189：`.dark` 类驱动）。
 *
 * ## 三态：存**意图**，不存生效值
 *
 * | 项 | 值 |
 * |---|---|
 * | 存储键 | `localStorage["pkh.theme"]` ∈ `light` / `dark` / `system` |
 * | 生效开关 | `<html class="dark">` —— **CSS 只读这一个类** |
 * | 写入方 | 本文件 `applyTheme()` + `index.html` 首绘前那段内联脚本 |
 *
 * 🔑 **首屏那一帧必须靠 `index.html` 里的内联脚本**（React mount 之后才加类就晚了，
 * 症状是刷新时闪一下另一个主题）。那句 `resolveDark` 的判定因此有**两份拷贝**，
 * 是"同一个判定写两处"的经典漂移点 ⇒ `theme.test.ts` 有一条守卫扫 `index.html`。
 *
 * ⚠️ 内联脚本必须是**普通脚本**：`type="module"` 是 deferred 的，不保证首绘前跑完。
 *
 * ⚠️ 主题**只有一个入口**（设置 → 外观）。别处再放一个 = 设计错误（用户点过名）。
 *
 * 纯逻辑（`resolveDark` / `readPref`）不碰 DOM，可单测；DOM 那一薄层只在
 * `applyTheme` / `watchSystem` 里。
 */

export const THEME_KEY = "pkh.theme";

export const THEME_PREFS = ["system", "light", "dark"] as const;

export type ThemePref = (typeof THEME_PREFS)[number];

export interface ThemeStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** 读存储的**意图**；非法/缺失/隐私模式一律回落到 `system`。 */
export function readPref(store?: ThemeStore): ThemePref {
  try {
    const s = store ?? localStorage;
    const raw = s.getItem(THEME_KEY);
    return (THEME_PREFS as readonly string[]).includes(raw ?? "")
      ? (raw as ThemePref)
      : "system";
  } catch {
    return "system";
  }
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

/**
 * 意图 → 生效值。**这就是 `index.html` 那段内联脚本的同一个判定**
 * （那边写成了一句等价的表达式，见文件头）。
 */
export function resolveDark(pref: ThemePref, sysDark = systemPrefersDark()): boolean {
  if (pref === "dark") return true;
  if (pref === "light") return false;
  return sysDark;
}

/** 把生效值写到 `<html>`（唯一写入点）。 */
export function applyClass(dark: boolean): void {
  document.documentElement.classList.toggle("dark", dark);
}

/** 设置页点一下走这里：落存储 + 立即生效。 */
export function applyTheme(pref: ThemePref, store?: ThemeStore): void {
  try {
    (store ?? localStorage).setItem(THEME_KEY, pref);
  } catch {
    /* 存不下就本次生效，不阻断 */
  }
  applyClass(resolveDark(pref));
}

/**
 * 跟随系统时监听系统切换。
 *
 * 🔑 只在 `pref === "system"` 时才需要订阅；用户在设置里显式选了浅/暗，
 * 系统怎么变都不该动他的界面。返回取消订阅函数。
 */
export function watchSystem(onChange: () => void): () => void {
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => onChange();
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  } catch {
    return () => {};
  }
}

/** 分段控件的展示文案（设置 → 外观）。 */
export const THEME_LABEL: Record<ThemePref, string> = {
  system: "跟随系统",
  light: "浅色",
  dark: "深色",
};
