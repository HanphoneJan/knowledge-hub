/**
 * 登录 / 注册（D138 · D174）。
 *
 * ## 四条纪律
 *
 * 1. 🔑 **不要每次进页面都 login**（`lib/auth.ts`）：`POST /auth/login` 每次新铸一把 key，
 *    反复登录会把 `api_keys` 表塞满 ⇒ 这里只在**用户主动提交**时调；
 * 2. ⚠️ **失败文案原样显示**：登录失败一律 `400 账号或密码不正确`（不区分用户不存在），
 *    前端**不许**替后端写得"更友好"（那会把探测信号还给攻击者）；
 * 3. ⚠️ 429（限流）带 `Retry-After` —— 如实告诉用户还要等几秒；
 * 4. ⚠️ 注册在 `ALLOW_REGISTER=false` 时对**非本机**返回 403 `REGISTRATION_CLOSED`
 *    （服务端先于限流判定）⇒ 原样显示。
 */
import { useMutation } from "@tanstack/react-query";
import { LogIn, UserPlus } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Alert } from "../../components/ui/alert";
import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { Segmented } from "../../components/ui/segmented";
import { api, ApiError, errText } from "../../lib/api";
import { useAuth } from "../../app/auth";

/** 429 的 `Retry-After` 是秒数；没有头就给一句通用的。 */
function rateHint(e: unknown): string | null {
  if (e instanceof ApiError && e.status === 429) {
    const raw = (e.detail as { retry_after?: number } | undefined)?.retry_after;
    return raw ? `请求太频繁，请等 ${raw} 秒再试。` : "请求太频繁，请稍后再试。";
  }
  return null;
}

export function LoginPage() {
  const navigate = useNavigate();
  const { signIn } = useAuth();
  const [mode, setMode] = React.useState<"login" | "register">("login");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [nickname, setNickname] = React.useState("");
  const [err, setErr] = React.useState<string | null>(null);

  const loginM = useMutation({
    mutationFn: () => api.login(email.trim(), password),
    onSuccess: (r) => {
      signIn(r.api_key, {
        id: r.user.id,
        email: r.user.email,
        display_name: r.user.display_name,
        role: r.user.role ?? null,
        nickname: r.user.nickname ?? null,
      });
      navigate("/chat", { replace: true });
    },
    onError: (e) => setErr(rateHint(e) ?? errText(e)),
  });

  const registerM = useMutation({
    mutationFn: () => api.register(email.trim(), password, displayName.trim(), nickname.trim() || undefined),
    onSuccess: (r) => {
      signIn(r.api_key, {
        id: r.user.id,
        email: r.user.email,
        display_name: r.user.display_name,
        role: r.user.role ?? null,
        nickname: r.user.nickname ?? null,
      });
      navigate("/chat", { replace: true });
    },
    onError: (e) => setErr(rateHint(e) ?? errText(e)),
  });

  const busy = loginM.isPending || registerM.isPending;
  const canSubmit =
    email.trim().length > 0 &&
    password.length > 0 &&
    (mode === "login" || displayName.trim().length > 0) &&
    !busy;

  const submit = React.useCallback(
    (e?: React.FormEvent) => {
      e?.preventDefault();
      setErr(null);
      if (!canSubmit) return;
      if (mode === "login") loginM.mutate();
      else registerM.mutate();
    },
    [canSubmit, mode, loginM, registerM],
  );

  return (
    <div className="flex min-h-full items-center justify-center bg-bg-canvas px-4 py-10">
      <div className="flex w-full max-w-[26rem] flex-col gap-5">
        <div className="flex flex-col items-center gap-2 text-center">
          <div className="flex size-12 items-center justify-center rounded-2xl bg-text-primary text-bg-base">
            <svg aria-hidden="true" viewBox="0 0 24 24" className="size-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
              <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 1 4 16z" />
              <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5h5A1.5 1.5 0 0 0 20 16z" />
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-text-primary">Knowledge Hub</h1>
          <p className="text-sm text-text-secondary">局域网自部署的多租户知识库</p>
        </div>

        <Card className="flex flex-col gap-4 p-5">
          <Segmented
            className="self-center"
            ariaLabel="登录或注册"
            value={mode}
            onChange={(v) => {
              setMode(v);
              setErr(null);
            }}
            options={[
              { value: "login", label: "登录", icon: LogIn },
              { value: "register", label: "注册", icon: UserPlus },
            ]}
          />

          <form className="flex flex-col gap-3" onSubmit={submit}>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="login-email">邮箱或昵称</Label>
              <Input
                id="login-email"
                value={email}
                autoComplete="username"
                onChange={(e) => setEmail(e.target.value)}
                placeholder={mode === "login" ? "you@example.com 或昵称" : "you@example.com"}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="login-pw">密码</Label>
              <Input
                id="login-pw"
                type="password"
                value={password}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>

            {mode === "register" && (
              <>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="reg-name">显示名</Label>
                  <Input
                    id="reg-name"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="别人看到的名字"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="reg-nick">昵称（可选，唯一，之后可用它登录）</Label>
                  <Input
                    id="reg-nick"
                    value={nickname}
                    onChange={(e) => setNickname(e.target.value)}
                    placeholder="例如 zhangsan"
                  />
                </div>
              </>
            )}

            {err && <Alert tone="danger">{err}</Alert>}

            <Button type="submit" block disabled={!canSubmit} loading={busy}>
              {mode === "login" ? "登录" : "注册并登录"}
            </Button>
          </form>

          <div className="text-2xs text-text-secondary">
            {mode === "login" ? (
              <>
                登录会铸一把新的 API Key 并存进 localStorage（D3 的模型下只能这样）——
                <strong className="font-semibold text-text-primary">key 等同密码</strong>
                ，别在共享机器上登录。
              </>
            ) : (
              <>
                注册是否开放由部署方决定（ALLOW_REGISTER）。关掉之后要新账号得用后端的
                create_user.py —— 那不是前端能替代的事。
              </>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
