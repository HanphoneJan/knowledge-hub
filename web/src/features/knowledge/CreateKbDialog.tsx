/**
 * 新建知识库对话框。
 *
 * 契约要点（`docs/DESIGN-API.md` §3 + `lib/api.ts::createKb`）：
 * - `workspace_id` 必填（库挂在工作区下）；
 * - `source_kind` ∈ `file` / `chat` / `photo` —— **建库时定死**（之后不可改，
 *   `PATCH /kb/{id}` 的字段清单里没有它）；
 * - `visibility` ∈ 三档；
 * - `chunk_strategy` 等切分参数走默认（`recursive`），要改去库设置页。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "../../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { Select } from "../../components/ui/select";
import { Alert } from "../../components/ui/alert";
import { api, errText } from "../../lib/api";
import { Keys } from "../../app/queryKeys";
import type { ChunkStrategy, SourceKind, Visibility } from "../../types/api";
import { SOURCE_KIND_LABEL } from "./badges";
import { DEFAULT_STRATEGY, STRATEGY_META, strategyChangeNotice, strategyOptions } from "./strategy";

export function CreateKbDialog({
  open,
  onOpenChange,
  defaultWorkspaceId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  defaultWorkspaceId?: number | null;
  onCreated?: (kbId: number) => void;
}) {
  const qc = useQueryClient();
  const wsQ = useQuery({ queryKey: Keys.workspaces(), queryFn: () => api.listWorkspaces(), enabled: open });

  const [name, setName] = React.useState("");
  const [desc, setDesc] = React.useState("");
  const [wsId, setWsId] = React.useState<number | null>(defaultWorkspaceId ?? null);
  const [kind, setKind] = React.useState<SourceKind>("file");
  const [vis, setVis] = React.useState<Visibility>("private");
  const [strategy, setStrategy] = React.useState<ChunkStrategy>(DEFAULT_STRATEGY);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setName("");
    setDesc("");
    setKind("file");
    setVis("private");
    setStrategy(DEFAULT_STRATEGY);
    setErr(null);
    setWsId((prev) => prev ?? defaultWorkspaceId ?? null);
  }, [open, defaultWorkspaceId]);

  const wsList = wsQ.data ?? [];
  React.useEffect(() => {
    if (wsId === null && wsList.length) setWsId(wsList[0].id);
  }, [wsId, wsList]);

  const createM = useMutation({
    mutationFn: () =>
      api.createKb({
        workspace_id: wsId as number,
        name: name.trim(),
        description: desc.trim() || null,
        visibility: vis,
        source_kind: kind,
        chunk_strategy: strategy,
      }),
    onSuccess: (kb) => {
      void qc.invalidateQueries({ queryKey: Keys.kbs() });
      onOpenChange(false);
      onCreated?.(kb.id);
    },
    onError: (e) => setErr(errText(e)),
  });

  const submit = React.useCallback(() => {
    if (!wsId) {
      setErr("先选一个工作区");
      return;
    }
    if (!name.trim()) {
      setErr("名字不能为空");
      return;
    }
    setErr(null);
    createM.mutate();
  }, [wsId, name, createM]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建知识库</DialogTitle>
          <DialogDescription>
            来源类型建库时定死，之后不能改 —— 选错就换一个库。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="kb-name">名称</Label>
            <Input
              id="kb-name"
              value={name}
              placeholder="例如：产品资料"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="kb-desc">描述（可选）</Label>
            <Input
              id="kb-desc"
              value={desc}
              placeholder="一句话说明这个库放什么"
              onChange={(e) => setDesc(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-kind">来源类型</Label>
              <Select
                id="kb-kind"
                value={kind}
                onChange={setKind}
                options={[
                  { value: "file", label: SOURCE_KIND_LABEL.file },
                  { value: "chat", label: SOURCE_KIND_LABEL.chat },
                  { value: "photo", label: SOURCE_KIND_LABEL.photo },
                ]}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-vis">可见性</Label>
              <Select
                id="kb-vis"
                value={vis}
                onChange={setVis}
                options={[
                  { value: "private", label: "私有（只有我）" },
                  { value: "workspace", label: "工作区（成员可见）" },
                  { value: "global", label: "全局（所有用户）" },
                ]}
              />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="kb-strategy">切分策略</Label>
            <Select
              id="kb-strategy"
              value={strategy}
              onChange={setStrategy}
              options={strategyOptions()}
            />
            <div className="text-2xs text-text-secondary">
              边界：{STRATEGY_META[strategy].boundary} —— {STRATEGY_META[strategy].effect}
              {strategy !== DEFAULT_STRATEGY && (
                <>
                  <br />
                  {strategyChangeNotice(strategy, { llmActive: true })[0]}
                </>
              )}
            </div>
          </div>
          {wsList.length > 1 && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-ws">工作区</Label>
              <Select
                id="kb-ws"
                value={wsId === null ? "" : String(wsId)}
                onChange={(v) => setWsId(Number(v))}
                options={wsList.map((w) => ({ value: String(w.id), label: w.name }))}
              />
            </div>
          )}
          {err && <Alert tone="danger">{err}</Alert>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={submit} loading={createM.isPending}>
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
