/**
 * `lib/settings.ts` + `lib/sessions.ts` 的单测。
 *
 * 重点是**会静默造成损失或误导**的地方：
 * 明文只此一次、吊销不可撤销、软删不等于不可撤销、空标题不该保存。
 */

import { describe, expect, it } from "vitest";

import {
  defaultKeyLabel,
  isCurrentKey,
  keyPrefix,
  keyState,
  oneTimeNotice,
  revokeConfirmText,
  sortKeys,
} from "./settings";
import {
  deleteConfirmText,
  emptySessionsHint,
  sessionSummary,
  sessionTitle,
  sortSessions,
  validateTitle,
} from "./sessions";
import type { ApiKeyRow, SessionRow } from "../types/api";

const key = (over: Partial<ApiKeyRow> = {}): ApiKeyRow => ({
  id: 1,
  key_prefix: "sk-pkh_ABCD1234",
  // ⚠️ 字段名是 `name`（契约 §1 + CreateKeyReq）—— 我第一版写 `label`，
  //   后端直接 422 `Field required: body.name`。
  name: "我的电脑",
  scopes: ["read", "write"],
  created_at: "2026-10-01T10:00:00+08:00",
  last_used_at: null,
  ...over,
});

// 🔑 `SessionRow` **没有 message_count**（`SessionOut` 里没有这个字段）——
//   第一版我脑补了它，于是界面会显示「undefined 条消息」。
const sess = (over: Partial<SessionRow> = {}): SessionRow => ({
  id: 7,
  kb_id: 1,
  title: "问过的问题",
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
  ...over,
});

describe("keyState", () => {
  it("两态：使用中 / 还没用过", () => {
    expect(keyState({ last_used_at: "2026-10-01" }).state).toBe("active");
    expect(keyState({ last_used_at: null }).state).toBe("unused");
  });

  it("🔑 没有「已吊销」状态 —— 吊销是删行，key 根本不在列表里", () => {
    // 我第一版脑补了 `revoked_at` 并写了三态，于是页面永远走不到那一态，
    // 而照此写的 e2e 断言恒假。后端 `revoke_key` 是 DELETE 掉 ApiKey 行。
    expect(Object.keys(keyState({ last_used_at: null })).sort()).toEqual(["label", "state"]);
  });

  it("🔑 「还没用过」不是「不能用」", () => {
    expect(keyState({ last_used_at: null }).label).toBe("还没用过");
    expect(keyState({ last_used_at: null }).label).not.toContain("失效");
  });
});

describe("吊销确认文案", () => {
  it("不可撤销 + 点明是哪一把", () => {
    const t = revokeConfirmText(key());
    expect(t).toContain("「我的电脑」");
    expect(t).toContain("不能恢复");
    expect(t).toContain("401");
  });

  it("🔑 从没用过的 key 要特别提醒（别吊销掉刚创建的）", () => {
    const t = revokeConfirmText(key({ last_used_at: null }));
    expect(t).toContain("从未被使用过");
  });

  it("用过的不加那句提醒", () => {
    const t = revokeConfirmText(key({ last_used_at: "2026-10-02T09:00:00+08:00" }));
    expect(t).toContain("最后一次被使用");
    expect(t).not.toContain("从未被使用过");
  });

  it("没有 name 时用前缀指认", () => {
    expect(revokeConfirmText(key({ name: "" }))).toContain(keyPrefix(key()));
  });
});

describe("oneTimeNotice", () => {
  it("🔑 必须说清「只此一次」与丢了的后果", () => {
    const t = oneTimeNotice();
    expect(t).toContain("只出现这一次");
    expect(t).toContain("再也查不到");
    expect(t).toContain("重新铸");
  });
});

describe("sortKeys", () => {
  it("使用中的在前（按最近使用），没用过的在后", () => {
    const rows = [
      key({ id: 1, last_used_at: "2026-10-01" }),
      key({ id: 2, last_used_at: null }),
      key({ id: 3, last_used_at: "2026-10-05" }),
    ];
    expect(sortKeys(rows).map((k) => k.id)).toEqual([3, 1, 2]);
  });

  it("同组内按最近使用 / id 倒序（顺序稳定，界面不跳）", () => {
    const rows = [
      key({ id: 1, last_used_at: "2026-10-01" }),
      key({ id: 2, last_used_at: "2026-10-05" }),
    ];
    expect(sortKeys(rows).map((k) => k.id)).toEqual([2, 1]);
  });

  it("不改动入参（sort 的基本要求）", () => {
    const rows = [key({ id: 2 }), key({ id: 1 })];
    const copy = rows.map((r) => r.id);
    sortKeys(rows);
    expect(rows.map((r) => r.id)).toEqual(copy);
  });
});

describe("isCurrentKey / defaultKeyLabel", () => {
  it("按前缀比对（明文只出现一次，之后只能这么比）", () => {
    expect(isCurrentKey(key(), "sk-pkh_ABCD1234")).toBe(true);
    expect(isCurrentKey(key(), "sk-pkh_OTHER")).toBe(false);
    expect(isCurrentKey(key(), null)).toBe(false);
  });

  it("默认 name 带日期（用户不用想名字）", () => {
    expect(defaultKeyLabel()).toMatch(/^我的电脑 \d{4}-\d{2}-\d{2}$/);
  });
});

describe("会话", () => {
  it("标题兜底用 id（不显示空白）", () => {
    expect(sessionTitle({ title: null, id: 3 })).toBe("会话 #3");
    expect(sessionTitle({ title: "   ", id: 3 })).toBe("会话 #3");
    expect(sessionTitle({ title: "好的", id: 3 })).toBe("好的");
  });

  it("🔑 空标题不许保存", () => {
    expect(validateTitle("")).toContain("不能为空");
    expect(validateTitle("   ")).toContain("不能为空");
    expect(validateTitle("x".repeat(201))).toContain("太长");
    expect(validateTitle("好的")).toBeNull();
  });

  it("🔑 删会话是**软删** —— 文案不能说不可撤销，也不能说刷新就回来", () => {
    const t = deleteConfirmText(sess());
    expect(t).toContain("软删除");
    expect(t).toContain("30 天");
    expect(t).not.toContain("不可撤销");
  });

  it("🔑 不编造「N 条消息」（那个字段根本不存在）", () => {
    const t = deleteConfirmText(sess());
    expect(t).not.toContain("undefined");
    expect(t).not.toMatch(/\d+ 条消息/);
    expect(t).toContain("要数得逐个取详情");
  });

  it("按最近更新排序（刚聊的在最上面）", () => {
    const rows = [
      sess({ id: 1, updated_at: "2026-10-01T00:00:00+08:00" }),
      sess({ id: 2, updated_at: "2026-10-05T00:00:00+08:00" }),
    ];
    expect(sortSessions(rows).map((s) => s.id)).toEqual([2, 1]);
  });

  it("🔑 updated_at 为空串时退回 created_at（排序不能因此失效）", () => {
    // ⚠️ `updated_at` 在类型上是非空 string，但真实数据可能缺 ——
    //   所以排序要能退回 created_at，而不是让 undefined 参与比较。
    const rows = [
      sess({ id: 1, updated_at: "", created_at: "2026-10-01T00:00:00+08:00" }),
      sess({ id: 2, updated_at: "", created_at: "2026-10-09T00:00:00+08:00" }),
    ];
    expect(sortSessions(rows).map((s) => s.id)).toEqual([2, 1]);
  });

  it("摘要只有时间；时间未知要如实说", () => {
    expect(sessionSummary(sess())).toContain("最后活动");
    expect(sessionSummary(sess())).not.toContain("undefined");
    expect(sessionSummary(sess({ updated_at: "", created_at: "" }))).toContain(
      "时间未知",
    );
  });

  it("「没有会话」与「加载失败」要区分", () => {
    expect(emptySessionsHint(null)).toContain("问答");
    expect(emptySessionsHint({ status: 500 })).toContain("加载失败");
  });
});