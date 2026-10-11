/** 我的资料分区（D175）—— 改显示名/昵称（自助）与邮箱/密码（要当前密码）。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { api, errText } from "../../../lib/api";
import { Keys } from "../../../app/queryKeys";

export function ProfileSection() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: Keys.me(), queryFn: () => api.me() });

  const [displayName, setDisplayName] = React.useState("");
  const [nickname, setNickname] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [note, setNote] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!me.data) return;
    setDisplayName(me.data.display_name ?? "");
    setNickname(me.data.nickname ?? "");
    setEmail(me.data.email ?? "");
  }, [me.data]);

  const saveM = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        display_name: displayName.trim() || null,
        nickname: nickname.trim() || null,
      };
      if (email.trim() && email.trim() !== me.data?.email) {
        body.email = email.trim();
        body.current_password = current;
      }
      if (next) {
        body.new_password = next;
        body.current_password = current;
      }
      return api.updateMe(body);
    },
    onSuccess: () => {
      setErr(null);
      setNote("已保存。改了邮箱或密码之后，下次登录用新的。");
      setCurrent("");
      setNext("");
      void qc.invalidateQueries({ queryKey: Keys.me() });
    },
    onError: (e) => {
      setNote(null);
      setErr(errText(e));
    },
  });

  const emailChanged = !!me.data && email.trim() !== (me.data.email ?? "");
  const needCurrent = emailChanged || !!next;

  return (
    <div className="flex max-w-[640px] flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && <Alert tone="success">{note}</Alert>}

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>我的资料</CardTitle>
          <CardDescription>
            昵称是唯一的，可以用它代替邮箱登录（D174）。改邮箱或密码必须带当前密码。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4 pt-0">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="me-display">显示名</Label>
            <Input id="me-display" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="me-nickname">昵称（唯一，可作登录名）</Label>
            <Input id="me-nickname" value={nickname} onChange={(e) => setNickname(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="me-email">邮箱</Label>
            <Input id="me-email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="me-cur">当前密码{needCurrent ? "" : "（改邮箱/密码时才需要）"}</Label>
              <Input
                id="me-cur"
                type="password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                autoComplete="current-password"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="me-new">新密码（留空 = 不改）</Label>
              <Input
                id="me-new"
                type="password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                autoComplete="new-password"
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              loading={saveM.isPending}
              disabled={needCurrent && !current}
              onClick={() => saveM.mutate()}
            >
              保存
            </Button>
            {needCurrent && !current && (
              <span className="text-2xs text-text-secondary">先填「当前密码」才能改邮箱或密码</span>
            )}
          </div>
        </CardContent>
      </Card>

      {me.data && (
        <div className="text-2xs text-text-secondary">
          账号 id {me.data.id} · 角色 {me.data.role ?? "user"} · 创建于{" "}
          {new Date(me.data.created_at).toLocaleString()}
        </div>
      )}
    </div>
  );
}
