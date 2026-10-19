/**
 * 设置页的**纯逻辑**（API Key 管理 · D64）。
 *
 * 与 `lib/trash.ts` 同一思路：把"会静默造成损失的地方"从组件里抽出来。
 *
 * ## 三处最容易出错的
 *
 * 1. 🔴 **明文 key 只出现一次**（D64）：页面必须当场显示 + 提供复制，
 *    而且**不能**用弹窗一闪而过 —— 用户要去翻剪贴板，弹窗一关就没了。
 * 2. 🔑 **吊销是不可撤销的**（吊销后那把 key 立刻失效）：确认文案必须说清，
 *    而且要说"谁在用"（label）—— 用户往往有几把 key，不记得哪把是谁的。
 * 3. ⚠️ **不能把明文存进 localStorage**：那是 XSS 一把就能拿走的持久化凭据。
 */

import type { ApiKeyRow } from "../types/api";

/** key 前缀（`sk-pkh_xxxxxxxx`）的中段。契约里只给前 12 位。 */


/** 逐码位降序比较：**不碰 locale** —— 排序结果必须与运行环境无关（CI 是 `LANG=C`）。 */
function cmpDesc(a: string, b: string): number {
  return a < b ? 1 : a > b ? -1 : 0;
}

export function keyPrefix(k: { key_prefix: string }): string {
  return k.key_prefix || "（无前缀）";
}

/**
 * 一把 key 的"还没用过 / 使用中"。
 *
 * ⚠️ **没有"已吊销"这个状态**：吊销是把 ApiKey 行**删掉**
 *   （`auth.py::revoke_key`），所以已吊销的 key 不会出现在列表里。
 * 我第一版脑补了 `revoked_at` 字段并写了三态 —— 于是页面永远走不到那一态，
 * 而"吊销后仍留在列表里、状态是已吊销"那条 e2e 断言是**恒假的**。
 */
export function keyState(k: { last_used_at?: string | null }): {
  state: "active" | "unused";
  label: string;
} {
  if (!k.last_used_at) {
    // ⚠️ "从未使用"要说清是"还没用过"而不是"不能用" ——
    //   用户会以为这把 key 是坏的。
    return { state: "unused", label: "还没用过" };
  }
  return { state: "active", label: "使用中" };
}

/** 吊销确认文案（不可撤销 + 是哪一把）。 */
export function revokeConfirmText(k: ApiKeyRow): string {
  const name = k.name ? `「${k.name}」` : `前缀 ${keyPrefix(k)}`;
  return [
    `吊销 API Key ${name}？`,
    "",
    "吊销后用它发起的请求立刻全部失败（401），而且**不能恢复** —���",
    "只能用登录重新铸一把新的。",
    "",
    k.last_used_at
      ? `它最后一次被使用：${new Date(k.last_used_at).toLocaleString()}。`
      : "⚠️ 它**从未被使用过** —— 确认这不是你刚创建、还没来得及配置到别处的 key。",
  ].join("\n");
}

/** 新建 key 的一次性展示文案（D64：明文只此一次）。 */
export function oneTimeNotice(): string {
  return [
    "🔑 **这把 key 的明文只出现这一次**。",
    "",
    "关掉之后本系统**再也查不到它**（数据库里只存前缀 + 哈希，契约 §1）。",
    "所以现在请：① 复制到密码管理器或 ② 填进你的 MCP / A2A 客户端配置。",
    "丢了只能重新铸一把 —— 旧的那把也会一起吊销掉（否则两把并存更危险）。",
  ].join("\n");
}

/** name 的占位（不要让用户想一个名字才能建 key；`name` 是**必填**的）。 */
export function defaultKeyLabel(): string {
  const d = new Date();
  return `我的电脑 ${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

/** 列表排序：使用中的在前（按最近使用），没用过的在后。 */
export function sortKeys(keys: ApiKeyRow[]): ApiKeyRow[] {
  const rank = (k: ApiKeyRow) => (k.last_used_at ? 0 : 1);
  return [...keys].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      cmpDesc(a.last_used_at ?? "", b.last_used_at ?? "") ||
      b.id - a.id,
  );
}

/**
 * 是不是"当前这把"（用前缀比对）。
 *
 * 🔑 只能比对前缀 —— 明文只在创建时出现一次，之后前端**没有**它。
 * 所以这个判断天然有误差（同前缀的两把极不可能，但可能）；
 * 文案里要说清是"按前缀判断"，不要写成"确定是当前这把"。
 */
export function isCurrentKey(
  k: { key_prefix: string },
  tokenPrefix: string | null,
): boolean {
  if (!tokenPrefix) return false;
  return k.key_prefix === tokenPrefix;
}