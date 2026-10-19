/**
 * 时间线 tab（D20）—— 月历 + 当日详情 + 纪念日/里程碑。
 *
 * ## 三条纪律
 *
 * 1. **契约的 `days` 是子集**（有照片 / 消息量 ≥ 该月中位数）：某天不在日历里
 *    **不等于**那天没记录 ⇒ 文案必须说"日历只是子集"（`lib/timeline.ts`）。
 * 2. 对 **file 库不适用**（后端 400）：显示"不适用"**并且不发**注定 400 的请求
 *    （`supportsTimeline`）。"空"和"不适用"是两件事。
 * 3. `?month=` / `?date=` 走 URL 是唯一真相（可分享、可后退）。
 */
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { Alert } from "../../../components/ui/alert";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { api, errText } from "../../../lib/api";
import {
  anniversaryLabel,
  dayMarks,
  emptyHint,
  intensity,
  leadingBlanks,
  milestoneNote,
  monthDates,
  monthLabel,
  monthSummary,
  notApplicableHint,
  shiftMonth,
  speakerLine,
  supportsTimeline,
  todayOf,
} from "../../../lib/timeline";
import { Keys } from "../../../app/queryKeys";
import { useBlobUrl } from "../../chat/useBlobUrl";
import { cn } from "../../../lib/cn";
import { isMonthView, type KbDetail } from "../../../types/api";

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export function TimelineTab({ kb }: { kb: KbDetail }) {
  const [params, setParams] = useSearchParams();
  const ok = supportsTimeline(kb.source_kind);

  const month = params.get("month") ?? thisMonth();
  const date = params.get("date");

  const setParam = React.useCallback(
    (patch: Record<string, string | null>) => {
      const p = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) {
        if (v == null) p.delete(k);
        else p.set(k, v);
      }
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  // ⚠️ 不适用就**不发请求**（后端注定 400，发了只是把明确的错误换成另一个）
  const monthQ = useQuery({
    queryKey: Keys.timeline(kb.id, `month:${month}`),
    queryFn: () => api.timelineMonth(kb.id, month),
    enabled: ok && !date,
  });
  const dayQ = useQuery({
    queryKey: Keys.timeline(kb.id, `date:${date ?? ""}`),
    queryFn: () => api.timelineDay(kb.id, date as string),
    enabled: ok && !!date,
  });

  if (!ok) {
    return (
      <Alert tone="warning" title="这个视图对这类库不适用">
        {notApplicableHint(kb.source_kind)}
      </Alert>
    );
  }

  const monthData = monthQ.data && isMonthView(monthQ.data) ? monthQ.data : null;
  const dayData = dayQ.data && !isMonthView(dayQ.data) ? dayQ.data : null;
  const marks = dayMarks(monthData?.days ?? []);
  const dates = monthDates(month);
  const today = todayOf(month);

  return (
    <div className="flex flex-col gap-3.5">
      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2 p-3.5 pb-2">
          <div className="flex items-center gap-2">
            <CalendarDays aria-hidden="true" className="size-4 text-text-secondary" />
            <CardTitle>{monthLabel(month)}</CardTitle>
          </div>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="上个月"
              onClick={() => setParam({ month: shiftMonth(month, -1), date: null })}
            >
              <ChevronLeft aria-hidden="true" className="size-4" />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setParam({ month: thisMonth(), date: null })}>
              回到本月
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="下个月"
              onClick={() => setParam({ month: shiftMonth(month, 1), date: null })}
            >
              <ChevronRight aria-hidden="true" className="size-4" />
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-3.5 pt-0">
          <div className="text-2xs text-text-secondary">
            {monthData
              ? monthSummary(monthData.total, monthData.active_days)
              : monthQ.isLoading
                ? "正在加载…"
                : ""}
            {monthData && Object.keys(monthData.speakers).length > 0
              ? ` · ${speakerLine(monthData.speakers)}`
              : ""}
          </div>

          {monthQ.isError && <Alert tone="danger">{errText(monthQ.error)}</Alert>}
          {monthQ.isLoading && <SkeletonRows rows={3} />}

          {monthData && (
            <>
              <div className="grid max-w-[640px] grid-cols-7 gap-1">
                {["一", "二", "三", "四", "五", "六", "日"].map((w) => (
                  <div key={w} className="py-1 text-center text-2xs text-text-secondary">
                    {w}
                  </div>
                ))}
                {Array.from({ length: leadingBlanks(month) }, (_, i) => (
                  <div key={`b-${i}`} />
                ))}
                {dates.map((d) => {
                  const m = marks[d];
                  const lv = intensity(m);
                  const active = date === d;
                  return (
                    <button
                      key={d}
                      type="button"
                      onClick={() => setParam({ date: active ? null : d })}
                      title={
                        m
                          ? `${d}：${m.total} 条消息${m.photos ? ` · ${m.photos} 张照片` : ""}`
                          : `${d}：日历里没有标记（不代表那天没记录）`
                      }
                      className={cn(
                        "flex h-10 flex-col items-center justify-center rounded-lg border-[0.5px] text-13 transition-colors",
                        active
                          ? "border-border-accent bg-bg-card text-text-primary"
                          : "border-transparent text-text-secondary hover:bg-bg-card",
                        d === today && !active && "ring-1 ring-accent-primary",
                      )}
                      style={
                        lv === 0
                          ? undefined
                          : {
                              backgroundColor: `var(--cal-lv${lv})`,
                              color: "var(--text-primary)",
                            }
                      }
                    >
                      {Number(d.slice(8, 10))}
                    </button>
                  );
                })}
              </div>
              <div className="text-2xs text-text-secondary">
                颜色深浅 = 当天的消息/照片强度（0~3）。⚠️ 后端返回的 days 是子集
                （有照片或消息量高的那些天）—— 某个格子没颜色不等于那天没记录。
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {date && (
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2 p-3.5 pb-2">
            <CardTitle>{date} 的详情</CardTitle>
            <Button variant="ghost" size="sm" onClick={() => setParam({ date: null })}>
              收起
            </Button>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 p-3.5 pt-0">
            {dayQ.isLoading && <SkeletonRows rows={2} />}
            {dayQ.isError && (
              <Alert tone={dayQ.error && (dayQ.error as { status?: number }).status === 400 ? "warning" : "danger"}>
                {errText(dayQ.error)}
              </Alert>
            )}
            {dayData && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-2xs text-text-secondary">
                  <Badge variant="secondary" size="sm">
                    {dayData.total} 条记录
                  </Badge>
                  <Badge variant="secondary" size="sm">
                    {dayData.docs} 份文档
                  </Badge>
                  {Object.entries(dayData.types).map(([k, v]) => (
                    <Badge key={k} variant="secondary" size="sm">
                      {k} {v}
                    </Badge>
                  ))}
                  {Object.keys(dayData.speakers).length > 0 && <span>{speakerLine(dayData.speakers)}</span>}
                </div>

                {dayData.photos.length > 0 && (
                  <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-5">
                    {dayData.photos.map((p) => (
                      <DayThumb key={p.doc_id} docId={p.doc_id} name={p.file_name} />
                    ))}
                  </div>
                )}

                {dayData.highlights.length > 0 ? (
                  <div className="flex flex-col gap-1.5">
                    {dayData.highlights.slice(0, 20).map((h, i) => (
                      <div key={i} className="rounded-lg bg-bg-card p-2.5 text-13 text-text-primary">
                        {Object.entries(h as Record<string, unknown>)
                          .map(([k, v]) => `${k}: ${String(v).slice(0, 120)}`)
                          .join(" · ")}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-2xs text-text-secondary">
                    {emptyHint(null, true)}（`highlights` 为空）
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-2">
        <Anniversaries kbId={kb.id} />
        <Milestones kbId={kb.id} />
      </div>
    </div>
  );
}

function Anniversaries({ kbId }: { kbId: number }) {
  const q = useQuery({ queryKey: Keys.timeline(kbId, "anniversaries"), queryFn: () => api.anniversaries(kbId) });
  return (
    <Card>
      <CardHeader className="p-3.5 pb-2">
        <CardTitle>纪念日</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5 p-3.5 pt-0">
        <div className="text-2xs text-text-secondary">
          契约原话：纪念日 = 有照片的日子（不是"消息最多的一天"）
        </div>
        {q.isLoading && <SkeletonRows rows={2} />}
        {q.data?.length === 0 && <div className="text-2xs text-text-secondary">还没有带照片的日子。</div>}
        {(q.data ?? []).map((a) => (
          <div key={a.date} className="flex items-center justify-between gap-2 text-13 text-text-primary">
            <span>{anniversaryLabel(a)}</span>
            <span className="text-2xs text-text-secondary">{a.photos.length} 张</span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function Milestones({ kbId }: { kbId: number }) {
  const q = useQuery({ queryKey: Keys.timeline(kbId, "milestones"), queryFn: () => api.milestones(kbId) });
  return (
    <Card>
      <CardHeader className="p-3.5 pb-2">
        <CardTitle>里程碑</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5 p-3.5 pt-0">
        <div className="text-2xs text-text-secondary">
          判据 = 按消息量降序取前 N（不设阈值，可解释的规则）
        </div>
        {q.isLoading && <SkeletonRows rows={2} />}
        {q.data?.length === 0 && <div className="text-2xs text-text-secondary">还没有可排的日子。</div>}
        {(q.data ?? []).map((m) => (
          <div key={m.date} className="flex items-center justify-between gap-2 text-13 text-text-primary">
            <span>
              {m.date} · {milestoneNote(m.chat_total)}
            </span>
            <span className="text-2xs text-text-secondary">{m.photos} 张照片</span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function DayThumb({ docId, name }: { docId: number; name: string }) {
  const blob = useBlobUrl(() => api.photoBlob(docId), [docId]);
  if (blob.error) return <div className="flex h-20 items-center justify-center rounded-lg bg-bg-card text-2xs text-state-error">图挂了</div>;
  if (!blob.url) return <div className="h-20 animate-pulse rounded-lg bg-bg-card" />;
  return <img src={blob.url} alt={name} className="h-20 w-full rounded-lg object-cover" />;
}
