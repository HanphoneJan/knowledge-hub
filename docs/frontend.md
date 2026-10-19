# 前端架构

> React 19 + Vite 7 单页应用。**运行时零 CDN / 零 Web Font**，内网离线可用。
>
> **本文定位**：前端架构概览（路由、功能区、设计意图）。目录细节、本地工具与
> "必须保住的坑"的**权威落点**在 [`web/README.md`](../web/README.md) —— 两边冲突时以它为准。

---

## 1. 技术栈

| 层 | 选型 |
|---|---|
| 框架 | React 19 + Vite 7 + TypeScript |
| 样式 | TailwindCSS v4（`@tailwindcss/vite`，无 PostCSS） |
| 组件 | shadcn/ui 模式自建（`src/components/ui/*`），设计 token 与部分组件照搬 RAGFlow |
| 图标 | `lucide-react` |
| 数据 | `@tanstack/react-query` |
| 路由 | `react-router-dom` v7 |
| 交互参考 | 对话页与外壳按 Open WebUI 1:1 还原设计（**只还原设计，未拷代码**） |

设计 token 与部分组件（`button` / `badge` / `card` / `table` / `input` / `textarea`）来自 RAGFlow（Apache-2.0），逐条出处与改动见 `web/THIRD-PARTY-NOTICES.md`。

---

## 2. 应用外壳

```
┌──┬─────────┬─────────────────────────┐
│图│ 二级面板 │ 内容区（自己滚动）        │
│标│ 288px   │ flex-1 min-w-0            │
│栏│         │                           │
└──┴─────────┴─────────────────────────┘
```

三条硬规矩：

1. **左图标栏三区**（对话 / 知识库 / 设置）——面板**互斥渲染**；
2. 内容区 `min-w-0 flex-1`，`<main>` 是 `overflow-hidden`，**页面自己滚**（新页面别假设 main 会滚动）；
3. 移动端（<768px）：只有图标栏 + 内容区，二级面板进左侧 `Sheet`。桌面 / 移动是**二选一渲染**（`useMediaQuery`），不是 CSS 藏一个——否则同一控件在 DOM 里出现两次。

### 2.1 路由

| 路由 | 页面 |
|---|---|
| `/login` | 登录 / 注册 |
| `/chat`、`/chat/:sessionId` | 对话 |
| `/kb` | 知识库列表 |
| `/kb/:kbId` | 知识库详情（tabs） |
| `/settings` | 设置（7 分区） |
| `*` | 回 `/chat` |

> 旧路由 `/search` `/debug` `/timeline` `/photos` `/sessions` **已删除（不重定向）**——这些能力现在都在会话、知识库 tabs 或检索面板里。库范围改为**多选**。

---

## 3. 功能区

### 3.1 chat/ — 对话

- `useAsk`：SSE 事件流；
- `Composer`：输入框与发送；
- `MessageList` / `AnswerText`：消息渲染；
- `CitationDrawer`：引用抽屉（逐条命中片段 + 原文出处）；
- `PageOverlay`：页图叠加命中框。

SSE 要点：`/ask` 是 **POST** ⇒ 不能用 `EventSource`，要 `fetch` + `ReadableStream` 手解 `\n\n`。帧序 `sources → delta* → done`，`sources` 先到就先画引用卡片。

### 3.2 knowledge/ — 知识库

- `KbListPage` / `KbDetailPage`；
- 详情 tabs：文档（上传 + 状态 + 进度）、检索、时间线、照片、设置；
- `strategy.ts`：切分策略的前端投影（纯逻辑 + 单测）。

切分策略卡会明确提示边界：六种策略、“不回溯”提示、`contextual` 没配 LLM 时会降级、`late` 更慢、`semantic` 对 PDF 不生效，并给出“重新解析全部 N 份”的出口。**改策略只对之后摄取的文档生效**，所以界面必须给出这个出口，否则那句提示就是空话。

### 3.3 settings/ — 设置

7 个分区，`sections.ts` 是**唯一清单**：

外观 · 账号资料 · 知识库默认 · 工作区与成员 · 外部大模型 · 用户管理 · 关于。

外部大模型分区对应 `GET/PUT /admin/llm` 与 `POST /admin/llm/test`；API Key 只回掩码。用户管理分区仅超管可见。

### 3.4 login/

登录 / 注册。⚠️ **不自动重登**：每次 login 都会新铸一把 Key，反复登录会把 `api_keys` 表塞满；只在 401 时清登录态。

---

## 4. `src/lib/`：纯逻辑核心

这些函数各自带单测，是前端最值得信赖的部分：

| 文件 | 职责 |
|---|---|
| `api.ts` | API 客户端；`searchKbs` 等 |
| `sse.ts` | POST SSE 解析 |
| `boxes.ts` | 归一化坐标 → 矩形；按 `locations` 形状分派 |
| `progress.ts` | 进度判定（只读 `jobs.progress`） |
| `upload.ts` | 上传分批、汇总 |
| `trash.ts` / `containerTrash.ts` | 回收站三态 |
| `timeline.ts` / `photos.ts` | 时间线与照片墙 |
| `kbScope.ts` / `kbStrategy.ts` | 库范围与策略投影 |
| `theme.ts` | 主题三态 + DOM 薄层 |
| `settings.ts` | 设置项 |
| `thumb.ts` | 缩略图状态 |
| `pii.ts` | PII 报告展示 |

---

## 5. 样式系统与主题

五条规矩只在 `styles.css` 一处维护：

1. 颜色只出现在 `styles.css` 的 `:root` / `.dark` 两段；
2. token 名沿用 RAGFlow；
3. 原生控件优先；
4. `cn()` 要注册自定义字号；
5. `RichText` 渲染 `**粗体**`；危险动作走原生 `confirm`。

这套实现特有的三点：

- `styles.css` 用 `@custom-variant dark (&:where(.dark, .dark *))`：照搬来的 RAGFlow 组件里有 `dark:` 变体，而 Tailwind v4 默认把它绑 `prefers-color-scheme`，不绑类的话用户手选浅色时那几处还是暗的；
- 圆角用 v4 默认刻度：控件 `rounded-md/lg`、卡片 `rounded-2xl`、气泡与输入条 `rounded-3xl`；
- 主题：存储键 `localStorage["pkh.theme"]`（存**意图** `light` / `dark` / `system`）→ 生效开关 **`<html class="dark">`**。

主题的那份判定有**两份拷贝**（`app/theme.ts` 与 `index.html` 首绘前的内联脚本），所以 `theme.test.ts` 有 6 条守卫扫 `index.html` 钉住它们。**单一入口**（只在“设置 → 外观”）与**首绘不闪**是两条质量属性，别动。

---

## 6. 必须保住的坑

| 坑 | 一句话 | 权威出处 |
|---|---|---|
| **画框坐标** | `locations[].bbox` 是**归一化 0~1**，`toRect` 是唯一换算点；按 `locations` 的**形状**分派（`row`/`col` → 网格、`kind==="page"` → 整页、其余 → 单框），**不按 `kind`** | `lib/boxes.ts` · `features/chat/PageOverlay.tsx` |
| **页图不能用 `<img src>`** | 页图/缩略图/原图三个端点都要 Bearer ⇒ 一律 `fetch` → blob → objectURL | `features/chat/useBlobUrl.ts` |
| **进度** | 百分比**只能**来自 `jobs.progress`；没有 `jobs` 行就什么都不显示；`done`/`failed` 不画条 | `lib/progress.ts` |
| **上传** | 单请求 ≤20 份（分批）；上传阶段**不给百分比**；汇总只算这一批、平均**排除失败** | `lib/upload.ts` |
| **SSE** | `/ask` 是 POST ⇒ 不能用 `EventSource`；帧序 `sources → delta* → done` | `lib/sse.ts` |
| **历史消息的角标** | 后端只存编号、不存来源身份 ⇒ 历史 `[n]` **点不出原文**，界面如实说明、不给死链 | `features/chat/types.ts` |
| **回收站 / 覆盖式更新** | 软删 30 天可恢复（三态 restore/expired/blocked）；「清空」是**唯一不可撤销**的端点 ⇒ 先 `dry_run` 预览再确认 | `lib/trash.ts` · `lib/containerTrash.ts` |
| **时间线 / 照片墙** | `days` 是**子集**（“某天不在日历里” ≠ “那天没记录”），对 file 库**不发**注定 400 的请求；照片墙**不丢无日期的照片**（最后一档），`page` 以 **URL** 为唯一真相 | `lib/timeline.ts` · `lib/photos.ts` |
| **结果正文** | 用 `hit.text`，**不用** `snippets`（它是字面子串匹配的产物，自然语言提问常常为空）——`snippets` 只用来打高亮 | `lib/api.ts::searchKbs` |

### 6.1 引用抽屉是怎么画的

1. 命中里带 `locations`（归一化 bbox）；
2. 页图端点要 Bearer ⇒ `fetch` 拿 blob → `createObjectURL`；
3. `toRect` 把归一化坐标换算成百分比，铺在页图上——**显示尺寸与坐标解耦**，DPI 变了框也不会错位；
4. 表格块按 `row`/`col` 排网格，或直接用 `table_json` 渲染真表格；
5. 扫描页只高亮整页（没有文本层，画不出框）。

---

## 7. 已知取舍

- **不引入 pdf.js**：底图用后端渲染的页图，与服务端 `render_page_with_boxes` 的框位同源。代价：页面不能选中文字。
- **API Key 存 localStorage**：当前鉴权模型就是“每个客户端一把 key”。**Key 等同密码**，别在共享机器上登录。
- **不自动重登**：每次登录都会新铸一把 key；只在 401 时清登录态。
- **零新增依赖**：不引 zustand / i18n / Radix Select / Radix DropdownMenu——状态用 context、`t()` 自写、下拉用原生 `<select>`、行操作菜单自写。
- **不维护 DOM/class 契约**：唯一的自动化验收线是**接口对齐**（`src/types/api.ts` 与契约一致）。

---

## 8. 开发与验收

```bash
cd web
npm install
npm run dev -- --host 127.0.0.1   # :5173，/api 代理到 :8010
npm test                          # vitest：核心纯逻辑 + 主题首绘守卫
npm run typecheck                 # tsc --noEmit
npm run build                     # tsc -b && vite build
```

开发期手动工具（不是 e2e 契约）：

```bash
node scripts/screenshot.mjs /chat /kb '/kb/6133?tab=docs'   # 每条路由浅/深两张
node scripts/screenshot.mjs --mobile /chat                  # 移动视口
node scripts/screenshot.mjs /chat '--ask=这个库里有什么？'    # 真发一轮问答 + 打开引用抽屉
node scripts/smoke.mjs --login                              # 逐路由：空白页 / console error / 4xx-5xx
```

- 用系统 Chrome（playwright-core 不自带浏览器）；
- `demo-data.mjs` 造一套**合成**演示数据；⚠️ **别拿真数据截图**——那会随仓库一起公开；
- `smoke.mjs` 是开发期安全网（抓白屏、未捕获异常、静默 401），**不是** e2e——别往里面加选择器断言。

`package.json` 里有 `"allowScripts": {"esbuild@0.28.2": true}`：npm 11 默认不执行依赖的 install 脚本，不显式批准的话 esbuild 的二进制装不上、`vite` 直接起不来。

---

## 9. 相关文档

- SSE 端点字段：[API 契约](./DESIGN-API.md)
- 坐标来源与表格结构：[检索与融合](./retrieval.md) · [文档处理与切分](./ingestion.md)
- 前端测试在整体验收里的位置：[测试与验收体系](./testing.md)
