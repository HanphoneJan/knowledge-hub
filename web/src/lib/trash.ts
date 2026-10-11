/**
 * 回收站与"清空"的**纯逻辑**（判定 + 文案）。
 *
 * 与 `lib/progress.ts` 同一个思路：把会静默出错的地方从组件里抽出来。
 * 这里最容易出错的是**不可撤销操作的确认流程** ——
 * 漏一次 dry_run 预览、或者把 `kb_id` 传错，用户就在毫不知情的情况下
 * 丢掉了 30 天的反悔窗口（D26）。
 *
 * ## 为什么"清空回收站"必须先预览
 *
 * `DELETE /trash` 是本项目**唯一不可撤销**的端点（删用户、删 KB 都还有软删兜底）。
 * DELETE 没有请求体，没法"先问一下"，所以后端给了 `dry_run`，
 * 前端必须真的用它 —— 不用的后果是用户凭感觉点确认。
 */

/** 体积显示。KB/MB/GB 三档，保留一位小数。 */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** 回收站条目要显示的"删除于"。空值不给"-"，那会像"刚刚"。 */
export function trashDeletedAt(doc: { deleted_at?: string | null }): string {
  if (!doc.deleted_at) return "删除时间未知";
  const d = new Date(doc.deleted_at);
  if (Number.isNaN(d.getTime())) return "删除时间未知";
  return `删除于 ${d.toLocaleString()}`;
}

/**
 * 还剩几天到期（D26 保留 30 天）。
 *
 * ⚠️ 已过期仍显示负数天数而不是 0 —— 那份数据随时可能被
 * `KIND_CLEANUP` 清掉，用户应该知道"它已经过期了"，而不是"还有 0 天"。
 */
export function daysLeft(deletedAt: string | null | undefined, now = Date.now()): number {
  if (!deletedAt) return 30;
  const d = new Date(deletedAt).getTime();
  if (Number.isNaN(d)) return 30;
  const days = 30 - (now - d) / 86_400_000;
  return Math.round(days);
}

/** 到期文案。过期 / 今天 / N 天后，三种说法不能混。 */
export function expiryLabel(deletedAt: string | null | undefined, now = Date.now()): string {
  const d = daysLeft(deletedAt, now);
  if (d <= 0) return "已过期，随时会被自动清理";
  if (d === 1) return "明天到期";
  return `${d} 天后到期`;
}

/** `DELETE /trash` 的响应（契约 §4.3）。 */
export interface PurgeResp {
  docs: number;
  chunks: number;
  blobs_dropped: number;
  bytes_freed: number;
  doc_ids: number[];
  ids_truncated: boolean;
  qdrant_failed: number[];
  dry_run: boolean;
}

/**
 * 确认框文案。
 *
 * ⚠️ 三条不许省的：
 * 1. **份数** —— 没有数字用户是在盲删
 * 2. **"不可撤销"** —— 它与其它删除不同（D26 的 30 天反悔窗口在这里不成立）
 * 3. **明确说清 scope** —— "清空回收站"清的是当前范围，不是全站
 *
 * ⚠️ `dry_run` **不影响**这段文案。确认框永远出现在真删**之前**，
 * 而它的数据来自 dry_run 预览 —— 所以这里要是对 dry_run 响应少说那句警告，
 * 用户就在整个流程里永远看不到"不可撤销"。
 * （第一版正是按 `dry_run` 抑制警告的，被自己的单测抓出来。）
 */
export function confirmText(p: PurgeResp, scopeLabel: string): string {
  if (p.docs === 0) return `${scopeLabel}的回收站已经是空的`;
  const size = formatBytes(p.bytes_freed);
  const qdrant =
    p.qdrant_failed.length > 0
      ? `\n\n⚠️ 有 ${p.qdrant_failed.length} 份的向量索引没清掉，索引要等对账任务清理。`
      : "";
  return (
    `确定清空${scopeLabel}回收站里的 ${p.docs} 份文档（约 ${size}）？` +
    `${qdrant}\n\n这一步不可撤销。`
  );
}

/**
 * 「释放 0 B」的解释。
 *
 * 数字本身是对的：内容寻址去重（同一份内容只存一份 blob），
 * 引用计数没归零就**没有释放磁盘**。但只显示 0 B 会让人以为删除没生效。
 */
export function freedHint(p: Pick<PurgeResp, "docs" | "bytes_freed">): string {
  if (p.docs === 0) return "";
  if (p.bytes_freed > 0) return "";
  return "（这些文档的内容与其他文档相同，去重后没有释放磁盘空间）";
}

/**
 * 这个清空按钮**该不该可点**。
 *
 * 回收站为空时禁用 —— 否则用户会点出一个 "0 份" 的确认框，
 * 那比禁用更让人怀疑是不是坏了。
 */
export function canPurge(trashCount: number): boolean {
  return trashCount > 0;
}

/**
 * 上传后要不要提示"旧版本进了回收站"（D28 覆盖式更新）。
 *
 * @param before 上传**前**回收站里的文件名集合
 * @param after  上传**后**回收站里的文件名集合
 */
export function overwriteNotice(
  before: readonly string[],
  after: readonly string[],
): string | null {
  const added = after.filter((n) => !before.includes(n));
  if (added.length === 0) return null;
  return `同名文件已有旧版本，被替换进回收站：${added.join("、")}`;
}