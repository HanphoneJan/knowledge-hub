/**
 * 检索调试面板的**纯逻辑**（D15 · 契约 §12）。
 *
 * 与 `lib/timeline.ts` 同一思路：把"会静默误导用户"的地方从组件里抽出来。
 * 这个面板的全部价值就是**如实说出系统为什么这么排**，所以最容易出错的
 * 恰恰是"把没跑的东西说成跑了"和"把空结果说成没匹配"。
 *
 * ## 四处最容易骗到人的地方
 *
 * 1. 🔴 **`skipped` 的腿不算"没命中"**：视觉腿在 API 进程恒 skipped（D50）。
 *    画成"0 结果"就是告诉用户"你的照片没被索引"。
 * 2. 🔴 **只有一条腿参与时**不能说"多路融合"—— 那是"单腿检索"，
 *    而用户会以为结果可靠（实际少了一条腿的召回）。
 * 3. 🔑 **`ranks` 决定"谁贡献了这条"**：一条命中被两条腿捞到 vs 只被一条腿
 *    捞到，可信度完全不同。前者是共识，后者可能是巧合。
 * 4. ⚠️ **RRF 分不是置信度**（≈0.03）：显示成百分比会让人问"为什么最高分 3%"。
 */

/** 四条腿的固定展示顺序（dense→sparse→trgm→visual，与融合顺序一致）。 */
export const LEG_ORDER: Array<"dense" | "sparse" | "trgm" | "visual"> = [
  "dense",
  "sparse",
  "trgm",
  "visual",
];

/** 四条腿的联合类型（与 `types/api.ts` 的 `LegName` 同源，这里独立定义避免循环依赖）。 */
export type LegName = "dense" | "sparse" | "trgm" | "visual";

/** `string` → `LegName` 的收窄（后端给的权重键是 `string`）。
 *
 * ⚠️ 用它而不是 `as LegName`：后者会把 `"visal"` 这种拼写错误**静默**变成
 * 一个查不到的值，于是权重列表里出现「visal×3」而没人发现。
 * 这里的 `undefined` 分支会退回显示原字符串（用户看得见问题）。
 */
export function legName(k: string): LegName | undefined {
  return (LEG_ORDER as string[]).includes(k) ? (k as LegName) : undefined;
}

/** 腿的中文名与"它到底在干什么"。 */
export const LEG_LABEL: Record<LegName, string> = {
  dense: "向量（语义）",
  sparse: "BM25（关键词）",
  trgm: "子串（精确片段）",
  visual: "视觉（页面/照片）",
};

/**
 * 腿的**短名**（权重/摘要里用）。
 *
 * 🔑 不能直接用 `LEG_LABEL`：`视觉（页面/照片）×3` 这种括号在权重列表里
 * 读起来像两件事，而它只是一条腿的名字 + 一句解释。
 */
export const LEG_SHORT: Record<LegName, string> = {
  dense: "向量",
  sparse: "BM25",
  trgm: "子串",
  visual: "视觉",
};

/** 一条腿的状态：`ok` 有结果 / `empty` 跑了但没命中 / `skipped` **压根没跑**。 */
export type LegState = "ok" | "empty" | "skipped";

export interface LegView {
  /** 🔑 收窄成 `LegName`：这样页面里 `legs[v.name]` 不用再断言类型，
   *  而字符串字面量拼错（比如 "visal"）会被 tsc 挡住 */
  name: LegName;
  label: string;
  state: LegState;
  n: number;
  ms: number;
  /** skipped 的原因（**如实显示**，D72） */
  reason: string | null;
}

/**
 * 腿的状态判定。
 *
 * 🔑 顺序不能反：先看 `skipped`，再看 `n`。
 * 反过来的话"被跳过的腿"（n=0）会被判成 `empty` ——
 * 而这两者对用户的含义完全相反：
 * - `empty`：这条腿跑了，没匹配上（换查询词也许有用）
 * - `skipped`：这条腿**没跑**（无论怎么改查询词都不会有结果）
 */
export function legState(leg: { n?: number; skipped?: string | null } | undefined): LegState {
  if (!leg) return "empty";
  if (leg.skipped) return "skipped";
  return (leg.n ?? 0) > 0 ? "ok" : "empty";
}

/** 腿的展示对象（按 `LEG_ORDER` 排序，缺的腿补成"empty"）。 */
export function legViews(
  legs: Partial<Record<LegName, { n?: number; ms?: number; skipped?: string | null }>>,
): LegView[] {
  const out: LegView[] = [];
  for (const name of LEG_ORDER) {
    const leg = legs?.[name];
    out.push({
      name,
      label: LEG_LABEL[name] ?? name,
      state: legState(leg),
      n: leg?.n ?? 0,
      ms: leg?.ms ?? 0,
      reason: leg?.skipped ?? null,
    });
  }
  return out;
}

/** 一句话概括"这次检索实际用了几条腿"。 */
export function fusionSummary(views: LegView[]): string {
  const ran = views.filter((v) => v.state !== "skipped");
  const skipped = views.filter((v) => v.state === "skipped");
  const withHits = views.filter((v) => v.state === "ok");
  const bits: string[] = [];
  if (ran.length === 0) return "没有腿参与检索";
  if (ran.length === 1) {
    // 🔑 单腿要说清是"单腿"，别让用户以为是多路融合的结果
    bits.push(`**单腿检索**（${ran[0].label}）—— 少了一条腿的召回`);
  } else {
    bits.push(
      `多路融合 ${ran.length} 条腿（${ran.map((v) => LEG_SHORT[v.name] ?? v.name).join(" + ")}）`,
    );
  }
  if (withHits.length === 0) bits.push("没有腿命中");
  if (skipped.length) {
    bits.push(`跳过 ${skipped.length} 条（${skipped.map((v) => LEG_LABEL[v.name] ?? v.name).join("、")}）`);
  }
  return bits.join(" · ");
}

/**
 * 一条命中被几条腿捞到（0 不可能出现在 `rrf` 里）。
 *
 * 🔑 这是调试面板最该展示的数字：`>= 2` 说明"两条腿都认为它相关"，
 * 只被一条腿捞到的更可能是巧合。
 */
export function hitConsensus(ranks: Partial<Record<string, number>> | undefined): number {
  return Object.keys(ranks ?? {}).length;
}

/** 共识度的一句话解释（不夸大）。 */
export function consensusText(ranks: Partial<Record<string, number>> | undefined): string {
  const n = hitConsensus(ranks);
  if (n === 0) return "";
  if (n === 1) return "仅一条腿捞到（可能是巧合，换个查询词结论会变）";
  return `${n} 条腿共同捞到`;
}

/**
 * RRF 分的展示：**绝不当置信度**。
 *
 * `score = Σ 1/(k + rank)`，量级约 0.03。所以：
 * - 不能显示成百分比（用户会问"为什么最高只有 3%"）
 * - 只能显示成"RRF 分"并说明它是什么
 */
export function rrfScoreText(score: number | undefined): string {
  if (score == null || !Number.isFinite(score)) return "-";
  return score.toFixed(4);
}

/**
 * 分腿耗时排序（慢的那条通常就是瓶颈）。
 *
 * 🔑 只返回**认识的**腿：耗时字典里还有 `total_ms`，而且将来可能加别的字段
 *   （`rerank_ms` 之类）。不做收窄的话页面里 `LEG_LABEL[slow.name]` 就得
 *   到处加 `?? 兜底`，而 `total_ms` 会被当成一条腿显示出来。
 */
export function slowestLeg(
  elapsed: Record<string, number> | undefined,
): { name: LegName; ms: number } | null {
  const rows = Object.entries(elapsed ?? {})
    .map(([k, v]) => ({ k, v }))
    .filter(({ k, v }) => k !== "total_ms" && Number.isFinite(v))
    .map(({ k, v }) => ({ name: legName(k), ms: v }))
    .filter((r): r is { name: LegName; ms: number } => r.name != null)
    .sort((a, b) => b.ms - a.ms);
  return rows[0] ?? null;
}

/** 路由结论的一行（`why` 是 D72 要求的"不能是黑盒"）。 */
export function routeText(
  route: { intent: string; weights: Record<string, number>; why: string } | null | undefined,
): string {
  if (!route) return "未路由（等权 RRF）";
  const w = Object.entries(route.weights ?? {})
    .map(([k, v]) => `${legName(k) ? LEG_SHORT[legName(k)!] : k}×${v}`)
    .join("、");
  const head = `判定「${route.intent}」${w ? ` · 权重 ${w}` : ""}`;
  return route.why ? `${head} · ${route.why}` : head;
}

/** 精排结论（`skipped` 要如实显示，别显示成"没生效"）。 */
export function rerankText(
  rr: { model: string | null; applied: boolean; skipped: string | null } | null | undefined,
): string {
  if (!rr || !rr.model) return "未开启精排（该库没配 rerank_model）";
  if (!rr.applied) return `${rr.model} · 未生效：${rr.skipped ?? "未知原因"}`;
  return `${rr.model} · 已生效`;
}

/** 软删过滤掉的命中数（>0 要说，否则用户以为索引里有幽灵结果）。 */
export function filteredNote(n: number | undefined): string {
  const c = n ?? 0;
  return c > 0
    ? `另有 ${c} 条命中因文档已软删被过滤掉（D7：先删 PG，异步清索引）`
    : "";
}

/** 空结果的原因分类：没跑腿 / 跑了没匹配上 / 有腿被跳过。 */
export function emptyDebugHint(views: LegView[], rrfCount: number): string {
  if (rrfCount > 0) return "";
  // ⚠️ 按「**有没有**腿被跳过」分分支，不是「是不是**全部**被跳过」：
  // 响应里**缺席的腿**（没这个键）判成 `empty`，于是第一版的 `every(skipped)`
  // 在最常见的情况（视觉腿跳过、其余三条没命中）下不成立，
  // 于是落进兜底只显示"没有可用的检索腿" —— 而真相是
  // "三条腿跑了都没匹配上，视觉腿压根没跑"。
  const skipped = views.filter((v) => v.state === "skipped");
  const ran = views.filter((v) => v.state !== "skipped");
  if (ran.length === 0) {
    return "所有检索腿都被跳过了 —— 这是环境问题（视觉编码只在 worker 进程加载，D50），不是「没匹配」";
  }
  const advice = "试试更短的关键词（BM25 腿）或换同义表述（向量腿）";
  if (skipped.length) {
    const why = skipped.map((v) => v.reason ?? LEG_LABEL[v.name] ?? v.name).join("、");
    return `${ran.length} 条腿跑了都没匹配上；另有 ${skipped.length} 条腿被跳过（${why}）。${advice}`;
  }
  return `${ran.length} 条腿跑了都没匹配上。${advice}`;
}