/**
 * ConfirmProvider —— 应用内确认框（替掉散落的 `window.confirm`）。
 *
 * ## 为什么不继续用原生 confirm
 *
 * 原生 confirm 能访问，但**不能给危险动作的"安全项"设默认焦点**，也不能带
 * 结构化说明（多段文案只能塞进一行）；同一件事在应用里就出现了两套观感。
 * 这里用已有的 Radix Dialog，`取消` 显式 `autoFocus`，对齐 dialog 指南：
 * 破坏性确认的初始焦点在**安全动作**上。
 *
 * ## 用法
 *
 * ```tsx
 * const confirm = useConfirm();
 * if (await confirm({ title: "删除？", danger: true, confirmLabel: "删除" })) { ... }
 * ```
 *
 * 返回 Promise<boolean>：确认 = true，取消 / Esc / 点遮罩 = false。
 */
import * as React from "react";

import { Button } from "./button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog";

export interface ConfirmOptions {
  title: string;
  /** 支持换行（`\n` 会被原样保留）。 */
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** true = 删除 / 撤销这类不可撤销动作，确认按钮用危险色。 */
  danger?: boolean;
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = React.createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [req, setReq] = React.useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(
    null,
  );

  const confirm = React.useCallback<ConfirmFn>(
    (options) => new Promise<boolean>((resolve) => setReq({ ...options, resolve })),
    [],
  );

  const settle = React.useCallback(
    (value: boolean) => {
      // 先取走再清空：清空会触发一次重渲染，闭包里的 req 可能已经变了。
      const current = req;
      setReq(null);
      current?.resolve(value);
    },
    [req],
  );

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog
        open={req != null}
        onOpenChange={(open) => {
          if (!open) settle(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{req?.title ?? ""}</DialogTitle>
            {req?.description != null && (
              <DialogDescription className="whitespace-pre-wrap">{req.description}</DialogDescription>
            )}
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" autoFocus onClick={() => settle(false)}>
              {req?.cancelLabel ?? "取消"}
            </Button>
            <Button
              variant={req?.danger ? "destructive" : "default"}
              onClick={() => settle(true)}
            >
              {req?.confirmLabel ?? "确定"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = React.useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm 必须在 <ConfirmProvider> 内使用");
  return ctx;
}
