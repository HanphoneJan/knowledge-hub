/**
 * 知识库「设置」tab —— 基本设置 / PII 掩码 / 精排 / 删除。
 *
 * 判定逻辑全在冻结的纯逻辑里（`lib/pii.ts` / `lib/rerank.ts`），这里只做接线：
 * - 🔑 `maskState` / `rerankState` 的 `null` 语义（"不启用"而不是"用默认"）别自己重写；
 * - ⚠️ 确认文案用**原生 `window.confirm`** + `plainText()`（去掉 markdown 星号与 emoji）；
 * - ⚠️ 改分块参数**不回溯**，要提示"去文档 tab 点重新解析"；
 * - ⚠️ 契约里 `GET /kb` 没有 `chunk_strategy` 字段（前端类型也没有），所以这里
 *   **不提供**切分策略下拉 —— 改策略要用后端 CLI/脚本，这不是前端漏做，是类型面就这样。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Alert } from "../../../components/ui/alert";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../../components/ui/dialog";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { RichText, plainText } from "../../../components/ui/rich-text";
import { Select } from "../../../components/ui/select";
import { Switch } from "../../../components/ui/switch";
import { api, errText } from "../../../lib/api";
import {
  docRows,
  maskOffConfirmText,
  maskOnConfirmText,
  maskState,
  reportHeadline,
  reportWarnings,
  typeRows,
} from "../../../lib/pii";
import {
  RERANK_DISABLE_PAYLOAD,
  RERANK_HINT,
  rerankEnablePayload,
  rerankState,
} from "../../../lib/rerank";
import { Keys } from "../../../app/queryKeys";
import type { Kb, KbDetail, Visibility } from "../../../types/api";
import { StrategyCard } from "./StrategyCard";

const RERANK_DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3";

export function KbSettingsTab({ kb }: { kb: KbDetail }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [note, setNote] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [confirmDel, setConfirmDel] = React.useState(false);

  const [form, setForm] = React.useState({
    name: kb.name,
    description: kb.description ?? "",
    visibility: kb.visibility as Visibility,
    chunk_size: kb.chunk_size,
    chunk_overlap: kb.chunk_overlap,
  });
  React.useEffect(() => {
    setForm({
      name: kb.name,
      description: kb.description ?? "",
      visibility: kb.visibility,
      chunk_size: kb.chunk_size,
      chunk_overlap: kb.chunk_overlap,
    });
  }, [kb]);

  const piiQ = useQuery({ queryKey: Keys.pii(kb.id), queryFn: () => api.piiReport(kb.id) });
  const rep = piiQ.data ?? null;

  const piiState = maskState(rep, kb.pii_mask);
  const rr = rerankState(kb);

  const syncKb = React.useCallback(
    (updated: Kb) => {
      void qc.invalidateQueries({ queryKey: Keys.kb(kb.id) });
      void qc.invalidateQueries({ queryKey: Keys.kbs() });
      return updated;
    },
    [qc, kb.id],
  );

  const saveM = useMutation({
    mutationFn: () =>
      api.updateKb(kb.id, {
        name: form.name.trim(),
        description: form.description.trim() || null,
        visibility: form.visibility,
        chunk_size: Number(form.chunk_size),
        chunk_overlap: Number(form.chunk_overlap),
      }),
    onSuccess: (k) => {
      syncKb(k);
      setErr(null);
      setNote("已保存。改了分块配置要去「文档」tab 对相关文档点「重新解析」才会生效（不回溯）。");
    },
    onError: (e) => setErr(errText(e)),
  });

  const imageDescM = useMutation({
    mutationFn: (on: boolean) => api.updateKb(kb.id, { generate_image_desc: on }),
    onSuccess: (k) => {
      syncKb(k);
      setNote("已更新（只对之后摄取的图片生效）。");
    },
    onError: (e) => setErr(errText(e)),
  });

  const rerankM = useMutation({
    mutationFn: (on: boolean) =>
      api.setRerank(kb.id, on ? rerankEnablePayload(RERANK_DEFAULT_MODEL) : RERANK_DISABLE_PAYLOAD),
    onSuccess: (k) => {
      syncKb(k);
      setNote(rerankState(k).on ? "精排已开启（这个库的搜索结果会重新排序）" : "精排已关闭");
    },
    onError: (e) => setErr(errText(e)),
  });

  const piiM = useMutation({
    mutationFn: (on: boolean) => api.setPiiMask(kb.id, on),
    // ⚠️ 提示文案要看**这次的入参**（`on`），不能看渲染时的 `piiState` ——
    //    那是改动前的旧值，会说反（"已开启"实际刚关掉）。
    onSuccess: (k, on) => {
      syncKb(k);
      void qc.invalidateQueries({ queryKey: Keys.pii(kb.id) });
      setNote(on ? "掩码已开启（只对之后摄取的文档生效）" : "掩码已关闭");
    },
    onError: (e) => setErr(errText(e)),
  });

  const delM = useMutation({
    mutationFn: () => api.deleteKb(kb.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: Keys.kbs() });
      void qc.invalidateQueries({ queryKey: Keys.kbTrash() });
      navigate("/kb");
    },
    onError: (e) => {
      setErr(errText(e));
      setConfirmDel(false);
    },
  });


  return (
    <div className="flex flex-col gap-3.5">
      {err && <Alert tone="danger">{err}</Alert>}
      {note && (
        <Alert tone="info" actions={<Button variant="ghost" size="xs" onClick={() => setNote(null)}>知道了</Button>}>
          {note}
        </Alert>
      )}

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>基本设置</CardTitle>
        </CardHeader>
        <CardContent className="p-4 pt-0">
          <div className="flex max-w-[520px] flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-name">名称</Label>
              <Input id="kb-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-desc">描述</Label>
              <Input
                id="kb-desc"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="kb-vis">可见性</Label>
              <Select
                id="kb-vis"
                value={form.visibility}
                onChange={(v) => setForm({ ...form, visibility: v })}
                options={[
                  { value: "private", label: "private · 仅自己" },
                  { value: "workspace", label: "workspace · 工作区成员" },
                  { value: "global", label: "global · 所有用户" },
                ]}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="kb-cs">chunk_size</Label>
                <Input
                  id="kb-cs"
                  type="number"
                  value={form.chunk_size}
                  onChange={(e) => setForm({ ...form, chunk_size: Number(e.target.value) })}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="kb-co">chunk_overlap</Label>
                <Input
                  id="kb-co"
                  type="number"
                  value={form.chunk_overlap}
                  onChange={(e) => setForm({ ...form, chunk_overlap: Number(e.target.value) })}
                />
              </div>
            </div>
            <div className="text-2xs text-text-secondary">
              嵌入模型：{kb.embed_model}（全局统一，D22，不能按库改）·
              chunk_size {100}~{2000} / overlap {0}~{500}（后端校验）
            </div>
            <div className="flex items-center gap-2">
              <Button onClick={() => saveM.mutate()} loading={saveM.isPending}>
                保存
              </Button>
              <span className="text-2xs text-text-secondary">
                改了分块参数同样不回溯 —— 要让已有文档生效，去下一张卡的「重新解析全部文档」。
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      <StrategyCard kb={kb} onSaved={syncKb} onError={setErr} onNote={setNote} />

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>图片描述（D31）</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4 pt-0">
          <Switch
            id="kb-imgdesc"
            checked={kb.generate_image_desc}
            disabled={imageDescM.isPending}
            onChange={(v) => imageDescM.mutate(v)}
            label="为图片生成文字描述再入库（只对之后摄取的图片生效）"
          />
          <div className="text-2xs text-text-secondary">
            关着的时候图片只能靠视觉腿召回（照片库/扫描件建议开）。
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>PII 掩码</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4 pt-0">
          <div className="text-2xs text-text-secondary">
            当前：{piiState.masking === "on" ? "已开启" : "未开启"} ·
            只对之后摄取/重新摄取的文档生效（不回溯）
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant={piiState.masking === "on" ? "secondary" : "default"}
              size="sm"
              disabled={piiM.isPending || piiState.masking === "on"}
              onClick={() => {
                if (window.confirm(plainText(maskOnConfirmText(rep)))) piiM.mutate(true);
              }}
            >
              {piiState.masking === "on" ? "已开启" : "开启掩码"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={piiM.isPending || piiState.masking === "off"}
              onClick={() => {
                if (window.confirm(plainText(maskOffConfirmText()))) piiM.mutate(false);
              }}
            >
              关闭掩码
            </Button>
            <Button variant="ghost" size="sm" loading={piiQ.isFetching} onClick={() => void piiQ.refetch()}>
              重新检查
            </Button>
          </div>

          {rep && (
            <div className="flex flex-col gap-1.5 text-2xs text-text-secondary">
              <div className="text-13 font-medium text-text-primary">
                <RichText text={reportHeadline(rep)} />
              </div>
              {reportWarnings(rep).map((w, i) => (
                <div key={i}>
                  <RichText text={w} />
                </div>
              ))}
              {piiState.backfill_needed && (
                <Alert tone="warning">
                  掩码已开，但索引里还有原文 —— 那些文档要逐份「重新摄取」才会被掩（不回溯）。
                </Alert>
              )}
              {typeRows(rep.source_by_type).length > 0 && (
                <ul className="my-1 list-disc ps-4">
                  {typeRows(rep.source_by_type).map((r) => (
                    <li key={r.type}>
                      {r.label}：{r.n}
                    </li>
                  ))}
                </ul>
              )}
              {docRows(rep).length > 0 && (
                <div className="mt-1 overflow-hidden rounded-lg border-[0.5px] border-border-button">
                  <table className="w-full text-start">
                    <thead className="bg-bg-card text-text-secondary">
                      <tr>
                        <th className="px-2 py-1 text-start font-normal">文件</th>
                        <th className="w-16 px-2 py-1 text-start font-normal">检出</th>
                        <th className="px-2 py-1 text-start font-normal">类型</th>
                      </tr>
                    </thead>
                    <tbody>
                      {docRows(rep).map((d) => (
                        <tr key={d.doc_id} className="border-t-[0.5px] border-border-button">
                          <td className="max-w-0 truncate px-2 py-1">{d.file_name}</td>
                          <td className="px-2 py-1">{d.total}</td>
                          <td className="px-2 py-1">{d.kinds}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>精排（Reranker）</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4 pt-0">
          <div className="text-2xs text-text-secondary">
            {rr.on ? `已开启 · ${rr.model}` : "未开启（按库可选，默认关）"}
          </div>
          <div className="text-2xs text-text-secondary">
            <RichText text={RERANK_HINT} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant={rr.on ? "secondary" : "default"}
              disabled={rerankM.isPending || rr.on}
              onClick={() => {
                if (window.confirm(plainText(RERANK_HINT))) rerankM.mutate(true);
              }}
            >
              开启精排
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={rerankM.isPending || !rr.on}
              onClick={() => rerankM.mutate(false)}
            >
              关闭精排
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="border-state-error/40">
        <CardHeader className="p-4 pb-2">
          <CardTitle>删除知识库</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 p-4 pt-0">
          <div className="text-2xs text-text-secondary">
            软删除，30 天内可以在「设置 → 回收站」恢复（D26/D42）。想彻底清掉要另外清空回收站。
          </div>
          <div>
            <Button variant="danger" onClick={() => setConfirmDel(true)}>
              删除这个库
            </Button>
          </div>
        </CardContent>
      </Card>

      <Dialog open={confirmDel} onOpenChange={setConfirmDel}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除「{kb.name}」？</DialogTitle>
            <DialogDescription>
              库连同它的文档一起进回收站，30 天内可恢复。恢复时那些文档会重新摄取一遍。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDel(false)}>
              取消
            </Button>
            <Button variant="destructive" loading={delM.isPending} onClick={() => delM.mutate()}>
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
