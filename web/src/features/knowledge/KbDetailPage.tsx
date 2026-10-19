/**
 * 知识库详情（tab 页：文档 / 检索 / 时间线 / 照片 / 设置）。
 *
 * 🔑 **`?tab=` 是唯一真相**（D184/D186 一脉相承）：页签状态不进 React state，
 * 否则"复制链接给同事"打开的会是另一个页面。
 * 🔑 **只有 chat/photo 库才渲染时间线 tab**；**只有 photo 库**才有照片 tab
 * —— 并且**不发**注定 400 的请求（`supportsTimeline`，见 `lib/timeline.ts`）。
 */
import { useQuery } from "@tanstack/react-query";
import { BookText, FileText, Image, Search, Settings, Sparkles } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { Alert } from "../../components/ui/alert";
import { Button } from "../../components/ui/button";
import { PageSpinner } from "../../components/ui/spinner";
import { Tabs } from "../../components/ui/tabs";
import { api, errText } from "../../lib/api";
import { staleStrategyHint } from "../../lib/kbStrategy";
import { bytes } from "../../lib/workspace";
import { supportsTimeline } from "../../lib/timeline";
import { RichText } from "../../components/ui/rich-text";
import { Keys } from "../../app/queryKeys";
import { SourceKindBadge, VisibilityBadge } from "./badges";
import { DocsTab } from "./tabs/DocsTab";
import { KbSettingsTab } from "./tabs/KbSettingsTab";
import { PhotosTab } from "./tabs/PhotosTab";
import { SearchTab } from "./tabs/SearchTab";
import { TimelineTab } from "./tabs/TimelineTab";

const TABS = ["docs", "search", "timeline", "photos", "settings"] as const;
type TabKey = (typeof TABS)[number];

export function KbDetailPage() {
  const { kbId } = useParams();
  const id = Number(kbId);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const kbQ = useQuery({
    queryKey: Keys.kb(id),
    queryFn: () => api.getKb(id),
    enabled: Number.isFinite(id),
  });

  const kb = kbQ.data;
  const rawTab = params.get("tab");
  const tab: TabKey = (TABS as readonly string[]).includes(rawTab ?? "")
    ? (rawTab as TabKey)
    : "docs";

  const setTab = React.useCallback(
    (next: string) => {
      const p = new URLSearchParams(params);
      p.set("tab", next);
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  const items = React.useMemo(() => {
    const out = [
      { value: "docs", label: "文档", icon: FileText },
      { value: "search", label: "检索", icon: Search },
      { value: "settings", label: "设置", icon: Settings },
    ];
    if (kb && supportsTimeline(kb.source_kind)) {
      out.splice(2, 0, { value: "timeline", label: "时间线", icon: Sparkles });
    }
    if (kb?.source_kind === "photo") {
      out.splice(kb && supportsTimeline(kb.source_kind) ? 3 : 2, 0, {
        value: "photos",
        label: "照片",
        icon: Image,
      });
    }
    return out as Array<{ value: string; label: string; icon: typeof FileText }>;
  }, [kb]);

  if (!Number.isFinite(id)) {
    return (
      <div className="p-6">
        <Alert tone="danger">知识库 id 不是数字：{kbId}</Alert>
      </div>
    );
  }

  if (kbQ.isLoading) return <PageSpinner label="正在取知识库…" />;

  if (kbQ.isError || !kb) {
    return (
      <div className="p-6">
        <Alert tone="danger" title="打不开这个知识库">
          {errText(kbQ.error)}
        </Alert>
        <div className="mt-3">
          <Button variant="outline" onClick={() => navigate("/kb")}>
            返回列表
          </Button>
        </div>
      </div>
    );
  }

  const stale = staleStrategyHint(kb.stale_strategy_docs);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b-[0.5px] border-border-button bg-bg-component/80 px-4 pt-4 backdrop-blur sm:px-6">
        <div className="mx-auto w-full max-w-[76rem]">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 flex-col gap-1.5">
              <div className="flex min-w-0 items-center gap-2">
                <BookText aria-hidden="true" className="size-4 shrink-0 text-text-secondary" />
                <h1 className="truncate text-xl font-semibold text-text-primary">{kb.name}</h1>
                <VisibilityBadge v={kb.visibility} />
                <SourceKindBadge kind={kb.source_kind} />
              </div>
              <div className="flex flex-wrap items-center gap-2 text-sm text-text-secondary">
                <span>{kb.doc_count} 份文档</span>
                <span className="text-text-disabled">·</span>
                <span>{kb.chunk_count} 个片段</span>
                <span className="text-text-disabled">·</span>
                <span>{bytes(kb.size_bytes)}</span>
                <span className="text-text-disabled">·</span>
                <span className="truncate">{kb.embed_model}</span>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => navigate("/chat")}>
                用这些库提问
              </Button>
            </div>
          </div>

          {stale && (
            <div className="mt-3">
              <Alert tone="warning">{stale}</Alert>
            </div>
          )}

          <div className="mt-3">
            <Tabs value={tab} onChange={setTab} items={items} className="border-b-0" />
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[76rem] px-4 py-4 sm:px-6">
          {tab === "docs" && <DocsTab kb={kb} />}
          {tab === "search" && <SearchTab kb={kb} />}
          {tab === "timeline" && <TimelineTab kb={kb} />}
          {tab === "photos" && <PhotosTab kb={kb} />}
          {tab === "settings" && <KbSettingsTab kb={kb} />}
        </div>
      </div>
    </div>
  );
}

/** 供 tab 内复用的一句话说明（避免每处各写一份）。 */
export function KbHint({ children }: { children: string }) {
  return (
    <div className="text-2xs text-text-secondary">
      <RichText text={children} />
    </div>
  );
}
