/**
 * 对话页（D188 重写的**重点**，向 Open WebUI 看齐）。
 *
 * ## 数据流
 *
 * ```
 * URL /chat/:sessionId ──► GET /sessions/{id}  ──► 历史消息（historyTurns）
 * 输入条 Enter ──► useAsk.send ──► POST /ask (SSE)  ──► sources → delta* → done
 *                                     └─ 新会话：done.session_id → 换 URL
 * ```
 *
 * ## 三条要点
 *
 * 1. 🔑 **首轮才拿得到 `session_id`**：`POST /ask` 的请求体里传 `session_id: null`，
 *    后端在 `done` 帧里给新 id ⇒ 前端拿到后**用 replace 换 URL**（不留一条能返回的空页）。
 * 2. ⚠️ **`sources` 先到就先画引用卡**（D46）：不要等 `done` 出来才显示引用。
 * 3. ⚠️ **历史消息的角标不可核验**（后端 `chat_messages` 只存编号）⇒
 *    抽屉里如实说明，不给死链（见 `types.ts`）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { Alert } from "../../components/ui/alert";
import { plainText } from "../../components/ui/rich-text";
import { PageSpinner } from "../../components/ui/spinner";
import { api, errText } from "../../lib/api";
import { validateTitle } from "../../lib/sessions";
import { Keys } from "../../app/queryKeys";
import { useScope } from "../../app/scope";
import { ChatTopBar } from "./ChatTopBar";
import { CitationDrawer } from "./CitationDrawer";
import { Composer } from "./Composer";
import { EmptyChat } from "./EmptyChat";
import { MessageList } from "./MessageList";
import type { Turn } from "./types";
import { historyTurns, useAsk } from "./useAsk";

export function ChatPage() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { kbIds, label } = useScope();

  const sid = sessionId ? Number(sessionId) : null;

  const sessionQ = useQuery({
    queryKey: Keys.session(sid as number),
    queryFn: () => api.session(sid as number),
    enabled: sid != null,
  });

  /** 刚流式写入的会话 id（见 `onSessionCreated`）。 */
  const streamedRef = React.useRef<number | null>(null);
  /** 已经灌过历史的会话 id。 */
  const seededRef = React.useRef<number | null>(null);

  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [focusIdx, setFocusIdx] = React.useState<number | null>(null);
  /** 点角标时记住"这是哪一轮的引用"（每轮回答有自己的 sources）。 */
  const [focusTurn, setFocusTurn] = React.useState<string | null>(null);

  const onSessionCreated = React.useCallback(
    (id: number) => {
      // 记住"这个会话是刚流式写进去的"：切 URL 时**不能**把 live turns 清掉，
      // 否则刚生成的回答会退化成"历史消息"（角标立刻变得不可核验）。
      streamedRef.current = id;
      void qc.invalidateQueries({ queryKey: Keys.sessions() });
      if (sid == null) navigate(`/chat/${id}`, { replace: true });
      else void qc.invalidateQueries({ queryKey: Keys.session(id) });
    },
    [qc, navigate, sid],
  );

  const { turns, busy, send, stop, setTurns } = useAsk(kbIds, sid, { onSessionCreated });

  // 历史 → turns：**一次会话只灌一次**，且只在"当前没有内容"时灌。
  // ⚠️ 不能无条件用历史覆盖：`done` 之后会 invalidate 会话，覆盖会把刚拿到的
  //    `sources` 丢掉（引用点不开）—— 那正是"前端自己把可核验的信息弄丢"。
  const history = React.useMemo(
    () => historyTurns(sessionQ.data?.messages ?? []),
    [sessionQ.data],
  );
  React.useEffect(() => {
    if (!sessionQ.data || busy) return;
    if (seededRef.current === sessionQ.data.id) return;
    if (turns.length > 0) return;
    setTurns(history);
    seededRef.current = sessionQ.data.id;
  }, [sessionQ.data, busy, history, turns.length, setTurns]);

  // 切会话：换成**另一个**会话才清空；`/chat` → `/chat/{刚建的 id}` 不算换。
  React.useEffect(() => {
    setDrawerOpen(false);
    setFocusTurn(null);
    setFocusIdx(null);
    if (sid == null) {
      seededRef.current = null;
      setTurns([]);
      return;
    }
    if (streamedRef.current === sid) return;
    seededRef.current = null;
    setTurns([]);
  }, [sid, setTurns]);

  const renameM = useMutation({
    mutationFn: (title: string) => api.renameSession(sid as number, title),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: Keys.session(sid as number) });
      void qc.invalidateQueries({ queryKey: Keys.sessions() });
    },
    onError: (e) => window.alert(plainText(errText(e))),
  });

  const handleRename = React.useCallback(() => {
    if (sid == null) return;
    const current = sessionQ.data?.title ?? "";
    const next = window.prompt("会话标题", current);
    if (next == null) return;
    const bad = validateTitle(next);
    if (bad) {
      window.alert(bad);
      return;
    }
    renameM.mutate(next.trim());
  }, [sid, sessionQ.data, renameM]);

  const handleCite = React.useCallback((turnKey: string, idx: number) => {
    setFocusTurn(turnKey);
    setFocusIdx(idx);
    setDrawerOpen(true);
  }, []);

  const handleNew = React.useCallback(() => {
    navigate("/chat");
  }, [navigate]);

  const activeTurn: Turn | undefined = React.useMemo(() => {
    if (focusTurn) {
      const hit = turns.find((t) => t.key === focusTurn);
      if (hit) return hit;
    }
    // 没指定就取最后一条**有来源**的回答
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].role === "assistant" && (turns[i].sources?.length ?? 0) > 0) return turns[i];
    }
    return turns[turns.length - 1];
  }, [turns, focusTurn]);

  const hasCitations = turns.some((t) => (t.citations?.length ?? 0) > 0);
  const title = sessionQ.data?.title?.trim() || (sid ? `会话 #${sid}` : "新对话");

  const handleCopyAll = React.useCallback(() => {
    const text = turns.map((t) => `${t.role === "user" ? "我" : "助手"}：${t.content}`).join("\n\n");
    void navigator.clipboard?.writeText(text);
  }, [turns]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <ChatTopBar
        title={title}
        onRename={sid != null ? handleRename : undefined}
        onNew={handleNew}
        citationsOpen={drawerOpen}
        hasCitations={hasCitations}
        onToggleCitations={() => setDrawerOpen((v) => !v)}
        onCopy={turns.length ? handleCopyAll : undefined}
      />

      {sessionQ.isError && (
        <div className="px-3 pt-3 sm:px-6">
          <Alert tone="danger" title="会话加载失败">
            {errText(sessionQ.error)}
          </Alert>
        </div>
      )}

      {sid != null && sessionQ.isLoading ? (
        <PageSpinner label="正在取会话…" />
      ) : turns.length === 0 ? (
        <EmptyChat onPick={send} />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[58rem] px-4 pb-2 pt-6 sm:px-6">
            <MessageList turns={turns} onCite={handleCite} onRetry={send} />
          </div>
        </div>
      )}

      <Composer
        onSend={send}
        onStop={stop}
        busy={busy}
        placeholder={
          kbIds && kbIds.length === 0
            ? "当前范围里一个库都没选 —— 先在左侧面板顶部选库"
            : `在「${label}」里问点什么…（Enter 发送，Shift+Enter 换行）`
        }
      />

      <CitationDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sources={activeTurn?.sources}
        history={activeTurn?.history}
        focusIdx={focusIdx}
      />
    </div>
  );
}
