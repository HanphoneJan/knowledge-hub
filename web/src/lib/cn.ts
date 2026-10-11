/**
 * 组合类名的小工具（shadcn 约定）。
 *
 * - `clsx`：处理条件/数组/对象形式的类名
 * - `tailwind-merge`：当两个 utility 作用于同一属性时，**后者胜**
 *   （例如基础 `rounded-md` 被调用处的 `rounded-full` 覆盖，而不是两者并存）
 *
 * ⚠️ **必须把我们的自定义字号告诉 twMerge**：`text-13` / `text-2xs` 不是
 * Tailwind 内置尺寸，twMerge 会把它当成**颜色**，于是
 * `cn("text-on-accent", "text-13")` 会丢掉 `text-on-accent` ——
 * 症状是主按钮变成"深蓝底 + 黑字"（可读性差），而这是肉眼才发现的 bug。
 *
 * ⚠️ 纯字符串处理，不碰 DOM ⇒ 不违反 `lib/` 只有纯逻辑的约定。
 */
import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      // 与 `styles.css` 的 `--text-*` 一一对应（内置 xs/sm/base/lg/xl 不用列）。
      text: ["2xs", "13"],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
