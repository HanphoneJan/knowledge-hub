/**
 * 外观分区 —— **主题的唯一入口**（设置 → 外观）。
 *
 * 🔑 三态分段控件（跟随系统 / 浅色 / 深色），`app/theme.ts` 是唯一的写入方，
 * 生效开关是 `<html class="dark">`；首绘那一帧靠 `index.html` 的内联脚本。
 * ⚠️ 别在别处再放一个主题开关（重复入口 = 设计错误，用户点过名）。
 */
import { Monitor, Moon, Sun } from "lucide-react";
import * as React from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../../components/ui/card";
import { Segmented } from "../../../components/ui/segmented";
import { applyTheme, readPref, THEME_LABEL, watchSystem, type ThemePref } from "../../../app/theme";

export function AppearanceSection() {
  const [pref, setPref] = React.useState<ThemePref>(() => readPref());

  // 跟随系统时，系统切换要实时反映（显式选了浅/暗就不该被系统动）
  React.useEffect(() => {
    if (pref !== "system") return;
    return watchSystem(() => applyTheme("system"));
  }, [pref]);

  const onChange = React.useCallback((next: ThemePref) => {
    setPref(next);
    applyTheme(next);
  }, []);

  return (
    <div className="flex max-w-[640px] flex-col gap-3.5">
      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>主题</CardTitle>
          <CardDescription>
            存的是意图（localStorage 的 pkh.theme）；选「跟随系统」时系统切换会实时跟随。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 p-4 pt-0">
          <Segmented
            name="theme"
            ariaLabel="主题"
            value={pref}
            onChange={onChange}
            options={[
              { value: "system", label: THEME_LABEL.system, icon: Monitor, title: "跟随系统" },
              { value: "light", label: THEME_LABEL.light, icon: Sun, title: "浅色" },
              { value: "dark", label: THEME_LABEL.dark, icon: Moon, title: "深色" },
            ]}
          />
          <div className="text-2xs text-text-secondary">
            生效开关是 &lt;html class="dark"&gt;；首绘前由 index.html 的内联脚本写这个类，
            所以刷新页面不会闪一下另一个主题。
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle>关于这套界面</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1.5 p-4 pt-0 text-2xs text-text-secondary">
          <div>
            设计 token 与部分组件照搬 RAGFlow（Apache-2.0，见 web/THIRD-PARTY-NOTICES.md）。
          </div>
          <div>对话页与外壳按 Open WebUI 的设计 1:1 还原（它是 SvelteKit，代码不能直接拷）。</div>
          <div>零 CDN / 零 Web Font：图标与字体全部打包进产物，内网离线可用。</div>
        </CardContent>
      </Card>
    </div>
  );
}
