import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthProvider, useAuth } from "./app/auth";
import { ScopeProvider } from "./app/scope";
import { ConfirmProvider } from "./components/ui/confirm";
import { ToastProvider } from "./components/ui/toast";
import { AppShell } from "./shell/AppShell";
import { ChatPage } from "./features/chat/ChatPage";
import { LoginPage } from "./features/login/LoginPage";

// 知识库 / 设置这两块页面最大（表格、策略卡、分区表单），按路由拆包；
// 对话与登录是首屏，保持同步加载 —— 拆了反而多一次往返。
const KbListPage = React.lazy(() =>
  import("./features/knowledge/KbListPage").then((m) => ({ default: m.KbListPage })),
);
const KbDetailPage = React.lazy(() =>
  import("./features/knowledge/KbDetailPage").then((m) => ({ default: m.KbDetailPage })),
);
const SettingsPage = React.lazy(() =>
  import("./features/settings/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 局域网自部署 + 单机后端：不必为了"省流量"把数据捂在缓存里。
      staleTime: 10 * 1000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

/** 没登录 → 登录页（**不自动重登**，D64 每次 login 都会新铸一把 key）。 */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token } = useAuth();
  const loc = useLocation();
  if (!token) return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  return <>{children}</>;
}

/** 已登录还去 `/login` → 直接送回对话页。 */
function LoginRoute() {
  const { token } = useAuth();
  if (token) return <Navigate to="/chat" replace />;
  return <LoginPage />;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ToastProvider>
          <ConfirmProvider>
            <BrowserRouter>
              <Routes>
                <Route path="/login" element={<LoginRoute />} />
                <Route
                  element={
                    <RequireAuth>
                      <ScopeProvider>
                        <AppShell />
                      </ScopeProvider>
                    </RequireAuth>
                  }
                >
                  <Route index element={<Navigate to="/chat" replace />} />
                  <Route path="/chat" element={<ChatPage />} />
                  <Route path="/chat/:sessionId" element={<ChatPage />} />
                  <Route path="/kb" element={<KbListPage />} />
                  <Route path="/kb/:kbId" element={<KbDetailPage />} />
                  <Route path="/settings" element={<SettingsPage />} />
                  {/* D162：旧路由 /search /debug /timeline /photos /sessions 已删除（不重定向） */}
                  <Route path="*" element={<Navigate to="/chat" replace />} />
                </Route>
              </Routes>
            </BrowserRouter>
          </ConfirmProvider>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
