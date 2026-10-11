import { describe, expect, it } from "vitest";

import {
  addMemberHint,
  allWsLabel,
  canManage,
  copyFeedback,
  filterKbsByWs,
  inviteOneTimeNotice,
  inviteUsable,
  kbAfterWsSwitch,
  redeemResultText,
  removeMemberText,
  revokeInviteText,
  wsOptions,
  bytes,
  diskText,
  usageScopeHint,
  usageText,
} from "./workspace";
import type { InviteRow, Kb, MemberRow, Workspace } from "../types/api";

function kb(id: number, ws: number, name = `库${id}`): Kb {
  return {
    id,
    workspace_id: ws,
    name,
    description: null,
    visibility: "private",
    source_kind: "file",
    embed_model: "bge-m3",
    chunk_size: 500,
    chunk_overlap: 50,
    generate_image_desc: false,
    pii_mask: false,
    // D125：per-KB 精排。null = 未启用（D90）。
    rerank_model: null,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function ws(id: number, name: string): Workspace {
  return { id, name, description: null, owner_id: 1, created_at: "2026-01-01T00:00:00Z" };
}

function inv(over: Partial<InviteRow> = {}): InviteRow {
  return {
    id: 1,
    workspace_id: 7,
    code_prefix: "ABCD",
    role: "member",
    max_uses: 1,
    used: 0,
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-08T00:00:00Z",
    revoked_at: null,
    expiry_text: "7 天后到期",
    state: "可用",
    ...over,
  };
}

describe("filterKbsByWs", () => {
  const kbs = [kb(1, 10), kb(2, 20), kb(3, 10)];

  it("按工作区过滤", () => {
    expect(filterKbsByWs(kbs, 10).map((k) => k.id)).toEqual([1, 3]);
  });

  // ⚠️ null = 全部，不是空：默认不筛
  it("null → 全部", () => {
    expect(filterKbsByWs(kbs, null)).toHaveLength(3);
  });

  it("空工作区 → 空数组", () => {
    expect(filterKbsByWs(kbs, 999)).toEqual([]);
  });
});

describe("kbAfterWsSwitch", () => {
  const kbs = [kb(1, 10), kb(2, 20), kb(3, 10)];

  // 🔑 坑 1：切到别的工作区却留着旧库的 id
  it("🔑 切到别的工作区时**落回该工作区的库**", () => {
    expect(kbAfterWsSwitch(kbs, 20, 1)).toBe(2);
    expect(kbAfterWsSwitch(kbs, 10, 2)).toBe(1);
  });

  it("目标工作区没有库 → null（不是带着别的上下文的 id）", () => {
    expect(kbAfterWsSwitch(kbs, 999, 1)).toBeNull();
  });

  it("切到「全部」且当前库可见 → 保持不变", () => {
    expect(kbAfterWsSwitch(kbs, null, 3)).toBe(3);
  });

  it("当前库已消失 → 落到第一个可见的", () => {
    expect(kbAfterWsSwitch(kbs, 10, 2)).toBe(1);
  });

  it("原本就没选 → 落到第一个可见的", () => {
    expect(kbAfterWsSwitch(kbs, 10, null)).toBe(1);
  });

  it("一个库都没有 → null", () => {
    expect(kbAfterWsSwitch([], null, 5)).toBeNull();
  });
});

describe("allWsLabel / wsOptions", () => {
  it("两个数字都带单位（分不清是工作区还是库）", () => {
    expect(allWsLabel([kb(1, 10), kb(2, 20)])).toBe("全部工作区（2 个工作区 · 2 个库）");
  });

  it("🔑 两个数字都是**全部**的（不随筛选变）", () => {
    // ⚠️ 曾经写成"当前工作区的库数"，于是切到"全部"后文案突然变小
    expect(allWsLabel([kb(1, 10), kb(2, 20)])).toContain("2 个工作区");
  });

  it("单工作区时不啰嗦", () => {
    expect(allWsLabel([kb(1, 10), kb(3, 10)])).toBe("全部工作区（1 个工作区 · 2 个库）");
  });

  it("选项带库数，空工作区也显示 0 而不是消失", () => {
    const rows = wsOptions([ws(10, "甲"), ws(20, "乙")], [kb(1, 10), kb(2, 10)]);
    expect(rows).toEqual([
      { id: 10, name: "甲", kb_count: 2 },
      { id: 20, name: "乙", kb_count: 0 },
    ]);
  });
});

describe("inviteOneTimeNotice", () => {
  it("🔑 说清「只显示这一次」与找不回来", () => {
    const t = inviteOneTimeNotice(inv(), "ABCD2345");
    expect(t).toContain("只显示这一次");
    expect(t).toContain("ABCD2345");
    expect(t).toContain("找不回来");
  });

  it("带上有效期与次数（用户要凭这些判断能不能用）", () => {
    const t = inviteOneTimeNotice(inv({ max_uses: 3, expiry_text: "3 天后到期" }), "ABCD2345");
    expect(t).toContain("3 天后到期");
    expect(t).toContain("3 次");
  });

  it("⚠️ 不含明文之外的额外承诺（比如「一定能加入」）", () => {
    const t = inviteOneTimeNotice(inv(), "ABCD2345");
    expect(t).not.toContain("永久有效");
  });
});

describe("copyFeedback", () => {
  it("成功给鼓励", () => {
    expect(copyFeedback(true)).toContain("已复制");
  });

  // ⚠️ 剪贴板在非 https 下常失败；静默失败 = 用户以为复制了
  it("🔑 失败要说出来", () => {
    expect(copyFeedback(false)).toContain("手动");
  });
});

describe("inviteUsable", () => {
  it("只有「可用」算可用", () => {
    expect(inviteUsable(inv())).toBe(true);
    expect(inviteUsable(inv({ state: "已过期" }))).toBe(false);
    expect(inviteUsable(inv({ state: "已用完" }))).toBe(false);
    expect(inviteUsable(inv({ state: "已撤销" }))).toBe(false);
  });

  // ⚠️ 前端不能自己按字段重算状态 —— 后端已经算好了
  it("🔑 以接口给的 state 为准（不自己推）", () => {
    expect(inviteUsable(inv({ state: "已过期", expires_at: "2999-01-01T00:00:00Z" }))).toBe(
      false,
    );
  });
});

describe("revokeInviteText", () => {
  it("没人用过 → 说明对方就换不了码", () => {
    expect(revokeInviteText(inv())).toContain("还没有人用过");
  });

  it("🔑 已用掉 → 说清**已加入的人不受影响**", () => {
    expect(revokeInviteText(inv({ used: 1 }))).toContain("已经加入的成员不受影响");
  });

  it("带上前缀让人认得出是哪一段", () => {
    expect(revokeInviteText(inv())).toContain("ABCD");
  });
});

describe("redeemResultText", () => {
  it("新加入 → 成功", () => {
    expect(redeemResultText({ already_member: false, message: "已加入「甲」" })).toContain("✅");
  });

  it("⚠️ 已经是成员 → 用提示语气而不是报错", () => {
    const t = redeemResultText({ already_member: true, message: "你已经是「甲」的成员了" });
    expect(t).toContain("ℹ️");
    expect(t).not.toContain("✅");
  });

  // 🔑 不自己猜原因：后端给什么就显示什么
  it("🔑 原样带上后端文案（不加工）", () => {
    const t = redeemResultText({ already_member: false, message: "邀请码无效（可能已过期…）" });
    expect(t).toContain("邀请码无效（可能已过期…）");
  });
});

describe("addMemberHint", () => {
  // ⚠️ 这是最容易让人以为"系统坏了"的一句
  it("🔑 说清「只能加已注册邮箱」并指向邀请码", () => {
    const t = addMemberHint();
    expect(t).toContain("已经注册");
    expect(t).toContain("邀请码");
  });
});

describe("removeMemberText", () => {
  const m: MemberRow = { user_id: 5, email: "b@t.local", display_name: "Bob", role: "member" };

  it("说是谁", () => {
    expect(removeMemberText(m, "a@t.local")).toContain("b@t.local");
  });

  it("🔑 移除自己时明确警告", () => {
    expect(removeMemberText(m, "b@t.local")).toContain("你自己");
  });

  it("移除别人不说「你自己」", () => {
    expect(removeMemberText(m, "a@t.local")).not.toContain("你自己");
  });

  it("说清不可撤销", () => {
    expect(removeMemberText(m, "a@t.local")).toContain("不能撤销");
  });

  it("display_name 为空时退回邮箱", () => {
    const t = removeMemberText({ ...m, display_name: null }, null);
    expect(t).toContain("b@t.local");
  });
});

describe("canManage", () => {
  it("只有 owner 能管理", () => {
    expect(canManage("owner")).toBe(true);
    expect(canManage("member")).toBe(false);
  });

  // ⚠️ 未知角色不能当 owner（fail-open 会把管理按钮露给不该看的人）
  it("🔑 未知角色 → 不能管理", () => {
    expect(canManage("admin")).toBe(false);
    expect(canManage("")).toBe(false);
  });
});
// ============================================================
// D132：工作区用量文案（端点是**已有的** admin 那条，只有 owner 能看）
// ============================================================

const STATS = {
  workspace_id: 5, kb_count: 3, doc_count: 12, chunk_count: 480,
  sum_doc_size_bytes: 1536, exclusive_blob_count: 2, blob_disk_bytes: 4096,
};

describe("usageText（D132）", () => {
  it("把库/文档/片段说全", () => {
    expect(usageText(STATS)).toBe("3 个库 · 12 份文档 · 480 个片段");
  });

  it("⚠️ 读不到时**不说 0**（那是「没用」，不是「没读到」）", () => {
    expect(usageText(null)).toBe("用量读不到");
    expect(usageText(null)).not.toContain("0");
    expect(diskText(null)).toContain("读不到");
  });

  it("真的没有内容时才是 0（与「读不到」要能区分）", () => {
    expect(usageText({
      workspace_id: 1, kb_count: 0, doc_count: 0, chunk_count: 0,
      sum_doc_size_bytes: 0, exclusive_blob_count: 0, blob_disk_bytes: 0,
    })).toBe("0 个库 · 0 份文档 · 0 个片段");
  });
});

describe("diskText（D132 · 两个字节数量的是不同的东西）", () => {
  it("🔑 同时给出原始与占盘，并说清占盘含派生物", () => {
    const t = diskText(STATS);
    expect(t).toContain("原始 1.5 KB");
    expect(t).toContain("占盘 4.0 KB");
    expect(t).toContain("预览与缩略图");
  });

  it("⚠️ 占盘比原始大时**不许**只显示一个（否则用户以为删库能腾那么多）", () => {
    // 这正是契约里那个实测：318KB 原始 → 722KB 占盘
    const t = diskText({
      ...STATS, sum_doc_size_bytes: 318 * 1024,
      blob_disk_bytes: 722 * 1024,
    });
    expect(t).toMatch(/原始 318\.0 KB/);
    expect(t).toMatch(/占盘 722\.0 KB/);
  });
});

describe("bytes", () => {
  it("三档：B / KB / MB，缺失给 \"-\" 而不是 0", () => {
    expect(bytes(999)).toBe("999 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(bytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(bytes(null)).toBe("-");
    expect(bytes(undefined)).toBe("-");
  });
});

describe("usageScopeHint", () => {
  it("🔑 说清只有 owner 能看，且解释为什么含别人的库", () => {
    expect(usageScopeHint()).toContain("owner");
    expect(usageScopeHint()).toContain("私有");
  });
});
