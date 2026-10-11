/**
 * 应用外壳（D162 信息架构 / D185 应用式布局 / D189 重写）。
 *
 * ```
 * ┌──┬─────────┬─────────────────────────┐
 * │图│ 二级面板 │ 内容区（自己滚动）        │
 * │标│ 280px   │ flex-1 min-w-0            │
 * │栏│         │                           │
 * └──┴─────────┴─────────────────────────┘
 * ```
 *
 * 🔑 三条硬规矩：
 * 1. 左图标栏三区（对话 / 知识库 / 设置）—— **面板互斥渲染**；
 * 2. 内容区 `min-w-0 flex-1`，`<main>` 是 `overflow-hidden`，**页面自己滚**
 *    （CONVENTIONS §10.5.2：新页面别再假设 main 会滚动）；
 * 3. 移动端（<768px）：只有图标栏 + 内容区，二级面板进左侧 `Sheet`。
 *    ⚠️ 桌面 / 移动是**二选一渲染**（`useMediaQuery`），不是 CSS 藏一个 ——
 *    否则同一控件在 DOM 里出现两次。
 */
import * as React from "react";
import { Outlet, useLocation } from "react-router-dom";

import { Sheet, SheetContent } from "../components/ui/sheet";
import { TooltipProvider } from "../components/ui/tooltip";
import { useMediaQuery } from "../app/scope";
import { ChatSide } from "./ChatSide";
import { IconRail } from "./IconRail";
import { KbSide } from "./KbSide";
import { SettingsSide } from "./SettingsSide";

type Zone = "chat" | "kb" | "settings";

function zoneOf(pathname: string): Zone {
  if (pathname.startsWith("/kb")) return "kb";
  if (pathname.startsWith("/settings")) return "settings";
  return "chat";
}

export function AppShell() {
  const { pathname } = useLocation();
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const [navOpen, setNavOpen] = React.useState(false);

  const zone = zoneOf(pathname);

  // 换区时关掉移动抽屉（否则点完还停在原来的面板上）
  React.useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  const panel = zone === "kb" ? <KbSide /> : zone === "settings" ? <SettingsSide /> : <ChatSide />;

  return (
    <TooltipProvider delayDuration={300} skipDelayDuration={200}>
      <div className="flex h-full w-full overflow-hidden bg-bg-canvas text-text-primary">
        <IconRail onOpenNav={isDesktop ? undefined : () => setNavOpen(true)} />

        {isDesktop ? (
          <aside className="flex w-[288px] shrink-0 flex-col border-e-[0.5px] border-border-button bg-bg-component">
            {panel}
          </aside>
        ) : (
          <Sheet open={navOpen} onOpenChange={setNavOpen}>
            <SheetContent title={zone === "kb" ? "知识库" : zone === "settings" ? "设置" : "对话"}>
              {panel}
            </SheetContent>
          </Sheet>
        )}

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <Outlet />
        </main>
      </div>
    </TooltipProvider>
  );
}
