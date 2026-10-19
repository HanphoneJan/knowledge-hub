# web/ — 前端

React + Vite 单页应用。照搬/参考的第三方来源见 [`THIRD-PARTY-NOTICES.md`](./THIRD-PARTY-NOTICES.md)。

> **本文定位**：目录、本地工具与实现细节（**权威落点**）。架构与设计意图的概览见
> [`docs/frontend.md`](../docs/frontend.md)；两边冲突时以本文为准。

React 19 + Vite 7 + TypeScript + **TailwindCSS v4**（`@tailwindcss/vite`，无 PostCSS）。
组件走 shadcn/ui 模式自建（`src/components/ui/*`），**设计 token 命名与部分组件照搬 RAGFlow**
（Apache-2.0，见 NOTICE），**对话页与外壳按 Open WebUI 1:1 还原设计**。图标 `lucide-react`，
数据 `@tanstack/react-query`，路由 `react-router-dom` v7（Radix 只用已装的 dialog/tooltip/label/slot）。
**运行时零 CDN / 零 Web Font**，内网离线可用。

```bash
cd web
npm install                # ⚠️ 见下方 allowScripts
npm run dev -- --host 127.0.0.1   # :5173，/api 代理到 :8010
npm test                   # vitest：核心纯逻辑 + 主题首绘守卫
npm run typecheck          # tsc --noEmit
npm run build              # tsc -b && vite build
```

### 目测与巡检（开发工具，不是 e2e 契约）

前端**不维护 DOM/class 契约**，唯一的自动化验收线是**接口对齐**。
留了两个手动工具：

```bash
node scripts/screenshot.mjs /chat /kb '/kb/6133?tab=docs'   # 每条路由出浅/深两张 → web/.shots/
node scripts/screenshot.mjs --mobile /chat                  # 390×844 移动视口
node scripts/screenshot.mjs /chat '--ask=这个库里有什么？'    # 真发一轮问答 + 打开引用抽屉
node scripts/screenshot.mjs --anon /login                   # 不注入登录态（看登录页）
node scripts/smoke.mjs --login                              # 逐路由：空白页 / console error / 4xx-5xx
```

- 用 `/usr/bin/google-chrome`（playwright-core 不自带浏览器）；`PKH_WEB` 换地址，`PKH_TOKEN` 换登录态；
  `PKH_OUT` 换输出目录、`PKH_WS` / `PKH_KBS` 把侧栏与检索范围收敛到演示数据。
- `smoke.mjs` 是**开发期安全网**（抓白屏、未捕获异常、静默 401 那类失败请求），
  **不是** e2e —— 别往里面加选择器断言（选择器不是契约）。
- `demo-data.mjs` 造一套**合成**演示数据（工作区 + 库 + 自造文档），
  根 `README.md` / `README_EN.md` 的截图就是用它 + `screenshot.mjs` 出的
  —— ⚠️ **别拿真数据截图**：那 36 个真实库是个人资料，截图会随仓库一起公开。

## 目录

```
src/
  styles.css        Tailwind 入口 + RAGFlow 命名的语义 token + `.dark` + `@theme inline` + `@utility`
  main.tsx / App.tsx  入口：QueryClient + AuthProvider + BrowserRouter + 路由表
  app/             主题（theme.ts + theme.test.ts）/ 登录态（auth.tsx）/ 库范围与工作区（scope.tsx）
                   / query key 工厂 / 极简 t()
  shell/           外壳：IconRail（图标栏）+ SidePanel（二级面板）+ ChatSide/KbSide/SettingsSide
  features/
    chat/          对话页：useAsk（SSE）/ Composer / MessageList / AnswerText / CitationDrawer / PageOverlay
    knowledge/     知识库：列表 / 详情（tabs: DocsTab·SearchTab·TimelineTab·PhotosTab·KbSettingsTab）
                   + strategy.ts（切分策略的前端投影；纯逻辑 + 单测）
    settings/      设置 7 分区：sections.ts 是**唯一**清单 + sections/*
    login/         登录 / 注册
  components/ui/   Button / Badge / Card / Table / Input / Textarea / Select / Switch / Segmented /
                   Tabs / Dialog / Sheet / Tooltip / Progress / Skeleton / EmptyState / Alert / Menu /
                   Avatar / RichText / Spinner / Page(+PageHeader)
  lib/             纯逻辑核心（`src/lib/*`：API 客户端、SSE、坐标、时间线…各自带单测）
  types/api.ts     契约的手抄投影（接口对齐的唯一真相）
```

## 样式系统与主题

五条规矩（**只在这一处维护**）：颜色只出现在 `styles.css` 的 `:root`/`.dark` 两段；token 名沿用 RAGFlow；
原生控件优先；`cn()` 要注册自定义字号；`RichText` 渲染 `**粗体**`；危险动作走原生 `confirm`。

本文件只补三点**这套实现特有**的：

- `styles.css` 用 `@custom-variant dark (&:where(.dark, .dark *))`：照搬来的 RAGFlow 组件里有 `dark:`
  变体，而 Tailwind v4 默认把它绑 `prefers-color-scheme` ⇒ 不绑类的话用户手选浅色时那几处还是暗的。
- 圆角用 **v4 默认刻度**：控件 `rounded-md/lg`、卡片 `rounded-2xl`、气泡与输入条 `rounded-3xl`（24px）。
- 主题：存储键 `localStorage["pkh.theme"]`（存**意图** `light`/`dark`/`system`）→ 生效开关
  **`<html class="dark">`**（写入方 `app/theme.ts` + `index.html` 首绘前那段**普通**内联脚本）→
  `app/theme.test.ts` 6 条守卫扫 `index.html`（那份判定有**两份拷贝**，所以钉住）。
  🔑 **单一入口**（只在「设置 → 外观」）与**首绘不闪**两条是质量属性，别动。

## 必须保住的坑（实现都在 `lib/*` 与功能目录里）

| 坑 | 一句话 | 权威出处 |
|---|---|---|
| **画框坐标** | `locations[].bbox` 是**归一化 0~1**，`toRect` 是唯一换算点；按 `locations` 的**形状**分派（`row`/`col` → 网格、`kind==="page"` → 整页、其余 → 单框），**不按 `kind`** | `lib/boxes.ts`；渲染在 `features/chat/PageOverlay.tsx`（换算成百分比铺回，显示尺寸与坐标解耦） |
| **页图不能用 `<img src>`** | 页图/缩略图/原图三个端点都要 Bearer ⇒ 一律 `fetch` → blob → objectURL | `features/chat/useBlobUrl.ts` |
| **进度** | 百分比**只能**来自 `jobs.progress`；没有 `jobs` 行就什么都不显示；`done`/`failed` 不画条 | `lib/progress.ts`；轮询用 `refetchInterval`（纯函数判定），绕开手写定时器那个坑 |
| **上传** | 单请求 ≤20 份（分批）；上传阶段**不给百分比**；汇总只算这一批、平均**排除失败** | `lib/upload.ts` |
| **SSE** | `/ask` 是 POST ⇒ 不能用 `EventSource`；`fetch` + `ReadableStream` 手解 `\n\n`；帧序 `sources → delta* → done`，**`sources` 先到就先画引用卡** | `lib/sse.ts` |
| **历史消息的角标** | 后端 `chat_messages` 只存编号、不存来源身份 ⇒ 历史 `[n]` **点不出原文**，界面如实说明、不给死链 | `features/chat/types.ts` |
| **回收站 / 覆盖式更新** | 软删 30 天可恢复（三态 restore/expired/blocked）；「清空」是**唯一不可撤销**的端点 ⇒ 先 `dry_run` 预览再确认 | `lib/trash.ts` · `lib/containerTrash.ts` |
| **时间线 / 照片墙** | `days` 是**子集**（"某天不在日历里" ≠ "那天没记录"），对 file 库**不发**注定 400 的请求；照片墙**不丢无日期的照片**（最后一档），`page` 以 **URL** 为唯一真相 | `lib/timeline.ts` · `lib/photos.ts` |
| **结果正文** | 用 `hit.text`，**不用** `snippets`（它是字面子串匹配的产物，自然语言提问常常为空）——`snippets` 只用来打高亮 | `lib/api.ts::searchKbs` |

## 验收

- `npm run typecheck && npm run build` 必须过；`npm test` 必须全绿（当前 **316** 项：纯逻辑单测 + 主题首绘守卫 + 切分策略 + locale 守卫）。
- 接口对齐：`src/types/api.ts` 与 `docs/DESIGN-API.md` 一致；后端 `uv run python eval/contract_check.py` 守路径/方法/参数面。
- 逐页浅/深**目测**（`scripts/screenshot.mjs`）+ `scripts/smoke.mjs` 无 console error / 无 4xx-5xx。

## 已知取舍

- **不引入 pdf.js**：底图用后端渲染的页图，与服务端 `render_page_with_boxes` 的框位同源。代价：页面不能选中文字。
- **API Key 存 localStorage**（当前鉴权模型就是「每个客户端一把 key」）。**key 等同密码**，别在共享机器上登录。
- **不自动重登**：每次登录都会新铸一把 key，反复登录会把 `api_keys` 表塞满。只在 401 时清登录态。
- **零新增依赖**：不引 zustand / i18n / Radix Select / Radix DropdownMenu ——
  状态用 context、`t()` 自写、下拉用原生 `<select>`、行操作菜单自写（`components/ui/menu.tsx`）。
- **支持改切分策略与新建工作区**：知识库「设置」tab 有策略卡（六种策略 + "不回溯"提示 +
  `contextual` 没配 LLM 时明说会降级 + `late` 更慢 + `semantic` 对 PDF 不生效 + 「重新解析全部 N 份」的出口），
  设置 →「工作区与成员」可新建工作区。`Kb.chunk_strategy` 是可选的（缺省按默认策略渲染，
  默认值只在 `features/knowledge/strategy.ts::strategyOf()` 落一次）。
- **改切分策略仍然只对"之后摄取的文档"生效**（后端 `PATCH /kb/{id}` 刻意不自动重跑）——
  界面因此必须给出「重新解析全部」这个出口，否则那句提示就是空话。

## npm 的 `allowScripts`

`package.json` 里有 `"allowScripts": {"esbuild@0.28.2": true}`。npm 11 默认**不执行**依赖的 install 脚本，
不显式批准的话 esbuild 的二进制装不上、`vite` 直接起不来。
