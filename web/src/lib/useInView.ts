/**
 * 元素是否进入过视口（一次性，进入后断开）。
 *
 * 用途：照片墙 / 时间线缩略图。它们走 `fetch` + blob（端点要 Bearer，不能用
 * `<img src>`），所以浏览器自己的 `loading="lazy"` **管不到**请求 —— 24 张
 * 原图会在挂载时一起发出去。这里用 IntersectionObserver 把"要不要开始取"
 * 变成一个显式条件。
 *
 * - 不支持 IntersectionObserver 的环境直接视为可见（功能不退化）。
 * - 已经可见后不再观察：滚动来回不会重复取图。
 */
import * as React from "react";

export function useInView<T extends Element>(rootMargin = "200px") {
  const ref = React.useRef<T | null>(null);
  const [inView, setInView] = React.useState(false);

  React.useEffect(() => {
    if (inView) return;
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          io.disconnect();
        }
      },
      { rootMargin },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [inView, rootMargin]);

  return { ref, inView };
}
