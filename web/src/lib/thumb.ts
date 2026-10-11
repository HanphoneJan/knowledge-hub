/**
 * 缩略图卡片的展示决策（纯函数）。
 *
 * ## 为什么要单独抽出来
 *
 * D36 的价值是"点开前先看到那页长什么样"，而缩略图有两个坑：
 *
 * 1. **不能用 `<img src>`**（CONVENTIONS §6.8）：那个端点要 Bearer，
 *    `<img>` 带不了请求头 → 静默 401 → 一块空白。
 *    必须 `fetch` → blob → `objectURL`（`requestBlob` 已经干这件事）。
 * 2. **`thumb_url` 可能是 null**：聊天 chunk / docx / 图片文档没有页
 *    （`types.thumb_url_for` 会返回 null 而不是编一个会 404 的 URL）。
 *    那就不显示缩略图区 —— **不能给一个占位块**，那等于说"这里应该有图"。
 *
 * 另有一条与全站一致：**图片 alt 用文件名**，而不是"缩略图"三个字 ——
 * 图片加载失败时 alt 是用户唯一能看到的信息。
 */

/** 引用类型里哪些值得配缩略图。 */
export function showThumb(kind: string, thumbUrl: string | null | undefined): boolean {
  if (!thumbUrl) return false;
  // `page`（扫描页）与 `image`（照片/视觉腿）是最需要"先看一眼"的两种
  return kind === "page" || kind === "image" || kind === "table" || kind === "text";
}

/**
 * 缩略图的 alt 文案。
 *
 * ⚠️ 带页码：用户看到的是"第 3 页"，这正是点它的理由。
 * 加载失败时 alt 是唯一的信息，所以不能写"缩略图"这种废话。
 */
export function thumbAlt(fileName: string | null | undefined, page: number | null): string {
  const name = (fileName ?? "").trim() || "文档";
  return page && page > 0 ? `${name} 第 ${page} 页` : name;
}

/**
 * 从 `thumb_url` 里取页码（用于 alt 与"看第几页"）。
 *
 * ⚠️ URL 的形状是 `/api/v1/documents/{id}/thumbs/{page}`，但**不要**假定它一定
 * 是这个形状 —— 解析不出来就返回 null，alt 少个页码而已，
 * 而抛异常会让整张卡片挂掉。
 */
export function pageOfThumbUrl(url: string | null | undefined): number | null {
  if (!url) return null;
  const m = url.match(/\/thumbs\/(\d+)(?:[?#]|$)/);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isInteger(n) && n > 0 ? n : null;
}

/** 缩略图区的固定高度（CSS 也要一致，两处各写一个数就会错位）。 */
export const THUMB_HEIGHT = 96;