/**
 * 检索范围（"选哪些库"）的** DOM 侧**。
 *
 * 判定逻辑一律走 `lib/kbScope.ts`（纯函数 + 单测），这里只做：
 * 读存储初始化、把变更写回存储、把 `Scope` 给需要它的页面。
 *
 * 🔑 范围是**跨页面共享**的（问答 / 检索都用它），所以放 context，
 * 不是每个页面自己一份 state（那样两页会各选各的）。
 */
import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { api } from "../lib/api";
import {
  isAll,
  isSelected,
  loadStoredScope,
  saveStoredScope,
  scopeLabel,
  scopeToKbIds,
  toggleScope,
  type Scope,
} from "../lib/kbScope";
import { filterKbsByWs, WS_KEY } from "../lib/workspace";
import type { Kb } from "../types/api";
import { Keys } from "./queryKeys";

interface ScopeValue {
  scope: Scope;
  setScope: (s: Scope) => void;
  /** 全部可见库（范围选择器 / 文案都要它）。 */
  kbs: Kb[];
  kbsLoading: boolean;
  toggle: (id: number) => void;
  isSelected: (id: number) => boolean;
  all: boolean;
  label: string;
  /** 传给 `/ask` 与 `/search` 的 `kb_ids`（`null` = 服务端推导全部）。 */
  kbIds: number[] | null;
  /** 当前工作区过滤（`null` = 全部）—— 面板与列表页**共用一份**。 */
  wsId: number | null;
  setWsId: (id: number | null) => void;
  /** 按 `wsId` 过滤后的库（`lib/workspace.ts::filterKbsByWs`）。 */
  kbsInWs: Kb[];
}

const Ctx = React.createContext<ScopeValue | null>(null);

function readWs(): number | null {
  try {
    const raw = localStorage.getItem(WS_KEY);
    if (raw == null || raw === "all") return null;
    const n = Number(raw);
    return Number.isInteger(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

export function ScopeProvider({ children }: { children: React.ReactNode }) {
  const [scope, setScopeState] = React.useState<Scope>(() => loadStoredScope());
  const [wsId, setWsIdState] = React.useState<number | null>(() => readWs());

  const kbsQ = useQuery({
    queryKey: Keys.kbs(),
    queryFn: () => api.listKb(),
    staleTime: 30 * 1000,
  });

  const kbs = React.useMemo(() => kbsQ.data ?? [], [kbsQ.data]);

  const setScope = React.useCallback((s: Scope) => {
    setScopeState(s);
    saveStoredScope(s);
  }, []);

  const setWsId = React.useCallback((id: number | null) => {
    setWsIdState(id);
    try {
      localStorage.setItem(WS_KEY, id === null ? "all" : String(id));
    } catch {
      /* 隐私模式：本次生效即可 */
    }
  }, []);

  const value = React.useMemo<ScopeValue>(() => {
    const allIds = kbs.map((k) => k.id);
    return {
      scope,
      setScope,
      kbs,
      kbsLoading: kbsQ.isLoading,
      toggle: (id: number) => setScope(toggleScope(scope, id, allIds)),
      isSelected: (id: number) => isSelected(scope, id),
      all: isAll(scope, allIds),
      label: scopeLabel(scope, kbs),
      kbIds: scopeToKbIds(scope),
      wsId,
      setWsId,
      kbsInWs: filterKbsByWs(kbs, wsId),
    };
  }, [scope, setScope, kbs, kbsQ.isLoading, wsId, setWsId]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useScope(): ScopeValue {
  const v = React.useContext(Ctx);
  if (!v) throw new Error("useScope 必须在 <ScopeProvider> 内使用");
  return v;
}

/** 媒体查询（外壳要按断点**只渲染一个**二级面板，不能靠 CSS 藏一个）。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = React.useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  React.useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return matches;
}
