/**
 * per-KB 精排开关的**纯逻辑**（D87/D90 · D125）。
 *
 * 为什么要单独抽出来测：这个开关的语义坑全是"看起来对"的
 * ——`null` 到底是"关"还是"用默认"，直接写在 JSX 里就没人能单测。
 */

/** 后端 `KbOut.rerank_model` → 界面状态。`null` 与 `""` 都算**关闭**。 */
export type RerankState = { on: boolean; model: string | null };

/**
 * 🔑 `null` 与 `""` 都表示**不启用**（D90），**不是**"用全局默认"。
 *
 * ⚠️ 第一版把 `null` 当成"回退到全局默认"，于是**每个库都在跑精排**
 * （实测 `applied=true`、白花 5.2s），而 `/search/debug` 显示的模型名
 * 还是"配置里那个" —— 从界面上完全看不出配置没生效。
 */
export function rerankState(kb: { rerank_model?: string | null } | null): RerankState {
  const raw = kb?.rerank_model ?? null;
  const model = typeof raw === "string" && raw.trim() !== "" ? raw.trim() : null;
  return { on: model !== null, model };
}

/** 界面上要说的话。🔑 关着的时候必须说清"没开"，不能只显示空白。 */
export function rerankText(s: RerankState): string {
  return s.on ? `已开启 · ${s.model}` : "未开启（按库可选，默认关）";
}

/**
 * 点"开启"要发给后端的值。
 *
 * 🔑 空串**不是**"清空"而是"关闭"（后端把 `""` 归一成 `None`），
 * 所以这里返回**默认模型名**而不是 `""`。
 */
export function rerankEnablePayload(defaultModel: string): string {
  return defaultModel.trim();
}

/** 点"关闭"要发给后端的值 —— 空串，后端归一成"不启用"。 */
export const RERANK_DISABLE_PAYLOAD = "";

/**
 * 提醒文案：**开启会改变排序**。
 *
 * 🔑 这一句不能省：精排不是"更准一点"，是**结果会变**。
 * 用户按以前的习惯翻到第 3 条发现不见了，第一反应是"系统坏了"。
 */
export const RERANK_HINT =
  "开启后这个库的搜索结果会**重新排序**（cross-encoder 精排，本机约 5 秒/次查询）。";
