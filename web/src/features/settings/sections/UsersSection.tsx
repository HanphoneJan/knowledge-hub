/**
 * 用户管理分区（D173/D175，**仅超管**）—— RAGFlow 的管理台表格观感。
 *
 * 🔑 服务端的防自锁规则（别在前端"优化"掉）：
 * - 不能降级/停用**最后一个**超级管理员；
 * - 不能停用**自己**。
 * ⇒ 所以这里**照常把按钮画出来**，让后端用错误码说话（`errText` 原样显示），
 *   而不是前端自己猜一条规则 —— 猜错了就是"按钮消失了但没人知道为什么"。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, UserCog } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Avatar } from "../../../components/ui/avatar";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { Menu } from "../../../components/ui/menu";
import { Select } from "../../../components/ui/select";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { Switch } from "../../../components/ui/switch";
import { api, errText } from "../../../lib/api";
import { Keys } from "../../../app/queryKeys";
import { useAuth } from "../../../app/auth";
import type { AdminUser, UserRole } from "../../../types/api";

export function UsersSection() {
  const qc = useQueryClient();
  const { user: me } = useAuth();
  const [creating, setCreating] = React.useState(false);
  const [editing, setEditing] = React.useState<AdminUser | null>(null);
  const [pwUser, setPwUser] = React.useState<AdminUser | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [note, setNote] = React.useState<string | null>(null);

  const q = useQuery({ queryKey: Keys.users(), queryFn: () => api.listUsers() });

  const invalidate = React.useCallback(() => {
    void qc.invalidateQueries({ queryKey: Keys.users() });
  }, [qc]);

  const toggleActiveM = useMutation({
    mutationFn: (u: AdminUser) => api.updateUser(u.id, { is_active: !u.is_active }),
    onSuccess: (u) => {
      setErr(null);
      setNote(`${u.display_name} 已${u.is_active ? "启用" : "停用"}。`);
      invalidate();
    },
    onError: (e) => {
      setNote(null);
      setErr(errText(e));
    },
  });

  const roleM = useMutation({
    mutationFn: ({ u, role }: { u: AdminUser; role: UserRole }) => api.updateUser(u.id, { role }),
    onSuccess: (u) => {
      setErr(null);
      setNote(`${u.display_name} 的角色已改为 ${u.role}。`);
      invalidate();
    },
    onError: (e) => {
      setNote(null);
      setErr(errText(e));
    },
  });

  const rows = q.data ?? [];

  return (
    <div className="flex flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && (
        <Alert tone="success" actions={<Button variant="ghost" size="xs" onClick={() => setNote(null)}>知道了</Button>}>
          {note}
        </Alert>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2 p-4 pb-2">
          <div className="flex flex-col gap-1">
            <CardTitle>用户管理</CardTitle>
            <CardDescription>
              超管能越过三档可见性（D173）；防自锁规则由后端判定，报错会原样显示。
            </CardDescription>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" className="size-4" />
            新建用户
          </Button>
        </CardHeader>
        <CardContent className="p-4 pt-0">
          {q.isLoading && <SkeletonRows rows={4} />}
          {q.isError && <Alert tone="danger">{errText(q.error)}</Alert>}
          {rows.length > 0 && (
            <div className="overflow-hidden rounded-2xl border-[0.5px] border-border-button">
              <table className="w-full">
                <thead className="bg-bg-title text-text-secondary">
                  <tr>
                    <th className="px-4 py-2.5 text-start text-2xs font-normal">用户</th>
                    <th className="px-4 py-2.5 text-start text-2xs font-normal">邮箱</th>
                    <th className="px-4 py-2.5 text-start text-2xs font-normal">角色</th>
                    <th className="px-4 py-2.5 text-start text-2xs font-normal">状态</th>
                    <th className="w-16 px-4 py-2.5 text-end text-2xs font-normal">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((u) => (
                    <tr key={u.id} className="border-t-[0.5px] border-border-button hover:bg-bg-card">
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          <Avatar name={u.display_name || u.email} size="sm" />
                          <div className="flex min-w-0 flex-col">
                            <span className="truncate text-13 text-text-primary">{u.display_name}</span>
                            {u.nickname && (
                              <span className="text-2xs text-text-secondary">@{u.nickname}</span>
                            )}
                          </div>
                          {me?.id === u.id && (
                            <Badge variant="secondary" size="sm">
                              你
                            </Badge>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-2xs text-text-secondary">{u.email}</td>
                      <td className="px-4 py-2.5">
                        <Badge variant={u.role === "superadmin" ? "warning" : "secondary"} size="sm">
                          {u.role}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge variant={u.is_active ? "success" : "destructive"} size="sm" dot>
                          {u.is_active ? "启用" : "停用"}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5 text-end">
                        <Menu
                          items={[
                            { key: "edit", label: "改资料 / 昵称", icon: UserCog, onSelect: () => setEditing(u) },
                            { key: "pw", label: "重置密码", icon: KeyRound, onSelect: () => setPwUser(u) },
                            {
                              key: "role",
                              label: u.role === "superadmin" ? "降级为普通用户" : "提升为超管",
                              onSelect: () =>
                                roleM.mutate({ u, role: u.role === "superadmin" ? "user" : "superadmin" }),
                            },
                            {
                              key: "active",
                              label: u.is_active ? "停用账号" : "启用账号",
                              danger: u.is_active,
                              onSelect: () => toggleActiveM.mutate(u),
                            },
                          ]}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <CreateUserDialog
        open={creating}
        onOpenChange={setCreating}
        onError={setErr}
        onDone={(m) => {
          setErr(null);
          setNote(m);
          invalidate();
        }}
      />

      <EditUserDialog
        user={editing}
        onOpenChange={(v) => !v && setEditing(null)}
        onError={setErr}
        onDone={(m) => {
          setErr(null);
          setNote(m);
          invalidate();
          setEditing(null);
        }}
      />

      <ResetPasswordDialog
        user={pwUser}
        onOpenChange={(v) => !v && setPwUser(null)}
        onError={setErr}
        onDone={(m) => {
          setErr(null);
          setNote(m);
          setPwUser(null);
        }}
      />
    </div>
  );
}

function CreateUserDialog({
  open,
  onOpenChange,
  onDone,
  onError,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onDone: (msg: string) => void;
  onError: (m: string) => void;
}) {
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [nickname, setNickname] = React.useState("");
  const [role, setRole] = React.useState<UserRole>("user");

  React.useEffect(() => {
    if (!open) return;
    setEmail("");
    setPassword("");
    setDisplayName("");
    setNickname("");
    setRole("user");
  }, [open]);

  const m = useMutation({
    mutationFn: () =>
      api.createUser({
        email: email.trim(),
        password,
        display_name: displayName.trim() || email.split("@")[0],
        nickname: nickname.trim() || null,
        role,
      }),
    onSuccess: (u) => {
      onOpenChange(false);
      onDone(`已建账号 ${u.email}（${u.role}）。`);
    },
    onError: (e) => onError(errText(e)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建用户</DialogTitle>
          <DialogDescription>
            自助注册关闭后（D138）这是建号的一个入口；也可以在后端用 create_user.py。
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5 sm:col-span-2">
            <Label htmlFor="nu-email">邮箱</Label>
            <Input id="nu-email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nu-name">显示名</Label>
            <Input id="nu-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nu-nick">昵称（唯一，可空）</Label>
            <Input id="nu-nick" value={nickname} onChange={(e) => setNickname(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nu-pw">密码（≥8 位）</Label>
            <Input
              id="nu-pw"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nu-role">角色</Label>
            <Select
              id="nu-role"
              value={role}
              onChange={(v) => setRole(v as UserRole)}
              options={[
                { value: "user", label: "user" },
                { value: "superadmin", label: "superadmin" },
              ]}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            disabled={!email.includes("@") || password.length < 8}
            loading={m.isPending}
            onClick={() => m.mutate()}
          >
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EditUserDialog({
  user,
  onOpenChange,
  onDone,
  onError,
}: {
  user: AdminUser | null;
  onOpenChange: (v: boolean) => void;
  onDone: (msg: string) => void;
  onError: (m: string) => void;
}) {
  const [displayName, setDisplayName] = React.useState("");
  const [nickname, setNickname] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [isActive, setIsActive] = React.useState(true);

  React.useEffect(() => {
    if (!user) return;
    setDisplayName(user.display_name ?? "");
    setNickname(user.nickname ?? "");
    setEmail(user.email ?? "");
    setIsActive(user.is_active);
  }, [user]);

  const m = useMutation({
    mutationFn: () =>
      api.updateUser(user!.id, {
        display_name: displayName.trim() || null,
        nickname: nickname.trim() || null,
        email: email.trim() || null,
        is_active: isActive,
      }),
    onSuccess: (u) => onDone(`${u.email} 已更新。`),
    onError: (e) => onError(errText(e)),
  });

  return (
    <Dialog open={!!user} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>改资料 · {user?.email}</DialogTitle>
          <DialogDescription>昵称唯一；邮箱改了之后对方下次要用新邮箱登录。</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="eu-name">显示名</Label>
            <Input id="eu-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="eu-nick">昵称</Label>
              <Input id="eu-nick" value={nickname} onChange={(e) => setNickname(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="eu-email">邮箱</Label>
              <Input id="eu-email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
          </div>
          <Switch
            id="eu-active"
            checked={isActive}
            onChange={setIsActive}
            label="账号启用（停用后立刻无法登录，Key 也失效）"
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button loading={m.isPending} onClick={() => m.mutate()}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ResetPasswordDialog({
  user,
  onOpenChange,
  onDone,
  onError,
}: {
  user: AdminUser | null;
  onOpenChange: (v: boolean) => void;
  onDone: (msg: string) => void;
  onError: (m: string) => void;
}) {
  const [pw, setPw] = React.useState("");
  React.useEffect(() => setPw(""), [user]);

  const m = useMutation({
    mutationFn: () => api.updateUser(user!.id, { new_password: pw }),
    onSuccess: (u) => onDone(`${u.email} 的密码已重置（不需要旧密码，D175）。`),
    onError: (e) => onError(errText(e)),
  });

  return (
    <Dialog open={!!user} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>重置密码 · {user?.email}</DialogTitle>
          <DialogDescription>
            超管重置不需要旧密码（D175）—— 请用安全渠道把新密码交给对方。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rp-pw">新密码（≥8 位）</Label>
          <Input id="rp-pw" type="password" value={pw} onChange={(e) => setPw(e.target.value)} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button disabled={pw.length < 8} loading={m.isPending} onClick={() => m.mutate()}>
            重置
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
