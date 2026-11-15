/**
 * 复制文本 —— **必须带非安全上下文的回退**。
 *
 * 项目是局域网自部署，常见访问方式是 `http://192.168.x.x:8013`。
 * 那里 `navigator.clipboard` 是 undefined（Clipboard API 只在 https / localhost
 * 下可用），而 `navigator.clipboard?.writeText()` 的可选链会让整条链**静默短路**
 * —— 按钮点了没反应，也没有任何报错。用户只会以为"复制坏了"。
 *
 * 回退用 `document.execCommand("copy")`：已废弃，但在 http 下仍是唯一可行路径，
 * 且只在安全上下文缺失时才会走到。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 权限被拒 / 文档失焦：继续走回退 */
    }
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    // 固定在视口外，避免页面跳一下；opacity 0 不影响 select。
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
