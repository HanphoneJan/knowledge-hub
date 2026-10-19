/**
 * 登录态（`lib/auth.ts` 的 DOM 侧薄层）。
 *
 * ## 三条纪律（都来自 `lib/auth.ts` 的注释，别绕）
 *
 * 1. 🔑 **不要每次进页面都 login**：`POST /auth/login` 每次**新铸一把 key**，
 *    一进来就 login ⇒ 每刷新一次就往 `api_keys` 插一行。所以只有"没有 key"
 *    或"key 失效（401）"时才走登录页。
 * 2. ⚠️ **不自动重登**：401 只是清登录态（`lib/api.ts::toApiError` 已经做了），
 *    刷新页面重新登录由用户决定。
 * 3. ⚠️ API Key 存 `localStorage`（D3 的模型下只能这样）—— **key 等同密码**。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { api } from "../lib/api";
import { clearAuth, getUser, getToken, saveAuth, type StoredUser } from "../lib/auth";
import { Keys } from "./queryKeys";

interface AuthValue {
  /** 已登录 = 有 token。 */
  token: string | null;
  user: StoredUser | null;
  isSuperadmin: boolean;
  /** 登录成功（调用方负责跳转）。 */
  signIn: (token: string, user: StoredUser) => void;
  signOut: () => void;
}

const Ctx = React.createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = React.useState<string | null>(() => getToken());
  const [user, setUser] = React.useState<StoredUser | null>(() => getUser());
  const qc = useQueryClient();

  // 校验 / 刷新当前用户（有 token 才发）。401 会让 lib/api.ts 清 auth，
  // 这里据此把 React 状态也清掉 —— 两处必须同步，否则界面还在、请求全 401。
  const me = useQuery({
    queryKey: Keys.me(),
    queryFn: () => api.me(),
    enabled: !!token,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  React.useEffect(() => {
    if (me.data) {
      const u: StoredUser = {
        id: me.data.id,
        email: me.data.email,
        display_name: me.data.display_name,
        role: me.data.role ?? null,
        nickname: me.data.nickname ?? null,
      };
      setUser(u);
      if (getToken()) saveAuth(getToken() as string, u);
    }
  }, [me.data]);

  React.useEffect(() => {
    if (me.isError && !getToken()) {
      setToken(null);
      setUser(null);
    }
  }, [me.isError, me.errorUpdatedAt]);

  const signIn = React.useCallback(
    (nextToken: string, nextUser: StoredUser) => {
      saveAuth(nextToken, nextUser);
      setToken(nextToken);
      setUser(nextUser);
      void qc.invalidateQueries();
    },
    [qc],
  );

  const signOut = React.useCallback(() => {
    clearAuth();
    setToken(null);
    setUser(null);
    qc.clear();
  }, [qc]);

  const value = React.useMemo<AuthValue>(
    () => ({
      token,
      user,
      isSuperadmin: user?.role === "superadmin",
      signIn,
      signOut,
    }),
    [token, user, signIn, signOut],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = React.useContext(Ctx);
  if (!v) throw new Error("useAuth 必须在 <AuthProvider> 内使用");
  return v;
}
