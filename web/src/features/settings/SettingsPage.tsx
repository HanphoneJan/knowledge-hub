/**
 * 设置页（D186 分区化）—— **左导航在二级面板，右侧只渲染当前分区**。
 *
 * 🔑 `?section=` 是唯一真相，分区清单的唯一来源是 `./sections.ts`
 * （面板与页面都读它 ⇒ 不会出现"面板里有、页面里没有"）。
 * 🔑 主题只有一个入口：这里的外观分区（别处再放一个 = 设计错误）。
 */
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { Page, PageHeader } from "../../components/ui/page";
import { useAuth } from "../../app/auth";
import { normalizeSection, SETTINGS_SECTIONS, visibleSections } from "./sections";
import { AppearanceSection } from "./sections/AppearanceSection";
import { KeysSection } from "./sections/KeysSection";
import { LlmSection } from "./sections/LlmSection";
import { MembersSection } from "./sections/MembersSection";
import { ProfileSection } from "./sections/ProfileSection";
import { TrashSection } from "./sections/TrashSection";
import { UsersSection } from "./sections/UsersSection";

export function SettingsPage() {
  const [params] = useSearchParams();
  const { isSuperadmin } = useAuth();
  const section = normalizeSection(params.get("section"));

  const allowed = visibleSections(isSuperadmin).some((s) => s.key === section);
  const meta = SETTINGS_SECTIONS.find((s) => s.key === section);

  return (
    <Page>
      <PageHeader
        title="设置"
        meta={<span>{meta?.label ?? ""} · 分区由 ?section= 决定（可分享 / 可后退）</span>}
      />

      {!allowed ? (
        <div className="text-sm text-text-secondary">这个分区只对超级管理员可见。</div>
      ) : (
        <React.Fragment>
          {section === "keys" && <KeysSection />}
          {section === "members" && <MembersSection />}
          {section === "trash" && <TrashSection />}
          {section === "profile" && <ProfileSection />}
          {section === "users" && <UsersSection />}
          {section === "llm" && <LlmSection />}
          {section === "appearance" && <AppearanceSection />}
        </React.Fragment>
      )}
    </Page>
  );
}
