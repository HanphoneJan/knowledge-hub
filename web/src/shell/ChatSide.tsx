/**
 * 对话区的二级面板：**新对话 + 最近会话**（Open WebUI 的侧栏）。
 *
 * ⚠️ 会话列表只能显示「标题 + 时间」：`SessionOut` **没有** `message_count`
 *    （编一个数字比不显示更糟，`lib/sessions.ts` 里记着这条）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquarePlus, Trash2 } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api, errText } from "../lib/api";
import { deleteConfirmText, sessionSummary, sessionTitle, sortSessions } from "../lib/sessions";
import { plainText } from "../components/ui/rich-text";
import { Hint } from "../components/ui/tooltip";
import { EmptyState } from "../components/ui/empty-state";
import { SkeletonRows } from "../components/ui/skeleton";
import { Keys } from "../app/queryKeys";
import { PanelGroup, PanelRow, SidePanel } from "./SidePanel";
import { ScopePicker } from "./ScopePicker";

export function ChatSide() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { sessionId } = useParams();
  const current = sessionId ? Number(sessionId) : null;

  const sessionsQ = useQuery({
    queryKey: Keys.sessions(),
    queryFn: () => api.sessionsAll(),
  });

  const rows = React.useMemo(() => sortSessions(sessionsQ.data?.items ?? []), [sessionsQ.data]);

  const createM = useMutation({
    mutationFn: () => api.createSession({}),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: Keys.sessions() });
      navigate(`/chat/${s.id}`);
    },
  });

  const removeM = useMutation({
    mutationFn: (id: number) => api.deleteSession(id),
    onSuccess: (_r, id) => {
      void qc.invalidateQueries({ queryKey: Keys.sessions() });
      if (current === id) navigate("/chat");
    },
    onError: (e) => window.alert(plainText(errText(e))),
  });

  const handleNew = React.useCallback(() => createM.mutate(), [createM]);

  return (
    <SidePanel
      top={<ScopePicker />}
      title="对话"
      actions={
        <Hint label="新建对话">
          <button
            type="button"
            aria-label="新建对话"
            onClick={handleNew}
            className="flex size-7 items-center justify-center rounded-md text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
          >
            <MessageSquarePlus aria-hidden="true" className="size-4" />
          </button>
        </Hint>
      }
    >
      {sessionsQ.isLoading && <SkeletonRows rows={4} />}
      {!sessionsQ.isLoading && rows.length === 0 && (
        <EmptyState
          size="sm"
          icon={MessageSquarePlus}
          title="还没有会话"
          description="问一句就会自动建一个会话"
        />
      )}
      {rows.length > 0 && (
        <PanelGroup title="最近">
          {rows.map((s) => (
            <PanelRow
              key={s.id}
              active={current === s.id}
              onClick={() => navigate(`/chat/${s.id}`)}
              title={sessionTitle(s)}
              subtitle={sessionSummary(s)}
              trailing={
                <Hint label="删除会话">
                  <button
                    type="button"
                    aria-label={`删除会话 ${sessionTitle(s)}`}
                    onClick={() => {
                      if (window.confirm(plainText(deleteConfirmText(s)))) removeM.mutate(s.id);
                    }}
                    className="flex size-6 items-center justify-center rounded-md text-text-secondary opacity-0 transition-opacity hover:bg-bg-input hover:text-state-error group-hover:opacity-100 focus-visible:opacity-100"
                  >
                    <Trash2 aria-hidden="true" className="size-3.5" />
                  </button>
                </Hint>
              }
            />
          ))}
        </PanelGroup>
      )}
    </SidePanel>
  );
}
