/** 设置区的二级面板：**分区导航**（D186）。分区清单的唯一来源是 `sections.ts`。 */
import * as React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../app/auth";
import { PanelGroup, PanelRow, SidePanel } from "./SidePanel";
import { normalizeSection, visibleSections } from "../features/settings/sections";

export function SettingsSide() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { isSuperadmin } = useAuth();
  const current = normalizeSection(params.get("section"));
  const items = visibleSections(isSuperadmin);

  const go = React.useCallback(
    (key: string) => navigate(`/settings?section=${key}`),
    [navigate],
  );

  return (
    <SidePanel title="设置">
      <PanelGroup>
        {items.map((s) => (
          <PanelRow
            key={s.key}
            active={current === s.key}
            onClick={() => go(s.key)}
            icon={s.icon}
            title={s.label}
          />
        ))}
      </PanelGroup>
    </SidePanel>
  );
}
