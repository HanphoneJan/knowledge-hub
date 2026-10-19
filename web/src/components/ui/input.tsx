/**
 * Input —— 照搬 RAGFlow `web/src/components/ui/input.tsx`（Apache-2.0）。
 *
 * 改动：`cn` 的 import；去掉 `useTranslation` 的 SearchInput 包装
 * （我们直接给 `placeholder`）；`border-0.5` → `border-[0.5px]`。
 * 保留它实测有价值的部分：`prefix` / `suffix` 槽 + 按内容测宽（图标不压字）、
 * 密码眼睛按钮、number 去步进箭头。
 */
import { Eye, EyeOff } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/cn";

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "prefix"> {
  prefix?: React.ReactNode;
  suffix?: React.ReactNode;
  rootClassName?: string;
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, rootClassName, type, value, onChange, prefix, suffix, ...props }, ref) => {
    const isControlled = value !== undefined;
    const { defaultValue, ...restProps } = props;
    const inputValue = isControlled ? value : defaultValue;
    const [showPassword, setShowPassword] = React.useState(false);
    const [prefixWidth, setPrefixWidth] = React.useState(0);
    const [suffixWidth, setSuffixWidth] = React.useState(0);

    const prefixRef = React.useRef<HTMLSpanElement>(null);
    const suffixRef = React.useRef<HTMLSpanElement>(null);

    React.useEffect(() => {
      if (prefixRef.current) setPrefixWidth(prefixRef.current.offsetWidth);
      if (suffixRef.current) setSuffixWidth(suffixRef.current.offsetWidth);
    }, [prefix, suffix]);

    const handleChange: React.ChangeEventHandler<HTMLInputElement> = (e) => {
      if (type === "number") {
        const numValue = e.target.value === "" ? "" : Number(e.target.value);
        onChange?.({
          ...e,
          target: { ...e.target, value: numValue },
        } as unknown as React.ChangeEvent<HTMLInputElement>);
      } else {
        onChange?.(e);
      }
    };

    const isPasswordInput = type === "password";

    const inputEl = (
      <input
        ref={ref}
        type={isPasswordInput && showPassword ? "text" : type}
        className={cn(
          "peer/input",
          "flex h-8 w-full rounded-md border-[0.5px] border-border-button bg-bg-input px-3 py-2 outline-none text-sm text-text-primary",
          "file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-text-disabled",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent-primary",
          "disabled:cursor-not-allowed disabled:opacity-50 transition-colors",
          type === "number" &&
            "[appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none",
          className,
        )}
        style={{
          paddingInlineStart: prefix && prefixWidth ? `${prefixWidth}px` : "",
          paddingInlineEnd: isPasswordInput ? "40px" : suffix && suffixWidth ? `${suffixWidth}px` : "",
        }}
        value={inputValue ?? ""}
        onChange={handleChange}
        {...restProps}
      />
    );

    if (prefix || suffix || isPasswordInput) {
      return (
        <div className={cn("relative", rootClassName)}>
          {prefix && (
            <span ref={prefixRef} className="absolute start-0 top-[50%] translate-y-[-50%] text-text-secondary">
              {prefix}
            </span>
          )}
          {inputEl}
          {suffix && (
            <span
              ref={suffixRef}
              className={cn("absolute end-0 top-[50%] translate-y-[-50%] text-text-secondary", {
                "end-14": isPasswordInput,
              })}
            >
              {suffix}
            </span>
          )}
          {isPasswordInput && (
            <button
              type="button"
              aria-label={showPassword ? "隐藏密码" : "显示密码"}
              className="p-2 text-text-secondary absolute border-0 end-1 top-[50%] translate-y-[-50%]"
              onClick={() => setShowPassword((v) => !v)}
            >
              {showPassword ? <EyeOff className="size-[1em]" /> : <Eye className="size-[1em]" />}
            </button>
          )}
        </div>
      );
    }

    return inputEl;
  },
);

Input.displayName = "Input";

/** 带放大镜的搜索框（RAGFlow 的 `SearchInput`，去掉 i18n）。 */
export const SearchInput = React.forwardRef<HTMLInputElement, InputProps>(
  function SearchInput(props, ref) {
    return (
      <Input
        ref={ref}
        prefix={
          <svg
            aria-hidden="true"
            className="ms-2 me-1 size-[1em]"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
        }
        {...props}
      />
    );
  },
);

export { Input };
