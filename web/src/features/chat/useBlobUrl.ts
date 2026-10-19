/**
 * `fetch` → blob → `URL.createObjectURL` 的小 hook。
 *
 * 🔑 **为什么不能用 `<img src="/api/v1/...">`**（CONVENTIONS §6.8）：
 * 页图 / 缩略图 / 原图三个端点都要 `Authorization: Bearer`，
 * 而 `<img>` 带不了自定义请求头 ⇒ 静默 401 ⇒ 页面上一块空白，
 * 只有控制台里看得到。所以一律走 `requestBlob` + objectURL。
 *
 * ⚠️ **必须 revoke**：objectURL 不释放就是内存泄漏（照片墙翻几页就明显了）。
 * ⚠️ 迟到响应要丢弃（`cancelled`），否则快速翻页会把上一页的图贴上来。
 */
import * as React from "react";

export interface BlobUrlState {
  url: string | null;
  loading: boolean;
  error: string | null;
  /**
   * HTTP 状态码（如果能拿到）。调用方靠它区分**能力缺失**与**真失败**：
   * `415` = 这类文档本来就没有页图/缩略图（`CONVENTIONS §0.8`：真相在 DB 的
   * mime/扩展名里，端点只是如实拒绝）⇒ 静默不显示，而不是报一条红字错误。
   */
  errorStatus: number | null;
}

export function useBlobUrl(
  load: (() => Promise<Blob>) | null,
  deps: React.DependencyList,
): BlobUrlState {
  const [state, setState] = React.useState<BlobUrlState>({
    url: null,
    loading: !!load,
    error: null,
    errorStatus: null,
  });
  const loadRef = React.useRef(load);
  loadRef.current = load;

  React.useEffect(() => {
    if (!loadRef.current) {
      setState({ url: null, loading: false, error: null, errorStatus: null });
      return;
    }
    let cancelled = false;
    let created: string | null = null;
    setState((s) => ({ url: s.url, loading: true, error: null, errorStatus: null }));
    loadRef
      .current()
      .then((blob) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setState({ url: created, loading: false, error: null, errorStatus: null });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        const status =
          typeof (e as { status?: unknown })?.status === "number"
            ? ((e as { status: number }).status)
            : null;
        setState({
          url: null,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
          errorStatus: status,
        });
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
