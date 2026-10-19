/**
 * 工作区切换与成员/邀请码交互的**纯逻辑**（D120 · D122）。
 *
 * 与 `lib/pii.ts` 同一思路：把"会静默做错事"的地方从组件里抽出来测。
 *
 * ## 这里的四个坑
 *
 * 1. 🔴 **切换工作区不能留着别的库的选中态**。
 *    前端做了"选中的库没了就落到第一个"的兜底，但那只在库**消失**时触发；
 *    切到另一个工作区时当前库**还在**（可见性是全局的），
 *    于是会出现"我在工作区 B、顶栏选中的却是工作区 A 的库"——
 *    之后所有 `?kb=` 链接都带着一个不属于当前上下文的 id。
 * 2. ⚠️ **邀请码明文只出现一次**（D120，与 API Key 同一纪律）：
 *    必须当场显示 + 可复制，且不能进 localStorage。
 * 3. 🔑 **兑换失败是一句话**（D120）：界面**不能替后端猜原因**。
 *    "码无效（可能已过期…）" 传上来就照原样显示；
 *    自己改写成"邀请码已过期"就是把猜测说成了事实。
 * 4. ⚠️ **移除成员不可撤销**（要对方再自己加回来）：确认文案要说清。
 */

import type { InviteRow, Kb, MemberRow, Workspace } from "../types/api";
import type { WorkspaceStats } from "../types/api";

/** 顶栏记住的"当前工作区"（`null` = 全部）。 */
export const WS_KEY = "pkh.ws";

/**
 * 按工作区过滤库。
 *
 * ⚠️ `wsId === null` → **全部**（不是空）：默认不筛，
 * 这样单工作区的用户（绝大多数）看不到任何多余控件的语义。
 */
export function filterKbsByWs(kbs: Kb[], wsId: number | null): Kb[] {
  return wsId === null ? kbs : kbs.filter((k) => k.workspace_id === wsId);
}

/**
 * 切换工作区后"选中的库"应该是哪个。
 *
 * 🔑 **必须落回新工作区内的库**（坑 1）。找不到就返回 `null` ——
 * 让顶栏显示"（这个工作区还没有库）"，
 * 好过继续带着一个别的上下文的 id 去请求。
 */
export function kbAfterWsSwitch(
  kbs: Kb[],
  wsId: number | null,
  currentKbId: number | null,
): number | null {
  const visible = filterKbsByWs(kbs, wsId);
  // 当前库**本来就在**可见范围内 → 保持不变（切到"全部"时的常见情况）
  if (currentKbId !== null && visible.some((k) => k.id === currentKbId)) {
    return currentKbId;
  }
  return visible.length ? visible[0].id : null;
}

/**
 * 工作区切换器里那一项「全部工作区」的文案。
 *
 * ⚠️ **两个数字都要写，且都写清单位**：只写「2 个」时用户分不清是
 * 2 个工作区还是 2 个库（第一版就只写了个裸数字，于是测试与实现各猜各的）。
 */
export function allWsLabel(kbs: Kb[]): string {
  const wsN = new Set(kbs.map((k) => k.workspace_id)).size;
  return `全部工作区（${wsN} 个工作区 · ${kbs.length} 个库）`;
}

/** 工作区下拉里每个选项的名字（带库数，空工作区也要看得见）。 */
export function wsOptions(
  wss: Workspace[],
  kbs: Kb[],
): { id: number; name: string; kb_count: number }[] {
  return wss.map((w) => ({
    id: w.id,
    name: w.name,
    kb_count: kbs.filter((k) => k.workspace_id === w.id).length,
  }));
}

// ============================================================
// 邀请码（D120）
// ============================================================

/** 明文只此一次的展示文案（生成后立刻显示，不能一闪而过）。 */
export function inviteOneTimeNotice(inv: InviteRow, code: string): string {
  return [
    `🔑 这段码**只显示这一次**（库里只存哈希，列表里只看得到前 ${inv.code_prefix.length} 位）。`,
    "",
    `    ${code}`,
    "",
    `默认 ${inv.expiry_text}、可用 ${inv.max_uses} 次。`,
    "把它发给对方（微信 / 邮件 / 当面都行），对方注册后自己输码加入。",
    "⚠️ 丢了只能重新生成一段 —— 这一段**找不回来**。",
  ].join("\n");
}

/** 复制按钮的反馈（剪贴板失败也要说，不能静默）。 */
export function copyFeedback(ok: boolean): string {
  return ok ? "已复制，去发给对方吧。" : "⚠️ 复制失败，请手动选中上面那串字符。";
}

/** 列表里一行的"还能用吗"。⚠️ 列表**可以**区分状态（兑换时才统一一句话）。 */
export function inviteUsable(inv: InviteRow): boolean {
  return inv.state === "可用";
}

/** 撤销确认：撤销的是"还没用掉的那份使用权"。 */
export function revokeInviteText(inv: InviteRow): string {
  const used = inv.used > 0;
  return [
    `撤销邀请码 ${inv.code_prefix}…？`,
    "",
    used
      ? `它已经被人用掉了 ${inv.used} 次 —— **已经加入的成员不受影响**，只是不能再用它进来。`
      : "它还没有人用过。撤销后对方就换不了码了。",
  ].join("\n");
}

/** 兑换结果文案（🔑 原样用后端的话，不自己猜原因）。 */
export function redeemResultText(r: {
  already_member: boolean;
  message: string;
}): string {
  return r.already_member
    ? `ℹ️ ${r.message}`
    : `✅ ${r.message}`;
}

/**
 * 加成员前的**前置提醒**。
 *
 * ⚠️ `POST /members` 只能加**已注册**的邮箱 —— 对方没注册就直接 400。
 *   而"邀请还没注册的人"要走**邀请码**。这句话必须在表单旁边，
 *   否则用户会以为是系统坏了。
 */
export function addMemberHint(): string {
  return [
    "按邮箱添加（对方需要**已经注册**）。",
    "⚠️ 对方还没注册？用下面的**邀请码** —— 他注册后自己输码就能加入。",
  ].join(" ");
}

/** 移除成员的确认文案（不可撤销：加回来要再发一次）。 */
export function removeMemberText(m: MemberRow, selfEmail: string | null): string {
  const who = m.display_name || m.email;
  const isSelf = selfEmail !== null && m.email === selfEmail;
  return [
    `把 ${who}（${m.email}）移出工作区？`,
    "",
    isSelf
      ? "⚠️ 这是**你自己** —— 移出后你就看不到这个工作区了（除非你是 owner）。"
      : "对方会立刻看不到这个工作区下的所有知识库。",
    "这个操作不能撤销：想加回来得再发一次邀请或再添一次邮箱。",
  ].join("\n");
}

/** 只有 owner 能做管理动作（其余一律隐藏按钮，不是禁用）。 */
export function canManage(role: string): boolean {
  return role === "owner";
}

/**
 * 工作区用量的**文案**（`GET /workspaces/{id}/stats`，**只有 owner/admin 能看**）。
 *
 * 🔑 显示**两个**字节数（契约 §9 专门解释过它们量的不是同一个东西）：
 * 原始文件之和 vs blob 目录实际占盘（含 `preview.pdf`、`thumbs/*.jpg`）。
 * 只显示一个，用户会以为"删掉这个库能腾出这么多空间"——
 * 而派生文件在文档删掉之后还在（按 sha 去重，一份内容多个库引用只占一份）。
 */
// 🔑 类型**只在这里定义一次**（`types/api.ts` 是契约的投影）；
//   在本文件再写一份 interface 的话，两边会慢慢差字段 —— 而后端少给一个字段
//   时 TypeScript 不会报错，症状是界面显示 "undefined"。
export type { WorkspaceStats };

/** 字节 → 可读体积（三档）。⚠️ 单测直接断言它，所以别改口径。 */
export function bytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 统计读不到时的提示。⚠️ 不能显示 "0 B" —— 那会把"没读到"说成"没用"。 */
export function usageText(s: WorkspaceStats | null | undefined): string {
  if (!s) return "用量读不到";
  return `${s.kb_count} 个库 · ${s.doc_count} 份文档 · ${s.chunk_count} 个片段`;
}

/** 磁盘那一行的两个数字（原始 vs 含派生物）。 */
export function diskText(s: WorkspaceStats | null | undefined): string {
  if (!s) return "磁盘用量读不到";
  return `原始 ${bytes(s.sum_doc_size_bytes)} · 占盘 ${bytes(s.blob_disk_bytes)}（含预览与缩略图）`;
}

/** 只有 owner/admin 能看 —— 别的角色要**说清为什么看不到**，别显示 0。 */
export function usageScopeHint(): string {
  return "只有工作区 owner 能看完整用量（含别人的私有库，磁盘账要全）";
}
