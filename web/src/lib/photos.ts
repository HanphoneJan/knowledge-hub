/**
 * 照片墙的**页码**规则（`PhotosPage` 唯一用得到的一处，抽出来是为了能单测）。
 *
 * ## 为什么值得抽
 *
 * 规则本身只有一行，但里面藏着一个**看起来多余、其实不能删**的分支
 * （`firstLoadDone`）—— 下一个人看到"反正 `total=0` 时 `pages=1`，
 * 夹一下也没差"就会把它简化掉，然后深链 `?page=3` 在一个真有 5 页的库里
 * 会被误夹成第 1 页（用户以为看到的是第 3 页）。那种 bug 只在
 * "URL 里的页码 > 1 **且**库不止一页"时出现 ⇒ 本地手点翻页器永远碰不到。
 */

/** 一页放几张（与 `PhotosPage.PAGE_SIZE` 一致；改一处就要改两处 ⇒ 有测试盯着）。 */
export const PHOTO_PAGE_SIZE = 24;

/**
 * URL 里的页码文本 → 一个**至少为 1** 的整数。
 *
 * ⚠️ 非法值（空、`abc`、`0`、`-3`）一律落到 1，而不是 NaN 或 0：
 * NaN 会变成 `page=NaN` 发给服务端（400），0/负数会让翻页器显示成 `第 0 / 3 页`。
 */
export function parsePhotoPage(raw: string | null | undefined): number {
  const n = Number.parseInt(raw ?? "1", 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

/** 总页数（至少 1 页 —— 0 张照片也显示"第 1 / 1 页"而不是"第 1 / 0 页"）。 */
export function photoPages(total: number, pageSize = PHOTO_PAGE_SIZE): number {
  return Math.max(1, Math.ceil((total || 0) / pageSize));
}

/**
 * **实际要显示/请求**的页码。
 *
 * @param urlPage      URL 里请求的页码（`parsePhotoPage` 的结果）
 * @param total        服务端返回的总数
 * @param firstLoadDone 首次加载**完成**过没有
 * @returns 要用的页码
 *
 * 🔑 两条规则：
 *
 * 1. **夹回**：页码超过实际页数时取最后一页。不夹的后果不是"空页"而是**死路**
 *    —— 只有 1 页的库深链 `?page=2` 会请求第 2 页拿到 0 行，
 *    而翻页器按 `pages > 1` 渲染 ⇒ 不出现 ⇒ 用户**没有回到第 1 页的路**
 *    （空态文案却写着"翻页看看"）。**提示与出路必须一致**。
 * 2. **首次加载不夹**：那时 `total` 还是 0（⇒ `pages=1`），一夹就把合法的
 *    深链 `?page=3` 变成第 1 页。`firstLoadDone` 就是用来区分
 *    "还不知道总数"与"确实只有一页"的。
 */
export function shownPhotoPage(
  urlPage: number,
  total: number,
  firstLoadDone: boolean,
  pageSize = PHOTO_PAGE_SIZE,
): number {
  if (!firstLoadDone) return Math.max(1, urlPage);
  return Math.min(Math.max(1, urlPage), photoPages(total, pageSize));
}
