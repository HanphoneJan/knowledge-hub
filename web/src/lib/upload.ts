/**
 * 批量上传的**汇总**进度（纯逻辑）。
 *
 * ## 为什么需要它
 *
 * 单个文档的进度条（`progress.ts`）回答的是"这一份到哪了"。一次传 20 份时，
 * 页面上是 20 行各自跳动 —— 用户的问题是另一个：
 *
 * - 还要等多久？（不知道还剩几份）
 * - 现在到哪一步了？（20 行都在 40%，他不知道该不该等）
 * - 有没有失败的？（失败的行混在里面，容易被跳过）
 *
 * ## 三条不许越界的规矩
 *
 * 1. **平均进度排除失败文档**。把 failed 算成 0% 会让进度条永远卡在
 *    90% 看着像坏了；算成 100% 是在说谎。做法是排除 + 在文案里写明
 *    「1 失败」。
 * 2. **上传阶段与摄取阶段分开说**。上传是"已提交几批"，摄取是"几份完成"——
 *    两者混成一句"上传中 40%"会让人以为已经开始了却不知道在传还是摄取。
 * 3. **不编造总数**。总份数来自用户选的文件数（`File[]`），
 *    失败的那批要如实记成"未提交"，不能悄悄少算。
 */

import type { ProgressView } from "./progress";

export interface BatchItem {
  docId: number;
  /** 这个文档在这次批量里的展示名（一般是 file_name） */
  name: string;
  view: ProgressView | null;
}

/** 上传阶段（POST /kb/{id}/documents 还在飞）。 */
export interface UploadPhase {
  /** 用户选了多少份 */
  total: number;
  /** 已经提交成功的份数 */
  submitted: number;
  /** 一共要发几个请求（D45：单请求 ≤20 个） */
  batches: number;
  /** 已经发完几个请求 */
  batchesDone: number;
}

export interface BatchProgress {
  total: number;
  done: number;
  failed: number;
  active: number;
  /** 平均百分比 0~100（**排除失败文档**，见文件头规矩 1） */
  avgPct: number;
  /** 有失败时为 true —— 用来决定文案里要不要写「N 失败」 */
  hasFailed: boolean;
  /** 还剩几份没结束 */
  remaining: number;
}

const FILES_PER_REQUEST = 20; // D45：单请求最多 20 个文件

/** 按 D45 把文件切成几批。 */
export function splitBatches(total: number, perRequest = FILES_PER_REQUEST): number {
  if (total <= 0) return 0;
  return Math.ceil(total / perRequest);
}

/**
 * 上传阶段的文案。
 *
 * ⚠️ 不能显示百分比 —— `fetch` 拿不到上传进度（要 XHR 才行），
 * 显示一个假的百分比就是编数字。显示**已提交份数 / 批数**，那是真知道的。
 */
export function uploadPhaseLabel(p: UploadPhase): string {
  if (p.total === 0) return "";
  if (p.submitted >= p.total) {
    return `已提交 ${p.total} 份，等摄取…`;
  }
  if (p.batches <= 1) {
    return `正在上传 ${p.submitted}/${p.total} 份…`;
  }
  return `正在上传第 ${Math.min(p.batchesDone + 1, p.batches)}/${p.batches} 批（已提交 ${p.submitted}/${p.total} 份）`;
}

/**
 * 汇总摄取进度。
 *
 * 每份文档的百分比自己算（不看 `showBar`）——
 * "排队中"是 0%，完成是 100%，running 是它自己的 pct。
 */
export function batchProgress(items: readonly BatchItem[]): BatchProgress {
  let done = 0;
  let failed = 0;
  let active = 0;
  const running: number[] = [];

  for (const it of items) {
    const v = it.view;
    if (!v) {
      // 没有任务行 = 不知道到哪了，**不能**当成 0%
      // （progress.ts 同样不显示它：没数据时显示 0% 等于说"卡住了"）
      continue;
    }
    if (v.tone === "ready") {
      done += 1;
      running.push(100);
    } else if (v.tone === "failed") {
      failed += 1;
      // 失败**不进平均**（文件头规矩 1）
    } else if (v.tone === "cancelled") {
      // 被删/被覆盖：既不算完成也不算失败，就不算进度
    } else {
      active += 1;
      running.push(v.pct);
    }
  }

  const avgPct = running.length
    ? Math.round(running.reduce((a, b) => a + b, 0) / running.length)
    : 0;

  return {
    total: items.length,
    done,
    failed,
    active,
    avgPct,
    hasFailed: failed > 0,
    remaining: items.length - done - failed,
  };
}

/**
 * 汇总条的文案。
 *
 * ⚠️ 全部完成且有失败时，**不能说"完成"** ——
 * "20 份已处理"听起来像都成功了。
 */
export function batchLabel(bp: BatchProgress): string {
  if (bp.total === 0) return "";
  if (bp.remaining === 0) {
    const base = `全部结束：${bp.done} 份完成`;
    return bp.hasFailed ? `${base}，${bp.failed} 份失败（见下方各行）` : base;
  }
  const parts = [`${bp.done}/${bp.total} 份完成`, `${bp.avgPct}%`];
  if (bp.active > 0) parts.push(`${bp.active} 份进行中`);
  if (bp.hasFailed) parts.push(`${bp.failed} 份失败`);
  return parts.join(" · ");
}

/** 全部结束（含失败）了吗？—— 决定要不要继续轮询。 */
export function batchSettled(bp: BatchProgress): boolean {
  return bp.total > 0 && bp.remaining === 0;
}

/**
 * 一批文件上传失败时，**哪些还没提交**。
 *
 * ⚠️ 顺序上传下，前面几批已经进去了 —— 只报一个"上传失败"会让用户
 * 以为什么都没发生，然后重传一遍，于是产生一堆重复文档。
 */
export function unsubmittedAfter(
  total: number,
  submittedBeforeFailure: number,
): number {
  return Math.max(0, total - submittedBeforeFailure);
}