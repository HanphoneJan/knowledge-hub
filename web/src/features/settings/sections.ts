/**
 * 设置页的**分区清单**（D186）—— 唯一的真相源。
 *
 * 🔑 二级面板的导航列表与页面渲染的分区都读这一份：
 * 加/改分区只改这里，不会再出现"面板里有、页面里没有"的漂移
 * （CONVENTIONS §10.5.3 提醒过这个坑）。
 *
 * `?section=` 是唯一真相；缺省/非法值回落到第一个（`keys`）。
 */
import {
  DatabaseBackup,
  Monitor,
  Sparkles,
  Trash2,
  UserRound,
  UsersRound,
  KeyRound,
  type LucideIcon,
} from "lucide-react";

export const SETTINGS_SECTIONS = [
  { key: "keys", label: "API Key", icon: KeyRound, superadminOnly: false },
  { key: "members", label: "工作区与成员", icon: UsersRound, superadminOnly: false },
  { key: "trash", label: "回收站", icon: Trash2, superadminOnly: false },
  { key: "profile", label: "我的资料", icon: UserRound, superadminOnly: false },
  { key: "users", label: "用户管理", icon: DatabaseBackup, superadminOnly: true },
  { key: "llm", label: "外部大模型", icon: Sparkles, superadminOnly: false },
  { key: "appearance", label: "外观", icon: Monitor, superadminOnly: false },
] as const satisfies ReadonlyArray<{
  key: string;
  label: string;
  icon: LucideIcon;
  superadminOnly: boolean;
}>;

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]["key"];

export const DEFAULT_SECTION: SettingsSection = "keys";

/** 规范化 `?section=`：非法/缺失一律回落（别让 URL 决定渲染出空白页）。 */
export function normalizeSection(raw: string | null | undefined): SettingsSection {
  const hit = SETTINGS_SECTIONS.find((s) => s.key === raw);
  return (hit?.key ?? DEFAULT_SECTION) as SettingsSection;
}

/** 当前角色能看见的分区。 */
export function visibleSections(isSuperadmin: boolean) {
  return SETTINGS_SECTIONS.filter((s) => !s.superadminOnly || isSuperadmin);
}
