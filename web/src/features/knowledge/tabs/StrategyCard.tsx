/**
 * 「切分策略」卡（D164 / D181 / D190）。
 *
 * ## 三件事必须同时做到，缺一个就是骗人
 *
 * 1. **能改**（`PATCH /kb/{id}` 的 `chunk_strategy`，契约字段一直在，前端刚接上）；
 * 2. **说清不回溯** —— 改完只对之后摄取的文档生效（D164 的刻意设计：
 *    PATCH 不自动重跑，因为全量重建可能几分钟）；
 * 3. **给出让已有文档生效的出口** —— 否则那句"不回溯"就成了一句空话。
 *    这里的出口是「重新解析全部 N 份」（逐份 `POST /documents/{id}/reparse`）。
 *
 * ⚠️ 顺带把 D181 的「重摄取欠账」（`stale_strategy_docs`）显示在这一张卡里 ——
 * 它本来就是"改了策略但还没重切"的那个数字，和本卡是同一件事。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Scissors } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import { Label } from "../../../components/ui/label";
import { Progress } from "../../../components/ui/progress";
import { RichText, plainText } from "../../../components/ui/rich-text";
import { Select } from "../../../components/ui/select";
import { api, errText } from "../../../lib/api";
import { staleStrategyHint } from "../../../lib/kbStrategy";
import { Keys } from "../../../app/queryKeys";
import type { ChunkStrategy, Kb, KbDetail } from "../../../types/api";
import {
  reparseAllPlan,
  STRATEGY_META,
  strategyChangeNotice,
  strategyOf,
  strategyOptions,
} from "../strategy";

export function StrategyCard({
  kb,
  onSaved,
  onError,
  onNote,
}: {
  kb: KbDetail;
  onSaved: (k: Kb) => void;
  onError: (m: string) => void;
  onNote: (m: string) => void;
}) {
  const qc = useQueryClient();
  const current = strategyOf(kb);
  const [target, setTarget] = React.useState<ChunkStrategy>(current);
  const [done, setDone] = React.useState(0);

  React.useEffect(() => {
    setTarget(strategyOf(kb));
  }, [kb]);

  // LLM 是否真的生效由后端算（`external_active`），前端不重算 —— 只有
  // `contextual` 需要它，读端点任意登录用户都能读。
  const llmQ = useQuery({ queryKey: Keys.llm(), queryFn: () => api.llmConfig() });
  const llmActive = llmQ.data?.external_active ?? true;

  const docsQ = useQuery({ queryKey: Keys.docs(kb.id), queryFn: () => api.listDocs(kb.id) });
  const docs = React.useMemo(() => (docsQ.data ?? []).filter((d) => d.status !== "deleted"), [docsQ.data]);

  const changed = target !== current;
  const meta = STRATEGY_META[target];
  const notices = React.useMemo(
    () => (changed ? strategyChangeNotice(target, { llmActive }) : []),
    [changed, target, llmActive],
  );

  const saveM = useMutation({
    mutationFn: () => api.updateKb(kb.id, { chunk_strategy: target }),
    onSuccess: (k) => {
      onSaved(k);
      onNote(
        `切分策略已改为「${STRATEGY_META[target].label}」—— 只对之后重新摄取的文档生效；` +
          "要用在已有文档上，请点下面的「重新解析全部」。",
      );
    },
    onError: (e) => onError(errText(e)),
  });

  const reparseM = useMutation({
    mutationFn: async () => {
      setDone(0);
      const failures: string[] = [];
      for (const [i, d] of docs.entries()) {
        try {
          await api.reparseDoc(d.id);
        } catch (e) {
          failures.push(`${d.file_name}：${errText(e)}`);
        }
        setDone(i + 1);
      }
      if (failures.length) {
        throw new Error(`${failures.length} 份没能入队 —— ${failures.slice(0, 3).join("；")}`);
      }
      return docs.length;
    },
    onSuccess: (n) => {
      onNote(`已把 ${n} 份文档重新排队解析（可以在「文档」tab 看进度）。`);
      void qc.invalidateQueries({ queryKey: Keys.docs(kb.id) });
      void qc.invalidateQueries({ queryKey: Keys.jobs({ kb_id: kb.id }) });
      void qc.invalidateQueries({ queryKey: Keys.kb(kb.id) });
      setDone(0);
    },
    onError: (e) => {
      onError(errText(e));
      setDone(0);
    },
  });

  const plan = reparseAllPlan(docs.length);
  const stale = staleStrategyHint(kb.stale_strategy_docs);

  return (
    <Card>
      <CardHeader className="p-4 pb-2">
        <div className="flex items-center gap-2">
          <CardTitle>切分策略</CardTitle>
          <span className="rounded-full bg-bg-card px-2 py-0.5 text-2xs text-text-secondary">
            当前：{STRATEGY_META[current].label}
          </span>
        </div>
        <CardDescription>
          一个库只用一种切法（单选 profile）。改了之后只对之后摄取的文档生效，不会自动重切。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 p-4 pt-0">
        <div className="flex max-w-[520px] flex-col gap-1.5">
          <Label htmlFor="kb-strategy">切分策略</Label>
          <Select
            id="kb-strategy"
            value={target}
            onChange={(v) => setTarget(v)}
            options={strategyOptions()}
          />
          <div className="text-2xs text-text-secondary">
            边界：{meta.boundary}
            <br />
            影响：{meta.effect}
          </div>
        </div>

        {changed && (
          <Alert tone="warning" title="换成这个策略之前先看一眼">
            <ul className="mt-1 list-disc ps-4">
              {notices.map((n, i) => (
                <li key={i}>
                  <RichText text={n} />
                </li>
              ))}
            </ul>
          </Alert>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button disabled={!changed} loading={saveM.isPending} onClick={() => saveM.mutate()}>
            保存策略
          </Button>
          {changed && (
            <Button variant="ghost" onClick={() => setTarget(current)}>
              撤销改动
            </Button>
          )}
        </div>

        {stale && (
          <Alert tone="warning" title="重摄取欠账">
            <RichText text={stale} />
          </Alert>
        )}

        <div className="flex flex-col gap-2 rounded-xl bg-bg-card p-3">
          <div className="text-13 text-text-primary">让已有文档用上新策略</div>
          <div className="text-2xs text-text-secondary">
            {plan.text}。⚠️ 重新解析会重新切分并覆盖索引（旧片段被替换），
            正在解析的文档也会重新排队。
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!plan.enabled || reparseM.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    plainText(
                      `重新解析「${kb.name}」的全部 ${docs.length} 份文档？\n\n` +
                        "每份都会被重新切分、重新编码，耗时可能几分钟（可以在「文档」tab 看进度）。",
                    ),
                  )
                ) {
                  reparseM.mutate();
                }
              }}
            >
              <RefreshCw aria-hidden="true" className={reparseM.isPending ? "size-3.5 animate-spin" : "size-3.5"} />
              {reparseM.isPending ? `正在入队 ${done}/${docs.length}` : "重新解析全部文档"}
            </Button>
            {docsQ.isLoading && <span className="text-2xs text-text-secondary">正在数文档…</span>}
            <span className="text-2xs text-text-secondary">
              <Scissors aria-hidden="true" className="inline size-3" /> chunk_size {kb.chunk_size} ·
              overlap {kb.chunk_overlap}
            </span>
          </div>
          {reparseM.isPending && docs.length > 0 && (
            <Progress value={done / docs.length} label="重新解析入队进度" />
          )}
        </div>
      </CardContent>
    </Card>
  );
}
