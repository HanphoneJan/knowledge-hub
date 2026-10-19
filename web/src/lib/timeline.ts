/**
 * 时间线与照片墙的**纯逻辑**（D20）。
 *
 * 与 `lib/progress.ts` / `lib/trash.ts` 同一思路：把会静默出错的地方从组件里抽出来。
 *
 * ## 这里最容易出错的四处
 *
 * 1. **月份加减要跨年**：`2025-12` 的下一个月是 `2026-01`。
 *    用 `new Date(month + "-01")` 加月份在 JS 里会溢出（12 月 → 1 月但年份不变），
 *    于是"看下个月"永远停在 12 月。
 * 2. **日期比较必须按字符串**：时间线的日期是 `YYYY-MM-DD`，
 *    `new Date("2025-02-30")` 会滚到 3 月（JS 的坑），字符串比较则稳定。
 * 3. **"不适用"与"没有内容"要能区分**：非 chat/photo 库后端返回 400，
 *    前端要显示"这个视图对这类库没意义"，而不是空白页或"没有记录"。
 * 4. **没有日期的照片要仍列得出来**（排在最后）：过滤掉等于让用户以为照片丢了。
 */

/** 契约 `source_kind` 里能用时间线视图的两个。 */
export const VIEW_SOURCE_KINDS: Array<"chat" | "photo"> = ["chat", "photo"];

/** 这个库能不能看时间线（其余库后端返回 400）。 */
export function supportsTimeline(sourceKind: string | null | undefined): boolean {
  return VIEW_SOURCE_KINDS.includes((sourceKind ?? "") as "chat" | "photo");
}

/**
 * 月份加���（跨年正确）。
 *
 * ⚠️ 不要用 `new Date()`：JS 的 Date 对 `2025-13` 会滚成 `2026-01`，
 * 对 `2025-00` 会滚成 `2024-12`，也就是"加减都错但看起来像对的"。
 */
export function shiftMonth(month: string, delta: number): string {
  const m = /^\d{4}-(0[1-9]|1[0-2])$/.test(month);
  if (!m) return month;
  const year = Number(month.slice(0, 4));
  const mon = Number(month.slice(5, 7));
  // 用 0..11 的偏移再一次性规整，避免逐月进位
  const idx = mon - 1 + delta;
  const y = year + Math.floor(idx / 12);
  const mm = ((idx % 12) + 12) % 12;
  return `${String(y).padStart(4, "0")}-${String(mm + 1).padStart(2, "0")}`;
}

/** 当月天数（闰年也要对：闰年 2 月是 29 天）。 */
export function daysInMonth(month: string): number {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) return 30;
  const y = Number(month.slice(0, 4));
  const m = Number(month.slice(5, 7));
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

/** 该月的日期序列（`YYYY-MM-DD`），用于画日历格子。 */
export function monthDates(month: string): string[] {
  const n = daysInMonth(month);
  const out: string[] = [];
  for (let d = 1; d <= n; d += 1) {
    out.push(`${month}-${String(d).padStart(2, "0")}`);
  }
  return out;
}

/**
 * 月历需要**多少个前导空格**（周一起始，所以 0~6）。
 *
 * ⚠️ 不补空格的话，月初那几天会从第一列开始排 —— 于是「星期几」那一行
 * 与格子对不上（7 月 1 日恰好是周一，所以第一版只在别的月份才暴露）。
 * 用 `Date.UTC` 而不是本地时间：时区会让 `getDay()` 在极端经度上偏一天。
 */
export function leadingBlanks(month: string): number {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) return 0;
  const y = Number(month.slice(0, 4));
  const m = Number(month.slice(5, 7));
  const dow = new Date(Date.UTC(y, m - 1, 1)).getUTCDay(); // 0=周日
  return (dow + 6) % 7;          // 周一 = 0
}

/** `YYYY-MM-DD` 是否合法（含真实存在性：2 月 30 日要判否）。 */
export function isDate(s: string | null | undefined): boolean {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1) return false;
  return d <= daysInMonth(`${y}-${String(m).padStart(2, "0")}`);
}

/** 该月的"今天"（用于高亮；不在本月则给 null）。 */
export function todayOf(month: string, now = new Date()): string | null {
  const y = String(now.getFullYear()).padStart(4, "0");
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const cur = `${y}-${m}`;
  return cur === month
    ? `${cur}-${String(now.getDate()).padStart(2, "0")}`
    : null;
}

/** 该月的下一个月 / 上一个月标签（给翻页按钮）。 */
export function monthLabel(month: string): string {
  const y = Number(month.slice(0, 4));
  const m = Number(month.slice(5, 7));
  return `${y} 年 ${m} 月`;
}

/** 时间线的"这个月有多少事"。 */
export function monthSummary(total: number, activeDays: number): string {
  if (total === 0) return "这个月没有记录";
  if (activeDays === 0) return `${total} 条记录`;
  return `${total} 条记录 · ${activeDays} 天有内容`;
}

/**
 * 说话人统计 → "张三 100 · 李四 100"（按条数降序，只留前 N 个）。
 *
 * ⚠️ 不能只取第一个：两个人的对话只显示一个人会让人以为数据缺了。
 * ⚠️ 也不该全列：5 个以上就变成噪声了。
 */
/** 中文排序器（拼音序）。**显式指定 locale** —— 见 `speakerLine` 里那条注释。 */
const ZH_COLLATOR = new Intl.Collator("zh-Hans-CN");

/** 逐码位降序比较：给 ISO 日期/月份这类**与语言无关**的键用（不碰 locale）。 */
function cmpDesc(a: string, b: string): number {
  return a < b ? 1 : a > b ? -1 : 0;
}

export function speakerLine(
  speakers: Record<string, number>,
  top = 3,
): string {
  const rows = Object.entries(speakers ?? {})
    .filter(([, n]) => n > 0)
    // ⚠️ 同条数时的次序必须**与运行环境无关**：`localeCompare(x, y)` 不传 locale 时
    //   用的是"当前环境的默认 locale" —— 本机 zh_CN 按拼音（甲 < 乙），
    //   而 CI 是 `LANG=C`（按码位，乙 U+4E59 < 甲 U+7532）⇒ 同一份数据两种顺序。
    //   ⇒ 显式钉 `zh-Hans-CN`：界面对中文用户是拼音序，且在任何机器上一致。
    .sort((a, b) => b[1] - a[1] || ZH_COLLATOR.compare(a[0], b[0]))
    .slice(0, top);
  if (rows.length === 0) return "";
  return rows.map(([k, v]) => `${k} ${v}`).join(" · ");
}

/** 月视图的 `days`（契约里是**子集**）→ 按日期排序的日历标记。 */
export function dayMarks(
  days: Array<{ date: string; total: number; photo_count: number }>,
): Record<string, { total: number; photos: number }> {
  const out: Record<string, { total: number; photos: number }> = {};
  for (const d of days ?? []) {
    if (!isDate(d.date)) continue;
    out[d.date] = { total: d.total ?? 0, photos: d.photo_count ?? 0 };
  }
  return out;
}

/** 一天的"强度"分级（0~3），用来给日历格子染色。 */
export function intensity(v: { total: number; photos: number } | undefined): 0 | 1 | 2 | 3 {
  if (!v || (v.total <= 0 && v.photos <= 0)) return 0;
  if (v.photos > 0) return 3;
  if (v.total >= 200) return 3;
  if (v.total >= 50) return 2;
  return 1;
}

/**
 * 照片墙：按月份分组（新的在前），无日期的单独一档放最后。
 *
 * 🔑 **无日期的不许过滤掉**（`taken_at` 抽不到 EXIF 也抽不到文件名日期时会是 null，
 * 见后端 `ingest/timeline.py` 的"不猜"）。过滤掉等于让用户以为照片丢了。
 */
export function groupPhotosByMonth(
  photos: Array<{ doc_id: number; file_name: string; date: string | null }>,
): Array<{ month: string | null; photos: typeof photos }> {
  const withDate = new Map<string, typeof photos>();
  const undated: typeof photos = [];
  for (const p of photos ?? []) {
    const m = p.date && isDate(p.date) ? p.date.slice(0, 7) : null;
    if (m == null) {
      undated.push(p);
      continue;
    }
    const bucket = withDate.get(m) ?? [];
    bucket.push(p);
    withDate.set(m, bucket);
  }
  // ⚠️ 显式标注类型：`month` 是 `string | null`（无日期那档），
  // 让 TS 从 `.map()` 反推会推成 `string` → 末尾 push `null` 报类型错，
  // 而那个报错完全指不到"无日期那档"。
  const out: Array<{ month: string | null; photos: typeof photos }> = Array.from(
    withDate.entries(),
  )
    .sort((a, b) => (a[0] < b[0] ? 1 : a[0] > b[0] ? -1 : 0))  // ISO 键：码位比较即可
    .map(([month, rows]) => ({
      month,
      photos: [...rows].sort((a, b) => cmpDesc(a.date ?? "", b.date ?? "")),
    }));
  if (undated.length) out.push({ month: null, photos: undated });
  return out;
}

/** 无日期那档的标题（不能叫"未知日期"太空泛，也不能假装它们是某天）。 */
export function undatedLabel(count: number): string {
  return `未标注日期（${count} 张）· 这些照片不参与时间线`;
}

/**
 * 纪念日的展示文案。
 *
 * ⚠️ 契约原话是"纪念日 = **有照片的日子**"，所以标题要体现"那天有照片"，
 * 而不是只报消息数 —— 那会让人以为"消息最多的那天"就是纪念日。
 */
export function anniversaryLabel(a: {
  date: string;
  count: number;
  chat_total: number;
}): string {
  const bits = [`${a.date} · ${a.count} 张照片`];
  if (a.chat_total > 0) bits.push(`${a.chat_total} 条消息`);
  return bits.join(" · ");
}

/**
 * 里程碑的判据说明（写出来给用户看）。
 *
 * ⚠️ 后端用的是"按消息量降序"，**不设阈值**（可解释的规则）。
 * 所以这里如实说"按消息量排序"，不要写成"最有意义的一天"——
 * 那是把机器的排序说成的人的判断。
 */
export function milestoneNote(total: number): string {
  return total > 0 ? `当天 ${total} 条消息` : "当天没有消息记录";
}

/** 视图不可用时的提示（区分"不适用"与"没有记录"）。 */
export function notApplicableHint(sourceKind: string | null | undefined): string {
  return (
    `时间线视图只对聊天库（source_kind=chat）与照片库（source_kind=photo）有意义，` +
    `当前这个库是 ${sourceKind ?? "未知"}。` +
    `如果需要，请新建一个对应类型的库（POST /kb 时指定 source_kind）。`
  );
}

/** 空列表的原因分类：接口错误 vs 确实没数据。 */
export function emptyHint(err: { status: number } | null, hasFilters: boolean): string {
  if (err) {
    return err.status === 400
      ? notApplicableHint(null)
      : `加载失败（HTTP ${err.status}）`;
  }
  return hasFilters ? "这个范围内没有记录" : "还没有任何记录";
}