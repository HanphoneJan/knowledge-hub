/**
 * 切分策略（D164）的**前端纯逻辑**。
 *
 * 后端那一侧的唯一真相是 `services/ingest/strategies.py` 的 `STRATEGY_NAMES` /
 * `STRATEGIES`（注册表 + 实现），并由守卫测试钉住与 `common.schemas.ChunkStrategy`
 * 相等。这里只是它的**投影**：label / 说明 / "换过去要注意什么"。
 *
 * ## 为什么要单独一个模块（而不是写在 JSX 里）
 *
 * 这些判定的出错方式全是**静默**的：
 * - 默认值落错（`undefined` 被当成"没设"而回退到别的策略）；
 * - `contextual` 在**没配 LLM** 时是**降级**跑的，界面上不说就等于骗人；
 * - 改策略**不回溯**（只对之后摄取的文档生效）—— 不说清，用户会以为改完就生效了。
 * ⇒ 判定做成纯函数 + 单测（`strategy.test.ts`）。
 */
import type { ChunkStrategy, Kb } from "../../types/api";

/** 展示顺序（与后端注册表的语义分组一致：从最简单到最重）。 */
export const STRATEGY_ORDER: readonly ChunkStrategy[] = [
  "fixed",
  "recursive",
  "semantic",
  "parent_child",
  "late",
  "contextual",
] as const;

/** 后端 `strategies.DEFAULT_STRATEGY`（改它等于改基线）。 */
export const DEFAULT_STRATEGY: ChunkStrategy = "recursive";

export interface StrategyMeta {
  key: ChunkStrategy;
  label: string;
  /** 一句话：边界怎么切。 */
  boundary: string;
  /** 一句话：编码/召回上的区别。 */
  effect: string;
  /** 需要外部大模型（没配就**降级**，不是失败）。 */
  needsLlm: boolean;
  /** 用 Late 编码（整篇前向、按 span 池化）—— 摄取更贵。 */
  lateEncoding: boolean;
  /** PDF 会**保持结构切分**（`semantic` 会重排文字 ⇒ 破坏 L3 坐标）。 */
  pdfStructuralOnly: boolean;
}

export const STRATEGY_META: Record<ChunkStrategy, StrategyMeta> = {
  fixed: {
    key: "fixed",
    label: "定长（fixed）",
    boundary: "按 chunk_size 定长切 + overlap",
    effect: "评测基线，最可预测；句子会被从中间截断",
    needsLlm: false,
    lateEncoding: false,
    pdfStructuralOnly: false,
  },
  recursive: {
    key: "recursive",
    label: "递归（recursive，默认）",
    boundary: "标题 > 段落 > 硬切",
    effect: "现行为（默认）；边界尽量落在自然段上",
    needsLlm: false,
    lateEncoding: false,
    pdfStructuralOnly: false,
  },
  semantic: {
    key: "semantic",
    label: "语义（semantic）",
    boundary: "相邻句子的向量相似度低于阈值才断",
    effect: "话题不被切散；⚠️ PDF 会保持结构切分",
    needsLlm: false,
    lateEncoding: false,
    pdfStructuralOnly: true,
  },
  parent_child: {
    key: "parent_child",
    label: "父子块（parent_child）",
    boundary: "子块 = 递归块，父块 = 章节/页",
    effect: "召回小子块、给模型的上下文用父块（父块 ≤2000 字）",
    needsLlm: false,
    lateEncoding: false,
    pdfStructuralOnly: false,
  },
  late: {
    key: "late",
    label: "Late Chunking（late）",
    boundary: "与递归同边界",
    effect: "整篇前向编码、按 span 池化 —— **摄取更慢**",
    needsLlm: false,
    lateEncoding: true,
    pdfStructuralOnly: false,
  },
  contextual: {
    key: "contextual",
    label: "上下文增强（contextual）",
    boundary: "与递归同边界",
    effect: "每块前面加一段 LLM 生成的上下文；**没配外部大模型会降级成递归**",
    needsLlm: true,
    lateEncoding: false,
    pdfStructuralOnly: false,
  },
};

/**
 * 库的当前策略。
 *
 * ⚠️ 契约里 `KbOut.chunk_strategy` **总是**有值（默认 `recursive`），但
 * `src/types/api.ts` 把字段写成可选（为了不动冻结的 `Kb` 夹具）⇒ 默认值
 * **只在这一个地方**落地，别在组件里各写各的 `?? "recursive"`。
 * 非法值也回落到默认（后端有 CHECK 约束，理论上不会出现；出现了也不该白屏）。
 */
export function strategyOf(kb: Pick<Kb, "chunk_strategy"> | null | undefined): ChunkStrategy {
  const raw = kb?.chunk_strategy;
  return isStrategy(raw) ? raw : DEFAULT_STRATEGY;
}

export function isStrategy(v: unknown): v is ChunkStrategy {
  return typeof v === "string" && (STRATEGY_ORDER as readonly string[]).includes(v);
}

export function strategyLabel(k: ChunkStrategy): string {
  return STRATEGY_META[k]?.label ?? k;
}

/** 下拉用的选项（顺序固定，默认项在第二位 —— 与后端注册表同序）。 */
export function strategyOptions(): Array<{ value: ChunkStrategy; label: string }> {
  return STRATEGY_ORDER.map((k) => ({ value: k, label: strategyLabel(k) }));
}

/**
 * 换策略时**必须**告诉用户的话。
 *
 * 🔑 三条缺一不可：
 * 1. **不回溯** —— 改完只对之后摄取的文档生效（`PATCH` 不自动重跑，D164 的刻意设计）；
 * 2. **怎么让它生效** —— 对已有文档点「重新解析」；
 * 3. **代价/前提** —— `contextual` 没配 LLM 会降级；`late` 更慢；`semantic` 对 PDF 无效。
 *
 * @param to 目标策略
 * @param opts.llmActive 外部大模型当前是否**真的生效**（`GET /admin/llm` 的
 *   `external_active`，后端算好的，前端不重算）
 */
export function strategyChangeNotice(
  to: ChunkStrategy,
  opts: { llmActive: boolean } = { llmActive: true },
): string[] {
  const meta = STRATEGY_META[to];
  const out: string[] = [
    "改策略**只对之后重新摄取的文档生效** —— 已经入库的片段不会自动重切。",
    "要让已有文档用上新策略，去「文档」tab 对它们点「重新解析」（可以在下面一次重解析全部）。",
  ];
  if (meta?.needsLlm && !opts.llmActive) {
    out.push(
      "⚠️ 这个策略需要外部大模型，而当前**没有生效的 LLM 配置** —— 摄取时会**降级**回递归切分" +
        "（不会失败，但你也拿不到上下文增强的效果）。先去「设置 → 外部大模型」配好再来。",
    );
  }
  if (meta?.lateEncoding) {
    out.push("⚠️ Late Chunking 的编码方式不同：整篇前向 + 按 span 池化，**摄取明显更慢**。");
  }
  if (meta?.pdfStructuralOnly) {
    out.push("⚠️ 对 PDF 不会执行语义切分（会重排文字、破坏引用坐标）—— 那些文档仍按结构切。");
  }
  return out;
}

/** 「一次重解析全部文档」按钮该不该可点，以及该说什么。 */
export function reparseAllPlan(docCount: number): { enabled: boolean; text: string } {
  if (docCount <= 0) return { enabled: false, text: "这个库还没有文档" };
  return {
    enabled: true,
    text: `重新解析全部 ${docCount} 份文档（会逐份重新入队，可能几分钟；期间可以离开这个页面）`,
  };
}
