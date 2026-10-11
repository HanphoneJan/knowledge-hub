/**
 * 左侧**图标栏**（~64px）—— RAGFlow 的一级导航观感（圆角高亮底 + 图标）。
 *
 * 三个入口就是产品结构本身（对话 / 知识库 / 设置，D162），**别改**。
 * 底部是退出；移动端多一个「导航」按钮用来开二级面板的抽屉。
 */
import { BookText, LogOut, MessagesSquare, PanelLeft, Settings } from "lucide-react";
import * as React from "react";
import { NavLink, useNavigate } from "react-router-dom";

import { Hint } from "../components/ui/tooltip";
import { useAuth } from "../app/auth";
import { cn } from "../lib/cn";

const ZONES = [
  { to: "/chat", label: "对话", icon: MessagesSquare },
  { to: "/kb", label: "知识库", icon: BookText },
  { to: "/settings", label: "设置", icon: Settings },
] as const;

export function IconRail({ onOpenNav }: { onOpenNav?: () => void }) {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();

  const handleSignOut = React.useCallback(() => {
    signOut();
    navigate("/login", { replace: true });
  }, [signOut, navigate]);

  return (
    <nav className="flex w-[64px] shrink-0 flex-col items-center gap-1.5 border-e-[0.5px] border-border-button bg-bg-component py-3">
      {/* 品牌标 */}
      <div className="mb-2 flex size-9 items-center justify-center rounded-xl bg-text-primary text-bg-base">
        <svg aria-hidden="true" viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 1 4 16z" />
          <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5h5A1.5 1.5 0 0 0 20 16z" />
        </svg>
      </div>

      {onOpenNav && (
        <RailButton label="导航" onClick={onOpenNav}>
          <PanelLeft className="size-[18px]" />
        </RailButton>
      )}

      {ZONES.map((z) => (
        <NavLink key={z.to} to={z.to} className="group flex flex-col items-center" aria-label={z.label}>
          {({ isActive }) => (
            <span
              className={cn(
                "flex size-10 flex-col items-center justify-center gap-0.5 rounded-xl transition-colors",
                isActive
                  ? "bg-bg-card text-text-primary"
                  : "text-text-secondary hover:bg-bg-card hover:text-text-primary",
              )}
            >
              <z.icon aria-hidden="true" className="size-[18px] shrink-0" />
              <span className="text-[10px] leading-none">{z.label}</span>
            </span>
          )}
        </NavLink>
      ))}

      <div className="mt-auto flex flex-col items-center gap-1.5">
        <Hint label={`退出（${user?.display_name ?? user?.email ?? "当前账号"}）`} side="right">
          <button
            type="button"
            aria-label="退出登录"
            onClick={handleSignOut}
            className="flex size-10 items-center justify-center rounded-xl text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
          >
            <LogOut aria-hidden="true" className="size-[18px]" />
          </button>
        </Hint>
      </div>
    </nav>
  );
}

function RailButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Hint label={label} side="right">
      <button
        type="button"
        aria-label={label}
        onClick={onClick}
        className="flex size-10 items-center justify-center rounded-xl text-text-secondary transition-colors hover:bg-bg-card hover:text-text-primary"
      >
        {children}
      </button>
    </Hint>
  );
}
