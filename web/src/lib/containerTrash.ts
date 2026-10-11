/**
 * 库 / 工作区回收站的**纯逻辑**（能不能恢复、为什么不能、文案）。
 *
 * 与 `lib/trash.ts` 同一个思路：会静默出错的地方从组件里抽出来。
 * 🔑 **到期天数不重写**：直接复用 `trash.ts::daysLeft / expiryLabel`
 * —— 同一个 30 天窗口有两份算法的话，两边会慢慢差 1 天，
 * 而用户看到的正是那个数字（§5.109 的形状）。
 *
 * ## 这件事真正的难点不是"列出回收站"，而是"别让用户点出一个 403"
 *
 * 库与工作区的恢复有一个**顺序依赖**：
 * 工作区软删 → 它的库恢复不了（成员身份来自未软删的成员关系）→
 * 必须先恢复工作区。而后端 `GET /kb/trash` 干脆**不列**这种库，
 * 所以界面上的"看不到"是对的；真正要处理的是**看得见但过期**的那一类：
 * 30 天到了之后数据随时会被 `KIND_CLEANUP` 清掉，这时候还给一个
 * 能点的按钮就是在骗人。
 */

import { daysLeft, expiryLabel } from "./trash";

export type TrashKind = "workspace" | "kb";

export interface TrashEntry {
  id: number;
  name: string;
  deleted_at?: string | null;
  /** 只有库有：它属于哪个工作区（用来判断"工作区已删 → 先恢复工作区"）。 */
  workspace_id?: number;
}

/** 回收站里一条东西的**状态**。三态，不是两态。 */
export type RestoreState =
  | { kind: "restore"; label: string; hint: string }
  | { kind: "expired"; label: string; hint: string }
  | { kind: "blocked"; label: string; hint: string };

/**
 * 这一条**现在能不能恢复**，以及该显示什么。
 *
 * ⚠️ 三态都要能出现：
 * - `restore` 正常
 * - `expired` 过了 30 天（随时会被自动清理）—— **不给能点的按钮**
 * - `blocked` 它在**已软删的工作区**里 —— 先恢复工作区
 *
 * @param deletedWsIds 当前回收站里的工作区 id 集合（用来判 blocked）
 * @param now 注入是为了可测（`Date.now()` 直接写就没法单测时间边界）
 */
export function restoreState(
  entry: TrashEntry,
  kind: TrashKind,
  deletedWsIds: readonly number[],
  now = Date.now(),
): RestoreState {
  const label = kind === "workspace" ? "恢复工作区" : "恢复知识库";
  const left = daysLeft(entry.deleted_at, now);

  if (kind === "kb" && entry.workspace_id != null
      && deletedWsIds.includes(entry.workspace_id)) {
    // ⚠️ 顺序依赖：成员身份来自未软删的成员关系，工作区一软删就恢复不了这个库。
    //   说清"先恢复工作区"比让用户点一下拿 403 好（D125 的同一堵墙）。
    return {
      kind: "blocked",
      label: "先恢复工作区",
      hint: "它所在的工作区在回收站里，要先恢复那个工作区才能恢复这个库",
    };
  }

  if (left <= 0) {
    return {
      kind: "expired",
      label: "已过期",
      hint: `${expiryLabel(entry.deleted_at, now)} —— 恢复不回来了`,
    };
  }

  return { kind: "restore", label, hint: expiryLabel(entry.deleted_at, now) };
}

/** 这个状态下按钮该不该可点。`blocked`/`expired` 都是"看得见但不能点"。 */
export function canRestore(s: RestoreState): boolean {
  return s.kind === "restore";
}

/**
 * 回收站为空时的提示。
 *
 * ⚠️ 不要写"没有删除任何东西"—— 那听起来像在指责用户；
 * "回收站是空的"是状态，"你删错了什么"是评价。
 */
export function emptyHint(): string {
  return "回收站是空的。删除的知识库与工作区会在这里保留 30 天。";
}

/**
 * 恢复**成功**之后要提示什么。
 *
 * 🔑 库恢复会**连带重新摄取**那些文档（D44：旧向量只留 3 天），
 * 所以"恢复好了"这句话必须说清"文档要重新处理一会儿" ——
 * 否则用户刷新一下发现文档不在列表里，以为恢复失败了又去点一次。
 */
export function restoredHint(kind: TrashKind): string {
  return kind === "workspace"
    ? "工作区已恢复"
    : "知识库已恢复，其文档正在重新处理（需要一会儿才可搜索）";
}