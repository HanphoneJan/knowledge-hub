/**
 * API Key 分区（D64）。
 *
 * ## 三条不许绕的纪律（`lib/settings.ts`）
 *
 * 1. 🔴 **明文只出现一次**：新建之后必须**当场、持久地**显示（不是一闪而过的弹窗），
 *    并提供复制；库里只存前缀 + 哈希；
 * 2. 🔑 **吊销不可撤销**：确认文案要带"是哪一把"（用户往往有几把）；
 * 3. ⚠️ **不要每次进页面就 login**（那会往 `api_keys` 表里灌行）—— 这里只读列表。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
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
import { RichText, plainText } from "../../../components/ui/rich-text";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/ui/empty-state";
import { api, errText } from "../../../lib/api";
import {
  defaultKeyLabel,
  keyPrefix,
  keyState,
  oneTimeNotice,
  revokeConfirmText,
  sortKeys,
} from "../../../lib/settings";
import { Keys } from "../../../app/queryKeys";

export function KeysSection() {
  const qc = useQueryClient();
  const [creating, setCreating] = React.useState(false);
  const [name, setName] = React.useState(() => defaultKeyLabel());
  const [plain, setPlain] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  const q = useQuery({ queryKey: Keys.keys(), queryFn: () => api.keys() });

  const createM = useMutation({
    mutationFn: () => api.createKey(name.trim()),
    onSuccess: (r) => {
      setPlain(r.api_key);
      setCreating(false);
      setCopied(false);
      void qc.invalidateQueries({ queryKey: Keys.keys() });
    },
    onError: (e) => setErr(errText(e)),
  });

  const revokeM = useMutation({
    mutationFn: (id: number) => api.revokeKey(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: Keys.keys() }),
    onError: (e) => setErr(errText(e)),
  });

  const rows = React.useMemo(() => sortKeys(q.data ?? []), [q.data]);

  return (
    <div className="flex flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}

      {plain && (
        <Card className="border-state-warning/50">
          <CardHeader className="p-4 pb-2">
            <CardTitle>这把 key 的明文只出现这一次</CardTitle>
            <CardDescription>
              关掉之后本系统再也查不到它（库里只存前缀 + 哈希，契约 §1）。现在复制到密码管理器，
              或者填进 MCP / A2A 客户端配置。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 p-4 pt-0">
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 overflow-x-auto rounded-lg bg-bg-card px-2.5 py-2 font-mono text-13">
                {plain}
              </code>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  void navigator.clipboard?.writeText(plain).then(
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
              <RichText text={oneTimeNotice()} />
            </div>
            <div>
              <Button variant="ghost" size="sm" onClick={() => setPlain(null)}>
                我已经存好了
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2 p-4 pb-2">
          <div className="flex flex-col gap-1">
            <CardTitle>API Key</CardTitle>
            <CardDescription>
              每个 Key 等同密码：能读写你可见范围内的知识库，也会被记进审计。
            </CardDescription>
          </div>
          <Button onClick={() => { setName(defaultKeyLabel()); setCreating(true); }} disabled={!!plain}>
            <Plus aria-hidden="true" className="size-4" />
            新建 Key
          </Button>
        </CardHeader>
        <CardContent className="p-4 pt-0">
          {q.isLoading && <SkeletonRows rows={3} />}
          {q.isError && <Alert tone="danger">{errText(q.error)}</Alert>}
          {q.data && rows.length === 0 && (
            <EmptyState
              size="sm"
              icon={KeyRound}
              title="还没有 API Key"
              description="MCP / A2A / 脚本访问都要一把 —— 登录用的那把是登录时铸的，不在这里。"
            />
          )}
          {rows.length > 0 && (
            <div className="overflow-hidden rounded-xl border-[0.5px] border-border-button">
              <table className="w-full">
                <thead className="bg-bg-title text-text-secondary">
                  <tr>
                    <th className="px-3 py-2 text-start text-2xs font-normal">名称</th>
                    <th className="px-3 py-2 text-start text-2xs font-normal">前缀</th>
                    <th className="px-3 py-2 text-start text-2xs font-normal">状态</th>
                    <th className="px-3 py-2 text-start text-2xs font-normal">最近使用</th>
                    <th className="w-20 px-3 py-2 text-end text-2xs font-normal">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((k) => {
                    const st = keyState(k);
                    return (
                      <tr key={k.id} className="border-t-[0.5px] border-border-button hover:bg-bg-card">
                        <td className="px-3 py-2 text-13 text-text-primary">{k.name || "（无名）"}</td>
                        <td className="px-3 py-2 font-mono text-2xs text-text-secondary">
                          {keyPrefix(k)}…
                        </td>
                        <td className="px-3 py-2">
                          <Badge
                            variant={st.state === "active" ? "success" : "secondary"}
                            size="sm"
                            dot
                          >
                            {st.label}
                          </Badge>
                        </td>
                        <td className="px-3 py-2 text-2xs text-text-secondary">
                          {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "从未"}
                        </td>
                        <td className="px-3 py-2 text-end">
                          <Button
                            variant="delete"
                            size="icon-sm"
                            aria-label={`吊销 ${k.name}`}
                            onClick={() => {
                              if (window.confirm(plainText(revokeConfirmText(k)))) revokeM.mutate(k.id);
                            }}
                          >
                            <Trash2 aria-hidden="true" className="size-3.5" />
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建 API Key</DialogTitle>
            <DialogDescription>
              名字用来区分用途（比如"家里的 MCP 客户端"）；明文只在创建后出现一次。
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="key-name">名称</Label>
            <Input
              id="key-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && name.trim()) createM.mutate();
              }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreating(false)}>
              取消
            </Button>
            <Button disabled={!name.trim()} loading={createM.isPending} onClick={() => createM.mutate()}>
              创建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
