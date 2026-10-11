/** 知识库的两个小徽章：**可见性**（三档）与**来源类型**（file/chat/photo）。 */
import { FileText, Image, MessagesSquare } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Hint } from "../../components/ui/tooltip";
import type { SourceKind, Visibility } from "../../types/api";

const VIS: Record<Visibility, { label: string; variant: "secondary" | "warning" | "success"; hint: string }> = {
  private: { label: "私有", variant: "secondary", hint: "只有创建者能看（D6 三档可见性）" },
  workspace: { label: "工作区", variant: "warning", hint: "本工作区全部成员能看" },
  global: { label: "全局", variant: "success", hint: "所有登录用户能看" },
};

export function VisibilityBadge({ v, size }: { v: Visibility; size?: "sm" | "default" }) {
  const it = VIS[v];
  return (
    <Hint label={it.hint}>
      <Badge variant={it.variant} size={size} dot>
        {it.label}
      </Badge>
    </Hint>
  );
}

export const VIS_LABEL: Record<Visibility, string> = {
  private: "私有 · 只有你",
  workspace: "工作区 · 成员可见",
  global: "全局 · 所有人可见",
};

export const SOURCE_KIND_LABEL: Record<SourceKind, string> = {
  file: "文档",
  chat: "聊天",
  photo: "照片",
};

export function SourceKindBadge({ kind }: { kind: SourceKind }) {
  const Icon = kind === "chat" ? MessagesSquare : kind === "photo" ? Image : FileText;
  return (
    <Badge variant="secondary" size="sm">
      <Icon aria-hidden="true" className="size-3" />
      {SOURCE_KIND_LABEL[kind]}
    </Badge>
  );
}
