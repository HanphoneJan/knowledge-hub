/**
 * 知识库「检索」tab —— 搜索栏 + 命中卡 + 高亮 + **调试四腿**（D15/D72）。
 *
 * ## 数据来源
 *
 * 走**跨库检索**端点 `POST /search`（`kb_ids=[这个库]`），而不是单库的
 * `/kb/{id}/search`：`lib/api.ts` 的注释里写明了理由 —— 多选范围一律走它，
 * 单库只是特例，这样前端只有一条检索路径，`include_debug` 也天然支持。
 *
 * ## 三条纪律
 *
 * 1. **正文用 `hit.text`，不用 `snippets`**（D77）：`snippets` 是字面子串匹配的产物，
 *    自然语言提问下常常是空的 —— 它只用来**打高亮**。
 * 2. **`skipped` 的腿不算"没命中"**（D72）：画成 0 结果就是告诉用户"你的照片没被索引"。
 * 3. **RRF 分不是置信度**（≈0.03）：只显示四位小数，不做百分比。
 */
import { Search as SearchIcon, SlidersHorizontal } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Card } from "../../../components/ui/card";
import { EmptyState } from "../../../components/ui/empty-state";
import { Select } from "../../../components/ui/select";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { api, errText, requestBlob } from "../../../lib/api";
import {
  consensusText,
  emptyDebugHint,
  filteredNote,
  fusionSummary,
  hitConsensus,
  LEG_LABEL,
  LEG_SHORT,
  legViews,
  rerankText,
  routeText,
  rrfScoreText,
  slowestLeg,
} from "../../../lib/debug";
import { showThumb, thumbAlt, pageOfThumbUrl } from "../../../lib/thumb";
import { cn } from "../../../lib/cn";
import type { DebugResp, HighlightSpan, KbDetail, SearchHit, SearchMode, SearchResp } from "../../../types/api";
import { useBlobUrl } from "../../chat/useBlobUrl";

const MODES: Array<{ value: SearchMode; label: string; hint: string }> = [
  { value: "hybrid", label: "混合（默认）", hint: "dense + sparse + trgm 融合" },
  { value: "semantic", label: "语义（向量）", hint: "只看向量腿" },
  { value: "exact", label: "精确（关键词）", hint: "BM25 + 子串" },
];

export function SearchTab({ kb }: { kb: KbDetail }) {
  const [query, setQuery] = React.useState("");
  const [mode, setMode] = React.useState<SearchMode>("hybrid");
  const [topK, setTopK] = React.useState(10);
  const [busy, setBusy] = React.useState(false);
  const [resp, setResp] = React.useState<SearchResp | null>(null);
  const [debug, setDebug] = React.useState<Omit<DebugResp, "query" | "mode"> | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [ms, setMs] = React.useState<number | null>(null);

  const run = React.useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      const q = query.trim();
      if (!q || busy) return;
      setBusy(true);
      setErr(null);
      const t0 = performance.now();
      try {
        const r = await api.searchKbs([kb.id], {
          query: q,
          mode,
          top_k: topK,
          include_debug: true,
        });
        setResp(r);
        setDebug(r.debug);
        setMs(performance.now() - t0);
      } catch (e2) {
        setErr(errText(e2));
        setResp(null);
        setDebug(null);
      } finally {
        setBusy(false);
      }
    },
    [query, busy, kb.id, mode, topK],
  );

  const items = resp?.items ?? [];

  return (
    <div className="flex flex-col gap-3.5">
      <Card className="p-3.5">
        <form onSubmit={run} className="flex flex-col gap-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`在「${kb.name}」里搜点什么…`}
              className="h-9 min-w-[12rem] flex-1 rounded-xl border-[0.5px] border-border-button bg-bg-input px-3 text-sm text-text-primary outline-none focus-visible:ring-1 focus-visible:ring-accent-primary"
            />
            <Select
              ariaLabel="检索模式"
              value={mode}
              onChange={setMode}
              options={MODES.map((m) => ({ value: m.value, label: m.label }))}
            />
            <Select
              ariaLabel="top_k"
              value={String(topK)}
              onChange={(v) => setTopK(Number(v))}
              options={["5", "10", "20", "30"].map((v) => ({ value: v, label: `top ${v}` }))}
            />
            <Button type="submit" loading={busy}>
              <SearchIcon aria-hidden="true" className="size-4" />
              检索
            </Button>
          </div>
          <div className="text-2xs text-text-secondary">
            {MODES.find((m) => m.value === mode)?.hint} ·
            检索模式与 top_k 都是契约字段（不是前端自己算的）
          </div>
        </form>
      </Card>

      {err && <Alert tone="danger">{err}</Alert>}

      {resp && (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-text-secondary">
          <span>
            {resp.total} 条命中 · mode={resp.mode} · 往返 {ms == null ? "-" : `${Math.round(ms)}ms`}
          </span>
          {debug && <span>· 后端 elapsed {JSON.stringify(debug.elapsed)}</span>}
        </div>
      )}

      {busy && !resp && <SkeletonRows rows={3} />}

      {resp && items.length === 0 && (
        <EmptyState
          icon={SearchIcon}
          title="没有命中"
          description={debug ? emptyDebugHint(legViews(debug.legs), debug.rrf.length) : "换个更短的关键词试试。"}
        />
      )}

      {items.length > 0 && (
        <div className="flex flex-col gap-2.5">
          {items.map((h) => (
            <HitCard key={`${h.doc_id}-${h.chunk_index}`} hit={h} />
          ))}
        </div>
      )}

      {debug && <DebugPanel debug={debug} />}
    </div>
  );
}

/** 命中卡：文件名 + 引用类型 + RRF 分 + 高亮片段 + 正文（`hit.text`）。 */
function HitCard({ hit }: { hit: SearchHit }) {
  return (
    <Card className="flex flex-col gap-2 p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="truncate text-sm font-medium text-text-primary">{hit.file_name}</span>
        <Badge variant="secondary" size="sm">
          {hit.kind}
        </Badge>
        <span className="text-2xs text-text-secondary">chunk #{hit.chunk_index}</span>
        <span className="ms-auto text-2xs text-text-secondary" title="RRF 分：只能用来排序，不是置信度">
          RRF {hit.score.toFixed(4)}
        </span>
      </div>

      {hit.snippets.length > 0 && (
        <div className="flex flex-col gap-1">
          {hit.snippets.slice(0, 3).map((s, i) => (
            <div key={i} className="text-2xs text-text-secondary">
              <Highlighted span={s} />
            </div>
          ))}
        </div>
      )}

      <div className="max-h-64 overflow-y-auto whitespace-pre-wrap text-13 leading-relaxed text-text-primary">
        {hit.text || "（后端没有返回正文）"}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-2xs text-text-secondary">
        <span>{hit.locations?.length ?? 0} 个坐标框</span>
        {/* ⚠️ 不写「有缩略图」：`thumb_url` 非空**不代表**端点取得回来 ——
            聊天导入 / Office 这类文档 `GET /thumbs/{n}` 会 415（§0.8），
            而那句话正好把"没有页图"说成"有图但没显示"。图在下面，
            加载得到就看得见，加载不到我们静默跳过（415/404 不是错误）。 */}
        {hit.locations == null && <span>该格式暂无坐标（Office / 聊天片段）</span>}
      </div>

      {showThumb(hit.kind, hit.thumb_url) && hit.thumb_url && (
        <HitThumb url={hit.thumb_url} alt={thumbAlt(hit.file_name, pageOfThumbUrl(hit.thumb_url))} />
      )}
    </Card>
  );
}

/** 缩略图：⚠️ 必须 blob（端点要 Bearer，`<img src>` 会静默 401，§6.8）。 */
function HitThumb({ url, alt }: { url: string; alt: string }) {
  const blob = useBlobUrl(() => requestBlob(url), [url]);
  // 415 = 这类文档本来就没有页图（聊天导入 / Office 等）⇒ 静默不显示。
  // 报一条红字错误会把"这个类型没有缩略图"说成"系统坏了"。
  if (blob.errorStatus === 415 || blob.errorStatus === 404) return null;
  if (blob.error) return <div className="text-2xs text-state-error">缩略图加载失败：{blob.error}</div>;
  if (!blob.url) return <div className="h-16 animate-pulse rounded-lg bg-bg-card" style={{ height: 96 }} />;
  return (
    <img
      src={blob.url}
      alt={alt}
      className="max-h-[240px] w-auto rounded-lg border-[0.5px] border-border-button bg-paper object-contain"
      style={{ height: 96 }}
    />
  );
}

/** `snippets` 的用途**只有**打高亮（正文用 `hit.text`，D77）。 */
function Highlighted({ span }: { span: HighlightSpan }) {
  const parts = React.useMemo(() => {
    const needle = span.highlight ?? "";
    if (!needle) return [{ text: span.text, hit: false }];
    const out: Array<{ text: string; hit: boolean }> = [];
    let rest = span.text;
    let guard = 0;
    while (rest && guard < 50) {
      guard += 1;
      const i = rest.indexOf(needle);
      if (i < 0) {
        out.push({ text: rest, hit: false });
        break;
      }
      if (i > 0) out.push({ text: rest.slice(0, i), hit: false });
      out.push({ text: needle, hit: true });
      rest = rest.slice(i + needle.length);
    }
    return out;
  }, [span]);

  return (
    <span className="whitespace-pre-wrap">
      {parts.map((p, i) =>
        p.hit ? (
          <mark key={i} className="rounded bg-mark px-0.5 text-mark-fg">
            {p.text}
          </mark>
        ) : (
          <React.Fragment key={i}>{p.text}</React.Fragment>
        ),
      )}
    </span>
  );
}

/** 调试面板：四腿 + 融合 + 路由 + 精排（D72 的"不能是黑盒"）。 */
function DebugPanel({ debug }: { debug: Omit<DebugResp, "query" | "mode"> }) {
  const views = React.useMemo(() => legViews(debug.legs), [debug.legs]);
  const slow = slowestLeg(debug.elapsed);

  return (
    <Card className="flex flex-col gap-3 p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <SlidersHorizontal aria-hidden="true" className="size-4 text-text-secondary" />
        <span className="text-sm font-medium text-text-primary">检索调试</span>
        <span className="text-2xs text-text-secondary">{fusionSummary(views)}</span>
        {slow && (
          <Badge variant="secondary" size="sm">
            最慢：{LEG_SHORT[slow.name]} {slow.ms}ms
          </Badge>
        )}
      </div>

      {/* 四条腿 */}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {views.map((v) => (
          <div
            key={v.name}
            className={cn(
              "flex flex-col gap-1 rounded-xl border-[0.5px] p-2.5",
              v.state === "skipped"
                ? "border-state-warning/40 bg-state-warning-5"
                : "border-border-button",
            )}
          >
            <div className="flex items-center gap-1.5">
              <span className="text-13 text-text-primary">{LEG_LABEL[v.name]}</span>
              <Badge
                variant={v.state === "ok" ? "success" : v.state === "skipped" ? "warning" : "secondary"}
                size="sm"
                dot
              >
                {v.state === "ok" ? `${v.n} 条` : v.state === "skipped" ? "没跑" : "没命中"}
              </Badge>
            </div>
            <div className="text-2xs text-text-secondary">{v.ms}ms</div>
            {v.state === "skipped" && v.reason && (
              <div className="text-2xs text-state-warning">{v.reason}</div>
            )}
          </div>
        ))}
      </div>

      {/* 路由 / 精排 / 过滤 */}
      <div className="flex flex-col gap-1 text-2xs text-text-secondary">
        <div>路由：{routeText(debug.route)}</div>
        <div>精排：{rerankText(debug.rerank)}</div>
        {filteredNote(debug.filtered_out_deleted) && <div>{filteredNote(debug.filtered_out_deleted)}</div>}
      </div>

      {/* RRF 融合表 */}
      {debug.rrf.length > 0 && (
        <div className="overflow-hidden rounded-xl border-[0.5px] border-border-button">
          <table className="w-full text-2xs">
            <thead className="bg-bg-card text-text-secondary">
              <tr>
                <th className="px-2 py-1.5 text-start font-normal">#</th>
                <th className="px-2 py-1.5 text-start font-normal">doc / chunk</th>
                {(["dense", "sparse", "trgm", "visual"] as const).map((l) => (
                  <th key={l} className="px-2 py-1.5 text-start font-normal">
                    {LEG_SHORT[l]}
                  </th>
                ))}
                <th className="px-2 py-1.5 text-start font-normal">RRF</th>
                <th className="px-2 py-1.5 text-start font-normal">共识</th>
              </tr>
            </thead>
            <tbody>
              {debug.rrf.slice(0, 20).map((r, i) => (
                <tr key={r.key} className="border-t-[0.5px] border-border-button">
                  <td className="px-2 py-1.5 text-text-secondary">{i + 1}</td>
                  <td className="px-2 py-1.5 text-text-primary">
                    #{r.doc_id} / {r.chunk_index}
                  </td>
                  {(["dense", "sparse", "trgm", "visual"] as const).map((l) => (
                    <td key={l} className="px-2 py-1.5 text-text-secondary">
                      {r.ranks[l] != null ? r.ranks[l] : "—"}
                    </td>
                  ))}
                  <td className="px-2 py-1.5 text-text-primary">{rrfScoreText(r.score)}</td>
                  <td className="px-2 py-1.5 text-text-secondary">
                    {hitConsensus(r.ranks) > 1 ? `${hitConsensus(r.ranks)} 腿` : "1 腿"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {debug.rrf.length > 0 && (
        <div className="text-2xs text-text-secondary">
          共识列：{consensusText(debug.rrf[0]?.ranks)}（第一行）。RRF 分只是排序用的倒数排名之和，不是置信度（最高也就 0.03 量级）。
        </div>
      )}
    </Card>
  );
}
