/**
 * 知识库列表 —— RAGFlow 的「数据集」卡片观感（卡片网格 + 可见性徽章 + 元信息）。
 *
 * ⚠️ 卡片上**不显示文档数 / 片段数**：`GET /kb` 的 `KbOut` 里**没有**这些字段
 * （统计只在 `GET /kb/{id}` 上）。为了卡片好看去逐个请求 36 次详情，
 * 或者按 `chunk_size` 编一个数字，都是"用一个假数字换观感"——
 * 真要数字就点进详情（那里是真的）。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookText, Clock, Plus, Scissors } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { EmptyState } from "../../components/ui/empty-state";
import { Menu } from "../../components/ui/menu";
import { Page, PageHeader } from "../../components/ui/page";
import { Select } from "../../components/ui/select";
import { Skeleton } from "../../components/ui/skeleton";
import { api, errText } from "../../lib/api";
import { plainText } from "../../components/ui/rich-text";
import { Keys } from "../../app/queryKeys";
import { useScope } from "../../app/scope";
import type { Kb } from "../../types/api";
import { CreateKbDialog } from "./CreateKbDialog";
import { SourceKindBadge, VisibilityBadge, VIS_LABEL } from "./badges";

export function KbListPage() {
  const navigate = useNavigate();
  const { kbsInWs, kbs, kbsLoading, wsId, setWsId } = useScope();
  const [creating, setCreating] = React.useState(false);
  const wsIds = React.useMemo(() => [...new Set(kbs.map((k) => k.workspace_id))], [kbs]);

  return (
    <Page>
      <PageHeader
        title="知识库"
        meta={
          <>
            <span>{kbsInWs.length} 个</span>
            <span className="text-text-disabled">·</span>
            <span>按可见性分三档，库列表只显示你有权限看的那些</span>
          </>
        }
        actions={
          <>
            {wsIds.length > 1 && (
              <Select
                ariaLabel="工作区"
                value={wsId === null ? "all" : String(wsId)}
                onChange={(v) => setWsId(v === "all" ? null : Number(v))}
                options={[
                  { value: "all", label: "全部工作区" },
                  ...wsIds.map((id) => ({ value: String(id), label: `工作区 #${id}` })),
                ]}
              />
            )}
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden="true" className="size-4" />
              新建知识库
            </Button>
          </>
        }
      />

      {kbsLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-36 w-full rounded-2xl" />
          ))}
        </div>
      ) : kbsInWs.length === 0 ? (
        <EmptyState
          size="lg"
          icon={BookText}
          title="还没有知识库"
          description="知识库是文档 / 聊天 / 照片的容器。建一个，然后把文件拖进去。"
          actions={<Button onClick={() => setCreating(true)}>新建知识库</Button>}
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {kbsInWs.map((k) => (
            <KbCard key={k.id} kb={k} onOpen={() => navigate(`/kb/${k.id}`)} />
          ))}
        </div>
      )}

      <CreateKbDialog
        open={creating}
        onOpenChange={setCreating}
        defaultWorkspaceId={wsId}
        onCreated={(id) => navigate(`/kb/${id}`)}
      />
    </Page>
  );
}

function KbCard({ kb, onOpen }: { kb: Kb; onOpen: () => void }) {
  const qc = useQueryClient();
  const removeM = useMutation({
    mutationFn: () => api.deleteKb(kb.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: Keys.kbs() });
      void qc.invalidateQueries({ queryKey: Keys.kbTrash() });
    },
    onError: (e) => window.alert(plainText(errText(e))),
  });

  return (
    <Card
      className="group flex cursor-pointer flex-col gap-3 p-4 transition-colors hover:border-border-default"
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter") onOpen();
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-bg-card text-text-secondary">
            <BookText aria-hidden="true" className="size-4" />
          </span>
          <span className="truncate text-sm font-medium text-text-primary">{kb.name}</span>
        </div>
        <span onClick={(e) => e.stopPropagation()} className="shrink-0">
          <Menu
            items={[
              { key: "open", label: "打开", onSelect: onOpen },
              {
                key: "delete",
                label: "删除（软删）",
                danger: true,
                onSelect: () => {
                  if (
                    window.confirm(
                      plainText(
                        `删除知识库「${kb.name}」？\n\n库连同它的文档一起进回收站，30 天内可恢复（D26/D42）。`,
                      ),
                    )
                  ) {
                    removeM.mutate();
                  }
                },
              },
            ]}
          />
        </span>
      </div>

      <p className="line-clamp-2 min-h-[2.5rem] text-13 text-text-secondary">
        {kb.description?.trim() || "（没有描述）"}
      </p>

      <div className="mt-auto flex flex-wrap items-center gap-1.5">
        <VisibilityBadge v={kb.visibility} size="sm" />
        <SourceKindBadge kind={kb.source_kind} />
      </div>

      <div className="flex flex-wrap items-center gap-2 text-2xs text-text-secondary">
        <span className="inline-flex items-center gap-1">
          <Scissors aria-hidden="true" className="size-3" />
          {kb.chunk_size}/{kb.chunk_overlap}
        </span>
        <span className="inline-flex items-center gap-1">
          <Clock aria-hidden="true" className="size-3" />
          {new Date(kb.created_at).toLocaleDateString()}
        </span>
        <span className="ms-auto hidden text-text-disabled group-hover:inline">
          {VIS_LABEL[kb.visibility]}
        </span>
      </div>
    </Card>
  );
}
