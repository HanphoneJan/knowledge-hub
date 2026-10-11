/**
 * 检索范围（"选哪些库"）的**纯逻辑**。
 *
 * ## 为什么单独抽出来
 *
 * 这是"会静默做错事"的一类：范围解析错了，界面照常渲染，只是搜出来的
 * 东西不对（或者以为在搜 A 库、其实搜的是 B 库）。与 `lib/timeline.ts` /
 * `lib/workspace.ts` 同一思路 —— 抽成纯函数、直接单测。
 *
 * ## 三种状态的区分（🔑 不能混）
 *
 * | 状态 | 含义 | 编码 |
 * |---|---|---|
 * | `null` | **全部有权限的库**（服务端推导，D8） | `"all"` |
 * | `number[]` | 明确选了这几个库 | `"1,2,3"` |
 * | `undefined` | **继承全局**（只有页面级覆盖会出现） | 参数缺省 |
 *
 * ⚠️ `null` 与 `undefined` 是**两件事**：前者是"我就是要搜全部"，
 * 后者是"这个页面没自己设，跟全局走"。搞混的话，页面级覆盖一打开就
 * 会把"全部"当成"没设"而回退，用户改不动。
 *
 * ## 两个存储键
 *
 * - `pkh.kbs`：全局范围（新）。
 * - `pkh.kb`：旧的**单选**键，只读一次用于迁移（老用户升上来不该丢选择）。
 */

import type { Kb } from "../types/api";

/** 生效范围：`null` = 全部有权限的库。 */
export type Scope = number[] | null;

export const SCOPE_STORE_KEY = "pkh.kbs";
/** 旧单选键（D122 之前的 `Layout` 在用）。**只读**，用于迁移。 */
export const LEGACY_KB_KEY = "pkh.kb";

type Store = Pick<Storage, "getItem" | "setItem">;

function validIds(ids: number[]): number[] {
  const seen = new Set<number>();
  const out: number[] = [];
  for (const n of ids) {
    if (Number.isInteger(n) && n > 0 && !seen.has(n)) {
      seen.add(n);
      out.push(n);
    }
  }
  return out;
}

/** 字符串 → 范围。`undefined` 表示"没给"（继承全局）。 */
export function decodeScope(raw: string | null | undefined): Scope | undefined {
  if (raw == null) return undefined;
  const s = raw.trim();
  if (s === "" || s === "all") return null;
  const ids = validIds(s.split(",").map((x) => Number(x.trim())));
  // 解析不出任何有效 id 时当作"全部"，而不是空集 ——
  // 空集会让检索返回 0 条，看起来像"库是空的"。
  return ids.length ? ids : null;
}

/** 范围 → 字符串。`null` 编码成 `"all"`。 */
export function encodeScope(scope: Scope): string {
  return scope === null ? "all" : validIds(scope).join(",");
}

/** 读全局范围；没有新键时回退旧的单选键，再回退"全部"。 */
export function loadStoredScope(store: Store = localStorage): Scope {
  const raw = store.getItem(SCOPE_STORE_KEY);
  if (raw != null) return decodeScope(raw) ?? null;
  const legacy = decodeScope(store.getItem(LEGACY_KB_KEY));
  return legacy === undefined ? null : legacy;
}

export function saveStoredScope(scope: Scope, store: Store = localStorage): void {
  try {
    store.setItem(SCOPE_STORE_KEY, encodeScope(scope));
  } catch {
    /* 隐私模式下写不进就退化成"每次默认全部"，不阻断 */
  }
}

/**
 * 勾选/取消一个库。
 *
 * 🔑 当前是"全部"（`null`）时**必须先展开成全部 id 再取反**，否则
 * "取消勾一个"会退化成"什么都不选"。`allIds` 就是展开用的全集
 * （调用方传 `GET /kb` 的结果）。
 */
export function toggleScope(scope: Scope, id: number, allIds: number[]): Scope {
  const base = scope === null ? validIds(allIds) : validIds(scope);
  const next = base.includes(id) ? base.filter((x) => x !== id) : [...base, id];
  return next.length ? next : null; // 一个都不剩 → 回到"全部"（而不是空集）
}

/** 某个库当前是否被选中（"全部"时视为全选）。 */
export function isSelected(scope: Scope, id: number): boolean {
  return scope === null ? true : scope.includes(id);
}

/** 当前是否等价于"全部有权限的库"。 */
export function isAll(scope: Scope, allIds: number[]): boolean {
  if (scope === null) return true;
  const a = new Set(allIds);
  return scope.length === a.size && scope.every((x) => a.has(x));
}

/**
 * 顶栏那一行的范围文案。
 *
 * ⚠️ "全部"要说清是**几个库**（`全部库（4）`）—— 只写"全部"时用户
 * 不知道到底搜了多少，而这类"范围不明"正是检索类应用最容易让人困惑的点。
 */
export function scopeLabel(scope: Scope, kbs: Kb[]): string {
  const all = kbs.map((k) => k.id);
  if (isAll(scope, all)) return `全部库（${all.length}）`;
  const n = scope?.length ?? 0;
  if (n === 1) {
    const kb = kbs.find((k) => k.id === scope![0]);
    return kb ? kb.name : "1 个库";
  }
  return `${n} 个库`;
}

/**
 * 把范围换算成请求参数。
 *
 * 🔑 "全部"传 `null`（让服务端用 `ctx.readable_kb_ids` 推导，D8）——
 * **不要**把当前看到的 id 列表传上去：那样会漏掉"我有权限但这一页没加载出来"
 * 的库，也会在客户端复刻一遍可见性判定（§0.2 明令禁止）。
 */
export function scopeToKbIds(scope: Scope): number[] | null {
  return scope === null ? null : validIds(scope);
}
