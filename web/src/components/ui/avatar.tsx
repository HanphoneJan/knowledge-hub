/**
 * Avatar —— 首字母头像（RAGFlow 成员表 / Open WebUI 助手头像都是这个形状）。
 *
 * ⚠️ 不引 `@radix-ui/react-avatar`（没装）：它解决的是"图片加载失败时兜底"，
 * 而我们的头像**本来就没有图片源**（只有姓名），所以首字母就是终态。
 */
import { cn } from "../../lib/cn";

const PALETTE = [
  "bg-[#5c96c8]/15 text-[#2f6f9f]",
  "bg-[#5ab77e]/15 text-[#2f7b52]",
  "bg-[#8866d3]/15 text-[#6b46c1]",
  "bg-[#d98f3f]/15 text-[#a3601c]",
  "bg-[#c8577f]/15 text-[#a13a5e]",
];

/** 取一个稳定的首字符（中文取首字，英文取首字母，都没有就 `?`）。 */
export function initialOf(name: string | null | undefined): string {
  const s = (name ?? "").trim();
  if (!s) return "?";
  const ch = Array.from(s)[0];
  return /[a-z]/i.test(ch) ? ch.toUpperCase() : ch;
}

function hashIndex(seed: string, mod: number): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 100000;
  return h % mod;
}

export function Avatar({
  name,
  size = "default",
  className,
}: {
  name: string | null | undefined;
  size?: "sm" | "default" | "lg";
  className?: string;
}) {
  const seed = (name ?? "").trim() || "?";
  const cls =
    size === "sm" ? "size-6 text-2xs" : size === "lg" ? "size-9 text-sm" : "size-8 text-xs";
  return (
    <span
      aria-hidden="true"
      title={seed}
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center rounded-full font-medium",
        cls,
        PALETTE[hashIndex(seed, PALETTE.length)],
        className,
      )}
    >
      {initialOf(seed)}
    </span>
  );
}
