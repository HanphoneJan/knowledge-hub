/**
 * 外部大模型分区（D170/D171）。
 *
 * ## 契约要点（`docs/DESIGN-API.md` §12）
 *
 * - 🔴 **API Key 明文只进不出**：读回来只有 `api_key_masked` + `has_key`；
 * - 🔑 **写时 `api_key` 不传 = 保持原值**（不是清空）⇒ 输入框留空就别把这个字段发出去；
 *   要清空用 `clear_api_key=true`；
 * - 🔑 `external_active` 由**后端算**（provider=openai_compat ∧ enabled ∧ base_url/model/key 齐全），
 *   前端**不重算**；
 * - 🔑 `use_for_ask` / `use_for_contextual` 是**分开**的开关（D171）；
 * - 🔒 写端点要 owner —— 403 原样显示。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Sparkles, Trash2, Zap } from "lucide-react";
import * as React from "react";

import { Alert } from "../../../components/ui/alert";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { Select } from "../../../components/ui/select";
import { SkeletonRows } from "../../../components/ui/skeleton";
import { Switch } from "../../../components/ui/switch";
import { api, errText } from "../../../lib/api";
import { Keys } from "../../../app/queryKeys";
import type { LlmProvider } from "../../../types/api";

interface Header {
  key: string;
  value: string;
}

export function LlmSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: Keys.llm(), queryFn: () => api.llmConfig() });

  const [provider, setProvider] = React.useState<LlmProvider>("extractive");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [model, setModel] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [clearKey, setClearKey] = React.useState(false);
  const [headers, setHeaders] = React.useState<Header[]>([]);
  const [temperature, setTemperature] = React.useState("0");
  const [enabled, setEnabled] = React.useState(false);
  const [useAsk, setUseAsk] = React.useState(false);
  const [useCtx, setUseCtx] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const [note, setNote] = React.useState<string | null>(null);

  React.useEffect(() => {
    const c = q.data;
    if (!c) return;
    setProvider(c.provider);
    setBaseUrl(c.base_url ?? "");
    setModel(c.model ?? "");
    setApiKey("");
    setClearKey(false);
    setHeaders(Object.entries(c.extra_headers ?? {}).map(([key, value]) => ({ key, value })));
    setTemperature(String(c.temperature ?? 0));
    setEnabled(c.enabled);
    setUseAsk(c.use_for_ask);
    setUseCtx(c.use_for_contextual);
  }, [q.data]);

  const saveM = useMutation({
    mutationFn: () => {
      const hdr: Record<string, string> = {};
      for (const h of headers) {
        if (h.key.trim()) hdr[h.key.trim()] = h.value;
      }
      const body: Parameters<typeof api.updateLlm>[0] = {
        provider,
        base_url: baseUrl.trim() || null,
        model: model.trim() || null,
        extra_headers: hdr,
        temperature: Number(temperature) || 0,
        enabled,
        use_for_ask: useAsk,
        use_for_contextual: useCtx,
      };
      // ⚠️ 留空 = **保持原值**（不传这个字段），不是清空
      if (apiKey.trim()) body.api_key = apiKey.trim();
      if (clearKey) body.clear_api_key = true;
      return api.updateLlm(body);
    },
    onSuccess: (c) => {
      setErr(null);
      setNote(
        c.external_active
          ? "已保存，当前生效：会走外部大模型。"
          : "已保存。当前不生效（provider / enabled / base_url / model / key 没配齐）。",
      );
      setApiKey("");
      setClearKey(false);
      void qc.invalidateQueries({ queryKey: Keys.llm() });
    },
    onError: (e) => {
      setNote(null);
      setErr(errText(e));
    },
  });

  const testM = useMutation({
    mutationFn: () => api.testLlm(),
    onSuccess: (r) => {
      setErr(null);
      setNote(
        `${r.ok ? "连通" : "失败"}：${r.message}` +
          (r.latency_ms != null ? `（${r.latency_ms}ms）` : "") +
          (r.model ? ` · 模型 ${r.model}` : ""),
      );
    },
    onError: (e) => {
      setNote(null);
      setErr(errText(e));
    },
  });

  const c = q.data;

  return (
    <div className="flex max-w-[760px] flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && (
        <Alert tone={note.startsWith("连通") || note.startsWith("已保存，当前生效") ? "success" : "info"}
          actions={<Button variant="ghost" size="xs" onClick={() => setNote(null)}>知道了</Button>}>
          {note}
        </Alert>
      )}

      <Card>
        <CardHeader className="p-4 pb-2">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle>外部大模型</CardTitle>
            {c && (
              <Badge variant={c.external_active ? "success" : "secondary"} size="sm" dot>
                {c.external_active ? "生效中" : "未生效"}
              </Badge>
            )}
            {c?.has_key && (
              <Badge variant="secondary" size="sm">
                Key {c.api_key_masked}
              </Badge>
            )}
          </div>
          <CardDescription>
            配置落库、立即生效；生成层与 Contextual 切分都读它。API Key 明文只进不出，读回来只有掩码。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4 pt-0">
          {q.isLoading && <SkeletonRows rows={4} />}
          {q.isError && <Alert tone="danger">{errText(q.error)}</Alert>}

          {c && (
            <>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="llm-provider">provider</Label>
                  <Select
                    id="llm-provider"
                    value={provider}
                    onChange={(v) => setProvider(v as LlmProvider)}
                    options={[
                      { value: "extractive", label: "extractive（本地抽取式，默认）" },
                      { value: "openai_compat", label: "openai_compat（外部兼容网关）" },
                    ]}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="llm-model">model</Label>
                  <Input id="llm-model" value={model} onChange={(e) => setModel(e.target.value)} placeholder="例如 gpt-4o-mini" />
                </div>
                <div className="flex flex-col gap-1.5 sm:col-span-2">
                  <Label htmlFor="llm-base">base_url</Label>
                  <Input
                    id="llm-base"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="例如 https://api.openai.com/v1"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="llm-key">
                    api_key{apiKey ? "（会覆盖）" : "（留空 = 保持原值）"}
                  </Label>
                  <Input
                    id="llm-key"
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={c.has_key ? c.api_key_masked ?? "已配置" : "还没配"}
                    autoComplete="off"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="llm-temp">temperature</Label>
                  <Input
                    id="llm-temp"
                    value={temperature}
                    onChange={(e) => setTemperature(e.target.value)}
                  />
                </div>
              </div>

              {c.has_key && (
                <Switch
                  id="llm-clear"
                  checked={clearKey}
                  onChange={setClearKey}
                  label="清空已保存的 API Key（clear_api_key=true）"
                />
              )}

              <div className="flex flex-col gap-2 rounded-xl bg-bg-card p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-13 text-text-primary">自定义请求头 extra_headers</div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setHeaders((h) => [...h, { key: "", value: "" }])}
                  >
                    <Plus aria-hidden="true" className="size-3.5" />
                    加一条
                  </Button>
                </div>
                <div className="text-2xs text-text-secondary">
                  某些网关必须带特殊头（例如 OpenCode Zen 的 x-opencode-session）——
                  不引代码就能接。
                </div>
                {headers.length === 0 && <div className="text-2xs text-text-secondary">（没有自定义头）</div>}
                {headers.map((h, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <Input
                      aria-label={`请求头名 ${i + 1}`}
                      className="flex-1"
                      value={h.key}
                      placeholder="Header-Name"
                      onChange={(e) =>
                        setHeaders((prev) => prev.map((x, j) => (i === j ? { ...x, key: e.target.value } : x)))
                      }
                    />
                    <Input
                      aria-label={`请求头值 ${i + 1}`}
                      className="flex-1"
                      value={h.value}
                      placeholder="value"
                      onChange={(e) =>
                        setHeaders((prev) => prev.map((x, j) => (i === j ? { ...x, value: e.target.value } : x)))
                      }
                    />
                    <Button
                      variant="delete"
                      size="icon-sm"
                      aria-label={`删除请求头 ${i + 1}`}
                      onClick={() => setHeaders((prev) => prev.filter((_, j) => j !== i))}
                    >
                      <Trash2 aria-hidden="true" className="size-3.5" />
                    </Button>
                  </div>
                ))}
              </div>

              <div className="flex flex-col gap-2">
                <Switch id="llm-enabled" checked={enabled} onChange={setEnabled} label="启用外部大模型" />
                <Switch
                  id="llm-ask"
                  checked={useAsk}
                  onChange={setUseAsk}
                  label="问答生成用它（use_for_ask）"
                />
                <Switch
                  id="llm-ctx"
                  checked={useCtx}
                  onChange={setUseCtx}
                  label="Contextual 切分用它（use_for_contextual）"
                />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button loading={saveM.isPending} onClick={() => saveM.mutate()}>
                  保存
                </Button>
                <Button variant="outline" loading={testM.isPending} onClick={() => testM.mutate()}>
                  <Zap aria-hidden="true" className="size-3.5" />
                  用当前配置试一次
                </Button>
                <span className="text-2xs text-text-secondary">
                  {c.updated_at
                    ? `上次修改 ${new Date(c.updated_at).toLocaleString()}`
                    : "还没改过（用的是 env 兜底）"}
                </span>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>默认生成层</CardTitle>
          <CardDescription>
            provider=extractive 时答案由检索到的原文句子拼装（可核验、无幻觉），
            并且每条角标都指得到原文；换成外部 LLM 后引用仍要求是合法角标，悬空会被后端拦下。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-2 p-4 pt-0 text-2xs text-text-secondary">
          <Sparkles aria-hidden="true" className="size-3.5" />
          无论用哪种生成层，检索实现都只有一套（services/retrieval/）。
        </CardContent>
      </Card>
    </div>
  );
}
