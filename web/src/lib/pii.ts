/**
 * PII 掩码开关与报告的**纯逻辑**（D121 · 契约 §3.1）。
 *
 * 与 `lib/settings.ts` 同一思路：把"会静默造成损失的地方"从组件里抽出来。
 *
 * ## 页面最容易出错的四处
 *
 * 1. 🔴 **开关的代价必须说在前面**：掩码让"搜我自己的手机号"失效，
 *    而个人知识库的核心用途恰恰是这个。所以默认关闭，
 *    打开前要说清"已有文档不会被回溯改动"。
 * 2. 🔑 **`source_*` 与 `indexed_*` 是两套数字**：
 *    开了掩码就是 `原文 7 / 索引 0`。合成一个会让用户以为掩码没生效，
 *    然后去反复排查一个根本没坏的开关。
 * 3. ⚠️ **`docs_not_scanned` ≠ "干净"**：那是"没检测过"（未摄取 / 纯扫描件），
 *    藏进"0 处"里就是把"没查"说成"没问题"。
 * 4. ⚠️ **截断必须显示**：`docs_truncated` / `scan_truncated` 都要说出来 ——
 *    一个"看起来完整其实只扫了 1%"的数字比截断提示危险得多。
 */

import type { PiiReport } from "../types/api";

/** 类型名 → 人话（前端只认接口给的键，不自己编）。 */


/** 逐码位比较：**不碰 locale** —— 排序结果必须与运行环境无关（CI 是 `LANG=C`）。 */
function cmpAsc(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export const PII_TYPE_LABEL: Record<string, string> = {
  id_card: "身份证",
  phone: "手机号",
  email: "邮箱",
  student_id: "学号",
  bank_card: "银行卡",
};

export function typeLabel(t: string): string {
  return PII_TYPE_LABEL[t] ?? t;
}

/** 按类型计数 → 一行行中文（按总数降序，条数多的在前）。 */
export function typeRows(byType: Record<string, number>): {
  type: string;
  label: string;
  n: number;
}[] {
  return Object.entries(byType ?? {})
    .map(([type, n]) => ({ type, label: typeLabel(type), n }))
    .sort((a, b) => b.n - a.n || cmpAsc(a.type, b.type));
}

/**
 * 开关的**当前状态**。
 *
 * ⚠️ `backfill_needed` = "开了掩码，但索引里还剩着原文" ——
 *   这是用户唯一能看出"开关没回溯历史"的信号，必须能算出来。
 */
export function maskState(
  rep: PiiReport | null,
  kbMask?: boolean,
): { masking: "on" | "off"; backfill_needed: boolean } {
  // ⚠️ `kbMask` 是 **boolean**、报告里的 `masking` 是 **字符串**。
  //   第一版写成 `(kbMask ?? rep?.masking) === "on"` —— 于是传 `true` 时
  //   `true === "on"` 为假，开关明明打开了、页面却说「未开」，
  //   而 `backfill_needed` 跟着恒为 false，回填提示整条消失。
  //   （单测就是钉这一条的：两个方向都试。）
  const masking: "on" | "off" =
    kbMask === undefined ? (rep?.masking === "on" ? "on" : "off") : kbMask ? "on" : "off";
  return {
    masking,
    // ⚠️ 只有"开着"且"索引里还剩"才算待回填；关着时索引里剩多少都不算问题
    backfill_needed: masking === "on" && (rep?.indexed_total ?? 0) > 0,
  };
}

/**
 * 开关确认文案（打开时用；关闭时用 {@link maskOffConfirmText}）。
 *
 * 🔑 三句话缺一不可：① 代价（搜不到自己的号码）② 不回溯 ③ 原件仍在。
 * 少任何一句，用户都会在"搜不到自己数据"之后才发现。
 */
export function maskOnConfirmText(rep: PiiReport | null): string {
  const backfill = rep?.docs_with_pii ?? 0;
  return [
    "打开 PII 掩码？",
    "",
    "🔴 之后摄取/重新摄取的文档，手机号、身份证、邮箱等会**在索引里被打码**：",
    "   你就**搜不到自己的这些号码**了（原文仍在文件里，检索的是打码后的副本）。",
    "",
    `⚠️ 开关**不回溯**已有文档：本库当前 ${rep?.doc_count ?? 0} 份文档里，`,
    `   有 ${backfill} 份检出过敏感信息 —— 它们需要逐份「重新摄取」才会被掩。`,
    "",
    "打开后可以随时用这里的报告看「原文几处 / 索引还剩几处」。",
  ].join("\n");
}

/** 关闭确认：关掉不会把已打码的内容还原。 */
export function maskOffConfirmText(): string {
  return [
    "关闭 PII 掩码？",
    "",
    "之后摄取的新文档不再打码。",
    "⚠️ 已经打码的 chunk **不会自动还原** —— 那些文档需要「重新摄取」才能变回可搜原文。",
  ].join("\n");
}

/**
 * 报告顶部那句话。
 *
 * ⚠️ 「索引还剩 0」时**不能**说成"这个库没有敏感信息" ——
 *   原文里可能有（`source_total > 0`），只是索引里没有。
 */
export function reportHeadline(rep: PiiReport): string {
  const { masking, backfill_needed } = maskState(rep);
  const bits = [
    `原文检出 ${rep.source_total} 处`,
    `索引里还剩 ${rep.indexed_total} 处`,
  ];
  let text = `${bits.join(" · ")}`;
  if (masking === "on") {
    // ⚠️ 这里**不用 `**` 粗体标记**：这句是渲染在 `<strong>` 里的，
    //   而页面没有 markdown 渲染器 —— `**` 会原样显示成两个星号。
    text += backfill_needed
      ? "（掩码已开，但有文档还没重新摄取）"
      : "（掩码已开，新摄取的内容不再进原文）";
  } else {
    text += "（掩码**未开**，原文直接进索引）";
  }
  return text;
}

/** ⚠️ 必须在页面上显示的提示（不能省；省了就是"数字骗人"）。 */
export function reportWarnings(rep: PiiReport): string[] {
  const out: string[] = [];
  if ((rep.docs_no_text ?? 0) > 0) {
    // 🔴 这条最容易被读反：照片/扫描件贡献的 0 **不是**"没有敏感信息"，
    //   而是"没有文本可检测"。图片里的身份证照、票据要靠 OCR，
    //   而 OCR 不在这个功能的范围内（D121）。
    out.push(
      `${rep.docs_no_text} 份文档**没有文本层**（照片 / 纯扫描件）—— ` +
        "它们没被检测，上面数字里的 0 **不代表图片里没有敏感信息**" +
        "（要看图里的字需要 OCR，不在本功能范围内）。",
    );
  }
  if (rep.docs_not_scanned > 0) {
    out.push(
      `${rep.docs_not_scanned} 份文档**没检测过**（还没摄取完，或纯扫描件没有文本）—— ` +
        "它们既没有掩码，也不在下面的数字里。",
    );
  }
  if (rep.docs_truncated) {
    out.push(
      `明细只列了前 ${rep.docs.length} 份（按命中数排序）；` +
        `上面的「原文检出 ${rep.source_total} 处」是**全量**，不受这里截断影响。`,
    );
  }
  if (rep.scan_truncated) {
    out.push(
      `索引扫描读到 ${rep.scan_chars.toLocaleString()} 字符就达上限了，` +
        "所以「索引里还剩」是**下限**，实际可能更多。",
    );
  }
  if (rep.docs_with_pii === 0 && rep.source_total === 0 && rep.doc_count > 0) {
    out.push("已检测的文档里没有检出敏感信息。");
  }
  return out;
}

/** 报告里逐份明细的行（含"哪些类型"）。 */
export function docRows(rep: PiiReport): {
  doc_id: number;
  file_name: string;
  total: number;
  kinds: string;
  status: string;
}[] {
  return rep.docs.map((d) => ({
    doc_id: d.doc_id,
    file_name: d.file_name,
    total: d.total,
    kinds: typeRows(d.by_type)
      .map((r) => `${r.label} ${r.n}`)
      .join("、"),
    status: d.status,
  }));
}