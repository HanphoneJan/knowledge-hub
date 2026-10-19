/**
 * 会话管理的**纯逻辑**（契约 §7）。
 *
 * 会话是"多轮追问"的载体，所以管理它有一个特殊要求：
 * 🔴 **删会话是软删（可恢复）**，所以确认文案不能说"不可撤销"，
 * 而且列表里必须能看出"删过" —— 否则用户点了删除、刷新一下又看见它，
 * 会以为删除失败（其实它已经不在列表接口的返回里了）。
 */

import type { SessionRow } from "../types/api";

/** 会话标题的兜底（后端可能给 null）。 */


/** 逐码位降序比较：**不碰 locale** —— 排序结果必须与运行环境无关（CI 是 `LANG=C`）。 */
function cmpDesc(a: string, b: string): number {
  return a < b ? 1 : a > b ? -1 : 0;
}

export function sessionTitle(s: { title: string | null; id: number }): string {
  const t = (s.title ?? "").trim();
  return t.length ? t : `会话 #${s.id}`;
}

/** 🔴 空的标题**不许**保存（存空 = 用户看不到哪个是哪个）。 */
export function validateTitle(title: string): string | null {
  const t = title.trim();
  if (!t) return "标题不能为空";
  if (t.length > 200) return "标题太长了（上限 200 字）";
  return null;
}

/**
 * 删会话的确认文案（**软删**）。
 *
 * ⚠️ 不能写"不可撤销"：契约 §7 的删除是 `deleted_at` 置位，30 天内可恢复
 * （D26 同一套回收站语义）。谎称不可撤销会让用户不敢删；
 * 谎称可撤销又会让用户以为刷新一下就回来了。
 */
export function deleteConfirmText(s: SessionRow): string {
  return [
    `删除会话「${sessionTitle(s)}」？`,
    "",
    "删除后**立刻**就不再参与多轮追问（历史会话里看不到它了）。",
    "",
    "ℹ️ 这是**软删除**：数据库里还留着，30 天内可以恢复",
    "（与文档回收站同一套语义，D26）—— 所以不要为了「整理一下」而删。",
    "",
    "⚠️ 这里**不显示**「里面有 N 条消息」：列表端点没有这个字段",
    "（`SessionOut` 只有 id/kb_id/title/时间），要数得逐个取详情 ——",
    "编一个数字比不显示更糟（§5.57 同一纪律：数字只能来自真实来源）。",
  ].join("\n");
}

/** 列表排序：最近更新的在前（用户刚聊的那个应该在最上面）。 */
export function sortSessions(rows: SessionRow[]): SessionRow[] {
  return [...rows].sort(
    (a, b) =>
      cmpDesc(a.updated_at ?? a.created_at, b.updated_at ?? b.created_at) ||
      b.id - a.id,
  );
}

/**
 * 列表里每行的摘要：**只有时间**。
 *
 * ⚠️ 不显示"条数"：`SessionRow` 没有那个字段（编一个就是撒谎）。
 *   要看内容点「查看」—— 详情端点才带 messages。
 */
export function sessionSummary(s: SessionRow): string {
  const at = s.updated_at || s.created_at;
  const when = at ? new Date(at).toLocaleString() : "时间未知";
  return `最后活动 ${when}`;
}

/** "没有会话"与"没有加载出来"的区别（前者给引导，后者给错误）。 */
export function emptySessionsHint(err: { status: number } | null): string {
  if (err) return `加载失败（HTTP ${err.status}）—— 刷新页面重试`;
  return "这个库还没有会话。在「问答」页问一句就会自动建一个。";
}