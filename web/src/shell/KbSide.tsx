/**
 * 知识库区的二级面板：**工作区切换 + 库列表 + 新建**。
 *
 * 🔑 列表按工作区过滤走 `lib/workspace.ts::filterKbsByWs`（`null` = 全部，
 * 不是空 —— 单工作区用户看不到任何多余控件）。
 */
import { useQuery } from "@tanstack/react-query";
import { BookText, Plus } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { filterKbsByWs, WS_KEY } from "../lib/workspace";
import { Select } from "../components/ui/select";
import { Hint } from "../components/ui/tooltip";
import { SkeletonRows } from "../components/ui/skeleton";
import { EmptyState } from "../components/ui/empty-state";
import { Keys } from "../app/queryKeys";
import { PanelGroup, PanelRow, SidePanel } from "./SidePanel";
import { CreateKbDialog } from "../features/knowledge/CreateKbDialog";
import { SourceKindBadge, VisibilityBadge, VIS_LABEL } from "../features/knowledge/badges";

export function KbSide() {
  const navigate = useNavigate();
  const { kbId } = useParams();
  const current = kbId ? Number(kbId) : null;
  const [creating, setCreating] = React.useState(false);
  const [wsId, setWsId] = React.useState<number | null>(() => {
    const raw = localStorage.getItem(WS_KEY);
    return raw ? Number(raw) : null;
  });

  const kbsQ = useQuery({ queryKey: Keys.kbs(), queryFn: () => api.listKb() });
  const wsQ = useQuery({ queryKey: Keys.workspaces(), queryFn: () => api.listWorkspaces() });

  const kbs = React.useMemo(() => filterKbsByWs(kbsQ.data ?? [], wsId), [kbsQ.data, wsId]);

  // 工作区切换器只在"真的多于一个工作区"时出现（单工作区用户不该看到它）
  const wsList = wsQ.data ?? [];
  const showWs = wsList.length > 1;

  const handleWs = React.useCallback((v: string) => {
    const next = v === "all" ? null : Number(v);
    setWsId(next);
    try {
      localStorage.setItem(WS_KEY, v);
    } catch {
      /* 隐私模式：本次生效即可 */
    }
  }, []);

  return (
    <>
      <SidePanel
        top={
          showWs ? (
            <div className="border-b-[0.5px] border-border-button p-2">
              <div className="px-1 pb-1 text-2xs text-text-secondary">工作区</div>
              <Select
                ariaLabel="工作区"
                className="w-full"
                value={wsId === null ? "all" : String(wsId)}
                onChange={handleWs}
                options={[
                  { value: "all", label: `全部工作区（${wsList.length}）` },
                  ...wsList.map((w) => ({ value: String(w.id), label: w.name })),
                ]}
              />
            </div>
          ) : undefined
        }
        title="知识库"
        actions={
          <Hint label="新建知识库">
            <button
              type="button"
              aria-label="新建知识库"
              onClick={() => setCreating(true)}
              className="flex size-7 items-center justify-center rounded-md text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
            >
              <Plus aria-hidden="true" className="size-4" />
            </button>
          </Hint>
        }
      >
        {kbsQ.isLoading && <SkeletonRows rows={4} />}
        {!kbsQ.isLoading && kbs.length === 0 && (
          <EmptyState
            size="sm"
            icon={BookText}
            title="这个范围里还没有知识库"
            description="新建一个，然后上传文档或导入聊天记录"
            actions={
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="rounded-md bg-text-primary px-3 py-1.5 text-xs text-bg-base"
              >
                新建知识库
              </button>
            }
          />
        )}
        {kbs.length > 0 && (
          <PanelGroup title={`共 ${kbs.length} 个`}>
            {kbs.map((k) => (
              <PanelRow
                key={k.id}
                active={current === k.id}
                onClick={() => navigate(`/kb/${k.id}`)}
                icon={BookText}
                title={k.name}
                subtitle={VIS_LABEL[k.visibility]}
                trailing={<SourceKindBadge kind={k.source_kind} />}
              />
            ))}
          </PanelGroup>
        )}
        {kbs.length > 0 && (
          <div className="px-2 pt-2 text-2xs text-text-secondary">
            可见性：<VisibilityBadge v="private" /> 只有你 ·{" "}
            <VisibilityBadge v="workspace" /> 工作区 · <VisibilityBadge v="global" /> 所有人
          </div>
        )}
      </SidePanel>

      <CreateKbDialog open={creating} onOpenChange={setCreating} defaultWorkspaceId={wsId} />
    </>
  );
}
