/**
 * 「重摄取欠账」的展示文案（D181）。
 *
 * 后端 `GET /kb/{id}` 的 `stale_strategy_docs` = 还停在**旧切分策略**的当前文档数
 * （文档摄取时记在 `documents.quality.strategy` 的策略 ≠ 该库当前 `chunk_strategy`）。
 * 改策略只对**之后**重新摄取的文档生效，所以这个数字就是"要对多少份点重新解析"。
 *
 * 🔑 纯函数：判据与文案与渲染分开，才能用 `npm test` 覆盖（对齐后端 §5.92 的纪律）。
 * 返回 `null` 表示**不显示**（0 / 缺失都不该弹提示 —— 干净是常态）。
 */
export function staleStrategyHint(n: number | null | undefined): string | null {
  const count = typeof n === "number" && Number.isFinite(n) ? n : 0;
  if (count <= 0) return null;
  return (
    `${count} 份文档仍在旧的切分策略下（改策略只对之后重新摄取的文档生效）` +
    `—— 对它们「重新解析」即可统一。`
  );
}
