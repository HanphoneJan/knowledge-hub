/**
 * API Key 的存取。
 *
 * ## 为什么存在 localStorage
 *
 * D3 的认证模型是"每用户一把 API Key"，登录时明文返回一次、之后库里只有哈希。
 * 所以前端能做的只有"把这一把存下来"（`sessionStorage` 也行，但换标签页就没了）。
 * 本项目是**局域网自部署**（D8 单机），浏览器里存一把可吊销的 key 可接受。
 *
 * ## ⚠️ 不要每次进页面都 login
 *
 * D64：`login` **新铸一把 key**，库里的旧 key 明文已经拿不回来了。
 * 一进来就 login ⇒ 每刷新一次页面就往 `api_keys` 表插一行 `web-login`。
 * 所以只在**没有 key 或 key 失效（401）**时才 login。
 */

const KEY = "pkh.api_key";
const USER = "pkh.user";

export interface StoredUser {
  id: number;
  email: string;
  display_name: string;
  /** D173：`user` / `superadmin`（登录响应里带；旧存储可能没有）。 */
  role?: string | null;
  nickname?: string | null;
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null; // 隐私模式下 localStorage 会抛
  }
}

export function getUser(): StoredUser | null {
  try {
    const raw = localStorage.getItem(USER);
    return raw ? (JSON.parse(raw) as StoredUser) : null;
  } catch {
    return null;
  }
}

export function saveAuth(token: string, user: StoredUser): void {
  try {
    localStorage.setItem(KEY, token);
    localStorage.setItem(USER, JSON.stringify(user));
  } catch {
    // 存不下就退化成"每次都要重新登录"，不阻断
  }
}

export function clearAuth(): void {
  try {
    localStorage.removeItem(KEY);
    localStorage.removeItem(USER);
  } catch {
    /* ignore */
  }
}