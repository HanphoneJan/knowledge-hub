/**
 * 工作区与成员分区（D120/D122 · RAGFlow 的成员表观感）。
 *
 * ## 四条纪律（`lib/workspace.ts` 的注释里都有）
 *
 * 1. 🔑 **邀请码明文只出现一次** —— 当场显示 + 复制，且**不进 localStorage**；
 * 2. ⚠️ **兑换失败是一句话**：原样显示后端的话，**不替它猜**原因（区分开就等于给攻击者探测信号）；
 * 3. ⚠️ **移除成员不可撤销**：确认文案要说清；
 * 4. 🔑 **用量只给 owner 看**，且要显示**两个**字节数（原始 vs 含派生物的占盘）——
 *    只显示一个会让人以为"删库能腾出那么多空间"。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Plus, Ticket, Trash2, UsersRound } from "lucide-react";
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
import { EmptyState } from "../../../components/ui/empty-state";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { RichText, plainText } from "../../../components/ui/rich-text";
import { Select } from "../../../components/ui/select";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { Tabs } from "../../../components/ui/tabs";
import { api, errText } from "../../../lib/api";
import {
  addMemberHint,
  allWsLabel,
  copyFeedback,
  diskText,
  inviteOneTimeNotice,
  inviteUsable,
  redeemResultText,
  removeMemberText,
  revokeInviteText,
  usageScopeHint,
  usageText,
} from "../../../lib/workspace";
import { Keys } from "../../../app/queryKeys";
import { useAuth } from "../../../app/auth";
import { useScope } from "../../../app/scope";
import type { InviteRow, MemberRow, Workspace } from "../../../types/api";

export function MembersSection() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const { kbs } = useScope();
  const [tab, setTab] = React.useState<"list" | "redeem">("list");
  const [err, setErr] = React.useState<string | null>(null);
  const [note, setNote] = React.useState<string | null>(null);
  const [wsId, setWsId] = React.useState<number | null>(null);
  const [creatingWs, setCreatingWs] = React.useState(false);

  const wsQ = useQuery({ queryKey: Keys.workspaces(), queryFn: () => api.listWorkspaces() });
  const wss = wsQ.data ?? [];

  React.useEffect(() => {
    if (wsId == null && wss.length) setWsId(wss[0].id);
  }, [wsId, wss]);

  const membersQ = useQuery({
    queryKey: Keys.members(wsId ?? 0),
    queryFn: () => api.members(wsId as number),
    enabled: wsId != null,
  });

  const current: Workspace | undefined = wss.find((w) => w.id === wsId);
  const isOwner = current != null && user != null && current.owner_id === user.id;

  const removeM = useMutation({
    mutationFn: (m: MemberRow) => api.removeMember(wsId as number, m.user_id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: Keys.members(wsId as number) });
      setNote("已移除该成员。");
    },
    onError: (e) => setErr(errText(e)),
  });

  return (
    <div className="flex flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && (
        <Alert tone="success" actions={<Button variant="ghost" size="xs" onClick={() => setNote(null)}>知道了</Button>}>
          {note}
        </Alert>
      )}

      <Tabs
        value={tab}
        onChange={(v) => setTab(v as "list" | "redeem")}
        items={[
          { value: "list", label: "工作区与成员", icon: UsersRound },
          { value: "redeem", label: "用邀请码加入", icon: Ticket },
        ]}
      />

      {tab === "redeem" && <RedeemCard onError={setErr} onDone={setNote} />}

      {tab === "list" && (
        <>
          {wsQ.isLoading && <SkeletonRows rows={3} />}
          {wss.length === 0 && !wsQ.isLoading && (
            <EmptyState
              icon={UsersRound}
              title="还没有工作区"
              actions={<Button onClick={() => setCreatingWs(true)}>新建工作区</Button>}
              description="工作区是成员的边界；注册时会自动给你建一个。也可以现在就建一个。"
            />
          )}

          <div className="flex flex-wrap items-center gap-2">
            {wss.length > 0 && (
              <>
                <Label htmlFor="ws-pick" className="text-2xs text-text-secondary">
                  工作区
                </Label>
                <Select
                  id="ws-pick"
                  value={wsId == null ? "" : String(wsId)}
                  onChange={(v) => setWsId(Number(v))}
                  options={wss.map((w) => ({ value: String(w.id), label: w.name }))}
                />
              </>
            )}
            <Button variant="outline" size="sm" onClick={() => setCreatingWs(true)}>
              <Plus aria-hidden="true" className="size-3.5" />
              新建工作区
            </Button>
            <span className="text-2xs text-text-secondary">
              {allWsLabel(kbs)} —— 列表只显示你自己的成员关系
            </span>
          </div>

          {current && (
            <Card>
              <CardHeader className="p-4 pb-2">
                <div className="flex flex-wrap items-center gap-2">
                  <CardTitle>{current.name}</CardTitle>
                  <Badge variant={isOwner ? "warning" : "secondary"} size="sm">
                    {isOwner ? "owner" : "member"}
                  </Badge>
                </div>
                <CardDescription>
                  {current.description?.trim() || "（没有描述）"} · id {current.id}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3 p-4 pt-0">
                <UsageCard wsId={current.id} isOwner={isOwner} />

                {membersQ.isLoading && <SkeletonRows rows={2} />}
                {membersQ.isError && <Alert tone="danger">{errText(membersQ.error)}</Alert>}
                {membersQ.data && (
                  <div className="overflow-hidden rounded-2xl border-[0.5px] border-border-button">
                    <table className="w-full">
                      <thead className="bg-bg-title text-text-secondary">
                        <tr>
                          <th className="px-4 py-2.5 text-start text-2xs font-normal">成员</th>
                          <th className="px-4 py-2.5 text-start text-2xs font-normal">邮箱</th>
                          <th className="px-4 py-2.5 text-start text-2xs font-normal">角色</th>
                          <th className="w-16 px-4 py-2.5 text-end text-2xs font-normal">操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {membersQ.data.map((m) => (
                          <tr key={m.user_id} className="border-t-[0.5px] border-border-button hover:bg-bg-card">
                            <td className="px-4 py-2.5">
                              <div className="flex items-center gap-2">
                                <Avatar name={m.display_name || m.email} size="sm" />
                                <span className="truncate text-13 text-text-primary">
                                  {m.display_name || m.email}
                                </span>
                                {m.email === user?.email && (
                                  <Badge variant="secondary" size="sm">
                                    你
                                  </Badge>
                                )}
                              </div>
                            </td>
                            <td className="px-4 py-2.5 text-2xs text-text-secondary">{m.email}</td>
                            <td className="px-4 py-2.5">
                              <Badge variant={m.role === "owner" ? "warning" : "secondary"} size="sm">
                                {m.role}
                              </Badge>
                            </td>
                            <td className="px-4 py-2.5 text-end">
                              {isOwner && m.role !== "owner" && (
                                <Button
                                  variant="delete"
                                  size="icon-sm"
                                  aria-label={`移除 ${m.email}`}
                                  onClick={() => {
                                    if (window.confirm(plainText(removeMemberText(m, user?.email ?? null)))) {
                                      removeM.mutate(m);
                                    }
                                  }}
                                >
                                  <Trash2 aria-hidden="true" className="size-3.5" />
                                </Button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {isOwner && (
                  <AddMemberForm
                    wsId={current.id}
                    onError={setErr}
                    onDone={(m) => {
                      setErr(null);
                      setNote(m);
                      void qc.invalidateQueries({ queryKey: Keys.members(current.id) });
                    }}
                  />
                )}
              </CardContent>
            </Card>
          )}

          {current && isOwner && <InvitesCard wsId={current.id} onError={setErr} />}
          {current && !isOwner && (
            <div className="text-2xs text-text-secondary">
              只有 owner 能加成员 / 发邀请码 / 撤销邀请（其余人看得到列表，看不到按钮）。
            </div>
          )}
        </>
      )}

      <CreateWorkspaceDialog
        open={creatingWs}
        onOpenChange={setCreatingWs}
        onError={(m) => {
          setNote(null);
          setErr(m);
        }}
        onCreated={(ws) => {
          setErr(null);
          setNote(`工作区「${ws.name}」已建好，你自动成为它的 owner。`);
          // 新工作区要能立刻出现在选择器里（列表查询 + 库列表都刷新）
          void qc.invalidateQueries({ queryKey: Keys.workspaces() });
          void qc.invalidateQueries({ queryKey: Keys.kbs() });
          setWsId(ws.id);
        }}
      />
    </div>
  );
}

/**
 * 新建工作区（`POST /workspaces`，D190 补的前端面）。
 *
 * 🔑 服务端会把创建者**自动加成 owner 成员** ⇒ 建完就能直接往里建库。
 * ⚠️ 工作区**名字不唯一**（无 DB 约束）⇒ 重名不是错误，这里不拦。
 */
function CreateWorkspaceDialog({
  open,
  onOpenChange,
  onCreated,
  onError,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (ws: Workspace) => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = React.useState("");
  const [desc, setDesc] = React.useState("");

  React.useEffect(() => {
    if (!open) return;
    setName("");
    setDesc("");
  }, [open]);

  const m = useMutation({
    mutationFn: () =>
      api.createWorkspace({ name: name.trim(), description: desc.trim() || null }),
    onSuccess: (ws) => {
      onOpenChange(false);
      onCreated(ws);
    },
    onError: (e) => onError(errText(e)),
  });

  const tooLong = name.trim().length > 128;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建工作区</DialogTitle>
          <DialogDescription>
            工作区是成员的边界：你可以把知识库放进它，再邀请同事进来（邀请码在下面那张卡）。
            建完之后你是它的 owner。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nws-name">名称</Label>
            <Input
              id="nws-name"
              value={name}
              placeholder="例如：研发部"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && name.trim() && !tooLong) m.mutate();
              }}
            />
            {tooLong && <div className="text-2xs text-state-error">名字最长 128 字（后端限制）</div>}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nws-desc">描述（可选）</Label>
            <Input id="nws-desc" value={desc} onChange={(e) => setDesc(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button disabled={!name.trim() || tooLong} loading={m.isPending} onClick={() => m.mutate()}>
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RedeemCard({ onDone, onError }: { onDone: (m: string) => void; onError: (m: string) => void }) {
  const qc = useQueryClient();
  const [code, setCode] = React.useState("");
  const m = useMutation({
    mutationFn: () => api.redeemInvite(code.trim()),
    onSuccess: (r) => {
      onError("");
      onDone(redeemResultText(r));
      setCode("");
      void qc.invalidateQueries({ queryKey: Keys.workspaces() });
    },
    onError: (e) => onError(errText(e)),
  });
  return (
    <Card>
      <CardHeader className="p-4 pb-2">
        <CardTitle>用邀请码加入工作区</CardTitle>
        <CardDescription>
          失败时后端统一回一句话（不存在 / 过期 / 用完 / 撤销分不出来）—— 这里原样显示，不替它猜。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 p-4 pt-0">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex min-w-[16rem] flex-1 flex-col gap-1.5">
            <Label htmlFor="code">邀请码</Label>
            <Input
              id="code"
              value={code}
              placeholder="8 位，如 ABCD2345"
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && code.trim()) m.mutate();
              }}
            />
          </div>
          <Button disabled={!code.trim()} loading={m.isPending} onClick={() => m.mutate()}>
            加入
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function UsageCard({ wsId, isOwner }: { wsId: number; isOwner: boolean }) {
  const q = useQuery({
    queryKey: Keys.wsStats(wsId),
    queryFn: () => api.workspaceStats(wsId),
    enabled: isOwner,
  });
  if (!isOwner) {
    return <div className="text-2xs text-text-secondary">{usageScopeHint()}</div>;
  }
  if (q.isError) {
    return <div className="text-2xs text-text-secondary">用量读不到：{errText(q.error)}</div>;
  }
  return (
    <div className="flex flex-col gap-1 rounded-xl bg-bg-card p-2.5 text-2xs text-text-secondary">
      <div>{usageText(q.data)}</div>
      <div>{diskText(q.data)}</div>
      <div>口径：我能读的那些库（别人的 private 不在其中 —— 否则这个接口就成了绕过可见性的探针）。</div>
    </div>
  );
}

function AddMemberForm({
  wsId,
  onDone,
  onError,
}: {
  wsId: number;
  onDone: (m: string) => void;
  onError: (m: string) => void;
}) {
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("member");
  const m = useMutation({
    mutationFn: () => api.addMember(wsId, { email: email.trim(), role }),
    onSuccess: (r) => {
      setEmail("");
      onDone(`已把 ${r.display_name || r.email} 加进来（${r.role}）。`);
    },
    onError: (e) => onError(errText(e)),
  });
  return (
    <div className="flex flex-col gap-2 rounded-xl bg-bg-card p-2.5">
      <div className="text-2xs text-text-secondary">
        <RichText text={addMemberHint()} />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-[14rem] flex-1 flex-col gap-1.5">
          <Label htmlFor="am-email">邮箱</Label>
          <Input
            id="am-email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="对方注册时用的邮箱"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="am-role">角色</Label>
          <Select
            id="am-role"
            value={role}
            onChange={setRole}
            options={[
              { value: "member", label: "member" },
              { value: "admin", label: "admin" },
            ]}
          />
        </div>
        <Button disabled={!email.includes("@")} loading={m.isPending} onClick={() => m.mutate()}>
          <Plus aria-hidden="true" className="size-4" />
          添加
        </Button>
      </div>
    </div>
  );
}

function InvitesCard({ wsId, onError }: { wsId: number; onError: (m: string) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: Keys.invites(wsId), queryFn: () => api.invites(wsId) });
  const [ttl, setTtl] = React.useState("7");
  const [uses, setUses] = React.useState("1");
  const [created, setCreated] = React.useState<{ code: string; inv: InviteRow } | null>(null);
  const [copied, setCopied] = React.useState(false);

  const createM = useMutation({
    mutationFn: () =>
      api.createInvite(wsId, { ttl_days: Number(ttl) || null, max_uses: Number(uses) || null }),
    onSuccess: (r) => {
      setCopied(false);
      setCreated({ code: r.code, inv: r.invite });
      void qc.invalidateQueries({ queryKey: Keys.invites(wsId) });
    },
    onError: (e) => onError(errText(e)),
  });

  const revokeM = useMutation({
    mutationFn: (id: number) => api.revokeInvite(wsId, id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: Keys.invites(wsId) }),
    onError: (e) => onError(errText(e)),
  });

  return (
    <Card>
      <CardHeader className="p-4 pb-2">
        <CardTitle>邀请码</CardTitle>
        <CardDescription>
          邀请还没注册的人用它（POST /members 只能加已注册的邮箱）。明文只返回这一次。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4 pt-0">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="iv-ttl">有效期（天）</Label>
            <Input id="iv-ttl" className="w-24" value={ttl} onChange={(e) => setTtl(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="iv-uses">可用次数（≤50）</Label>
            <Input id="iv-uses" className="w-28" value={uses} onChange={(e) => setUses(e.target.value)} />
          </div>
          <Button loading={createM.isPending} onClick={() => createM.mutate()}>
            <Ticket aria-hidden="true" className="size-4" />
            生成邀请码
          </Button>
        </div>

        {q.isLoading && <SkeletonRows rows={2} />}
        {q.data && q.data.length === 0 && (
          <div className="text-2xs text-text-secondary">还没有邀请码。</div>
        )}
        {q.data && q.data.length > 0 && (
          <div className="overflow-hidden rounded-2xl border-[0.5px] border-border-button">
            <table className="w-full">
              <thead className="bg-bg-title text-text-secondary">
                <tr>
                  <th className="px-4 py-2.5 text-start text-2xs font-normal">前缀</th>
                  <th className="px-4 py-2.5 text-start text-2xs font-normal">状态</th>
                  <th className="px-4 py-2.5 text-start text-2xs font-normal">已用 / 上限</th>
                  <th className="px-4 py-2.5 text-start text-2xs font-normal">有效期</th>
                  <th className="w-16 px-4 py-2.5 text-end text-2xs font-normal">操作</th>
                </tr>
              </thead>
              <tbody>
                {q.data.map((inv) => (
                  <tr key={inv.id} className="border-t-[0.5px] border-border-button hover:bg-bg-card">
                    <td className="px-4 py-2.5 font-mono text-2xs text-text-primary">
                      {inv.code_prefix}…
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge
                        variant={inviteUsable(inv) ? "success" : "secondary"}
                        size="sm"
                        dot
                      >
                        {inv.state}
                      </Badge>
                    </td>
                    <td className="px-4 py-2.5 text-2xs text-text-secondary">
                      {inv.used} / {inv.max_uses}
                    </td>
                    <td className="px-4 py-2.5 text-2xs text-text-secondary">{inv.expiry_text}</td>
                    <td className="px-4 py-2.5 text-end">
                      <Button
                        variant="delete"
                        size="icon-sm"
                        aria-label={`撤销邀请码 ${inv.code_prefix}`}
                        disabled={!inviteUsable(inv)}
                        onClick={() => {
                          if (window.confirm(plainText(revokeInviteText(inv)))) revokeM.mutate(inv.id);
                        }}
                      >
                        <Trash2 aria-hidden="true" className="size-3.5" />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>

      <Dialog open={!!created} onOpenChange={(v) => !v && setCreated(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>邀请码（只显示这一次）</DialogTitle>
            <DialogDescription>
              列表里只看得到前 4 位；这一段找不回来，丢了只能重新生成。
            </DialogDescription>
          </DialogHeader>
          {created && (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 rounded-lg bg-bg-card px-2.5 py-2 font-mono text-base tracking-widest">
                  {created.code}
                </code>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    void navigator.clipboard?.writeText(created.code).then(
                      () => setCopied(true),
                      () => setCopied(false),
                    );
                  }}
                >
                  <Copy aria-hidden="true" className="size-3.5" />
                  {copied ? "已复制" : "复制"}
                </Button>
              </div>
              <div className="text-2xs text-text-secondary">
                <RichText text={inviteOneTimeNotice(created.inv, created.code)} />
              </div>
              <div className="text-2xs text-text-secondary">{copyFeedback(copied)}</div>
            </div>
          )}
          <DialogFooter>
            <Button onClick={() => setCreated(null)}>关掉</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
