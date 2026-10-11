/**
 * 摄取进度的**展示决策**（纯函数）。
 *
 * 与 `lib/boxes.ts` 同一个思路：把"唯一会静默出错的地方"从组件里抽出来，
 * 用真实数据做单测。这里的出错方式是**看起来能显示、其实在说谎**。
 *
 * ## 三条不许越界的规矩
 *
 * 1. **没有任务就不显示进度条**（返回 `null`）。
 *    没有 `jobs` 行时编一个 0% 出来，用户会以为卡住了。
 * 2. **失败不显示进度条**，改显示原因。
 *    一根停在 67% 的条 + "failed" 徽章是自相矛盾的，而且 67% 会被
 *    误读成"再等等就好"。
 * 3. **百分比只来自 `jobs.progress`**（0~1），前端不做任何推算。
 *    `DocumentOut` 里**没有** progress 字段（它在 `jobs` 表上）——
 *    拿 `documents.status` 换算百分比是编数字，比不显示更糟。
 *
 * ## 为什么终态要区分 done / failed / cancelled
 *
 * 三者在页面上长得一样（"不动了"），但对用户的意义完全不同：
 * done 是成功、failed 要重试（`POST /documents/{id}/retry`）、
 * cancelled 是被删除或覆盖掉的旧版本。合成一个"完成"会骗人。
 */

import type { DocStatus, Job } from "../types/api";

/** 进度条的语义色，对应 CSS 里的 badge class。 */
export type Tone = "working" | "ready" | "failed" | "cancelled";

export interface ProgressView {
  /** 0~100 的整数。`showBar=false` 时不代表任何数值，不要用。 */
  pct: number;
  /** 一行人话，直接显示。 */
  label: string;
  tone: Tone;
  /** 只有真的在动的时候才画条。 */
  showBar: boolean;
}

/** `jobs.status` 里算"不会再变"的状态。 */
export const TERMINAL_JOB_STATUSES = ["done", "failed", "cancelled"] as const;

/**
 * 挑出**代表这个文档**的任务。
 *
 * 一个文档可能有多条任务（ingest + embed_visual + thumbnail…）。
 * 文本摄取只看 `ingest`；纯扫描件没有 ingest 产出的 chunk，
 * 那时退回"最新的一条"（视觉腿才是它在做的事）。
 */
export function pickDocJob(jobs: readonly Job[], docId: number): Job | null {
  const mine = jobs.filter((j) => j.doc_id === docId);
  if (mine.length === 0) return null;
  return mine.reduce((best, j) => (prefersJob(j, best) ? j : best));
}

/**
 * 按 doc_id 分组，每个文档只留**一条**代表任务。
 *
 * ⚠️ 选择规则必须与 `pickDocJob` **完全一致**（ingest 优先，其次 id 最大）。
 * 这两个函数是同一件事的两种问法（一篇一篇 vs 整张表），
 * 规则一漂移，列表页和详情页就会显示**不同的任务** ——
 * 那种不一致极难排查（数据是对的，只有显示在骗人）。
 */
export function groupJobsByDoc(jobs: readonly Job[]): Map<number, Job> {
  const out = new Map<number, Job>();
  for (const j of jobs) {
    const key = j.doc_id;
    if (key == null) continue;
    const prev = out.get(key);
    if (!prev || prefersJob(j, prev)) out.set(key, j);
  }
  return out;
}

/** `cand` 是否比 `cur` 更适合当代表。 */
function prefersJob(cand: Job, cur: Job): boolean {
  if (cand.kind !== cur.kind) {
    if (cand.kind === "ingest") return true;
    if (cur.kind === "ingest") return false;
  }
  return cand.id > cur.id;
}

const KIND_LABEL: Record<string, string> = {
  ingest: "解析入库",
  embed_visual: "视觉编码",
  thumbnail: "生成缩略图",
  pdf_preview: "生成预览",
  import_chat: "导入聊天",
  reindex: "重建索引",
  cleanup: "清理过期文件",
};

/**
 * 把一条任务翻译成"显示什么"。
 *
 * @param job 该文档的任务；`null` 表示还没有 → 返回 `null`（不显示）
 * @param docStatus 文档自己的状态。用来覆盖一个真实存在的边界：
 *   文档已经 `ready` 但任务行还没被 worker 收尾（几毫秒的窗口），
 *   此时该显示"完成"而不是"99%"。
 */
export function jobView(job: Job | null, docStatus?: DocStatus): ProgressView | null {
  if (!job) return null;

  const what = KIND_LABEL[job.kind] ?? job.kind;

  switch (job.status) {
    case "done":
      return { pct: 100, label: `${what}完成`, tone: "ready", showBar: false };

    case "failed":
      // 不显示条：停在半路的进度会被读成"还能继续"
      return {
        pct: Math.round(job.progress * 100),
        label: `${what}失败${job.error ? `：${job.error.slice(0, 60)}` : ""}`,
        tone: "failed",
        showBar: false,
      };

    case "cancelled":
      return { pct: 0, label: `${what}已取消`, tone: "cancelled", showBar: false };

    case "pending":
      // progress=0 且还在排队：说"排队中"比"0%"有用 ——
      // 用户看到 0% 会以为卡死了，而事实是 worker 还没轮到它。
      if (job.progress <= 0) {
        return { pct: 0, label: "排队中", tone: "working", showBar: false };
      }
      break;

    case "running":
      break;

    default:
      break;
  }

  // 文档已经 ready 但任务行还没收尾 → 以文档状态为准（D14 允许短暂不一致）
  if (docStatus === "ready") {
    return { pct: 100, label: "完成", tone: "ready", showBar: false };
  }

  const pct = clampPct(job.progress);
  return {
    pct,
    label: `${what} ${pct}%`,
    tone: "working",
    showBar: pct < 100,
  };
}

/** 0~1 → 0~100 的整数，并夹住越界值。 */
export function clampPct(progress: number): number {
  if (!Number.isFinite(progress)) return 0;
  return Math.min(100, Math.max(0, Math.round(progress * 100)));
}

/**
 * 还有没有需要继续轮询的任务。
 *
 * 页面已经在"文档全终态"时停轮询了，但**任务可能比文档慢一步**
 * （`finish_job` 在 handler 返回之后才跑）—— 只看文档会在那几毫秒里
 * 提前收工，进度条停在 97%。所以两个条件都要满足"没有活动任务"。
 */
export function hasActiveJob(jobs: readonly Job[]): boolean {
  return jobs.some((j) => !TERMINAL_JOB_STATUSES.includes(j.status as never));
}