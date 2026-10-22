# 前端：外壳、对话与引用

前端是 React 19 + Vite 7 + TypeScript 单页应用，样式用 TailwindCSS v4，组件走 shadcn/ui 模式自建。运行时零 CDN、零 Web Font，内网离线可用。目录细节与本地工具以 `web/README.md` 为准。

## 技术栈与目录

| 层 | 选型 |
|---|---|
| 框架 | React 19 + Vite 7 + TypeScript |
| 样式 | TailwindCSS v4，`@tailwindcss/vite`，无 PostCSS |
| 组件 | `src/components/ui/*`，设计 token 与部分组件照搬 RAGFlow |
| 图标 | `lucide-react` |
| 数据 | `@tanstack/react-query` |
| 路由 | `react-router-dom` v7 |
| 交互参考 | 对话页与外壳按 Open WebUI 还原设计，只还原设计，未拷代码 |

设计 token 与 `button`、`badge`、`card`、`table`、`input`、`textarea` 来自 RAGFlow（Apache-2.0），逐条出处见 `web/THIRD-PARTY-NOTICES.md`。

目录按职责分：

```text
src/
  app/          主题、登录态、库范围、query key、极简 t()
  shell/        图标栏 + 二级面板 + 内容区
  features/
    chat/       对话页、SSE、引用抽屉、页图叠加
    knowledge/  知识库列表与详情 5 个 tab
    settings/   设置 7 个分区
    login/      登录与注册
  components/ui/  自建组件
  lib/          纯逻辑核心，各自带单测
  types/api.ts  契约的手抄投影
```

## 外壳与路由

外壳是三列：左侧图标栏、二级面板、内容区。图标栏分对话、知识库、设置三区，面板互斥渲染；内容区 `min-w-0 flex-1`，`<main>` 是 `overflow-hidden`，**页面自己滚**。新页面不要假设 main 会滚动。

移动端（宽度小于 768 px）只留图标栏与内容区，二级面板进左侧 `Sheet`。桌面与移动是二选一渲染（`useMediaQuery`），不是用 CSS 藏一个，否则同一控件会在 DOM 里出现两次。

主要路由如下：

| 路由 | 页面 |
|---|---|
| `/login` | 登录与注册 |
| `/chat`、`/chat/:sessionId` | 对话 |
| `/kb` | 知识库列表 |
| `/kb/:kbId` | 知识库详情，tab 在 `?tab=` |
| `/settings` | 设置 |

旧路由 `/search`、`/debug`、`/timeline`、`/photos`、`/sessions` 已删除且不重定向，这些能力现在都在会话、知识库 tab 或检索面板里。库范围从单选改成多选，范围逻辑在 `src/lib/kbScope.ts`。

## 对话页与 SSE

数据流是两条：`/chat/:sessionId` 进来先 `GET /sessions/{id}` 取历史；输入条回车走 `useAsk.send`，发 `POST /ask` 的 SSE 流。

`/ask` 是 POST 加 JSON body 加 Bearer，所以**不能用 `EventSource`**。`EventSource` 只能 GET、不带自定义请求头、不带请求体；用它连请求都发不出去，界面表现为一直转圈。`src/lib/sse.ts` 用 `fetch` 加 `ReadableStream` 手解帧：一帧以空行结束，一帧可能被 TCP 切成任意多个 chunk，所以按 `\n\n` 攒够再解析。

帧序固定为 `sources` → `delta*` → `done`。`sources` 先到就先画引用卡片，不等 `done`。首轮请求带 `session_id: null`，后端在 `done` 里返回新 id，前端拿到后用 `replace` 换 URL，不留下一条能返回的空页。

停止生成要落到明确状态。`abort()` 之后消息标 `stopped`，否则那条消息会永远停在“生成中”的转圈上。已经有半截正文时不能抹掉，保留并标注中断。

切换会话时只有换成另一个会话才清空 turns；从 `/chat` 跳到刚创建的 `/chat/{id}` 不算换，否则刚生成的 `sources` 会立刻退化成历史消息，引用角标变得不可核验。历史消息只灌一次，且只在当前没有内容时灌；无条件用历史覆盖会把刚拿到的 `sources` 丢掉。

## 引用抽屉与画框

引用抽屉分两栏：左边是逐条命中片段与出处，右边是页图。点角标时记住“这是哪一轮的引用”，因为每轮回答有自己的 `sources`。

画框的几何层在 `src/lib/boxes.ts`，是纯函数，唯一会静默出错的一层：框会画出来，只是整体偏了半行。三条规矩：

- `locations[].bbox` 是归一化 0~1，必须乘回**当前渲染页图**的像素尺寸；直接当像素用会让框整体下移约半行。
- 按 `locations` 的**形状**分派，不按 `kind`：有 `row` / `col` 就是表格网格；`kind === "page"` 是整页高亮；其余按单框画。对外引用类型只有 `text` / `page` / `image`，而表格块会以 `kind="text"` 加一堆带行列的框出现。
- 表格格子超过 24 个时默认只画外框，并给一个展开逐格的开关。这不是为了性能，是为了分主次：用户要看的是“这张表”，逐格描边只是辅助。

页图、缩略图、原件三个端点都要 Bearer，所以**不能用 `<img src>`**，否则静默 401，只在控制台看得到。`useBlobUrl` 统一走 `fetch` → blob → `createObjectURL`，卸载时释放。

扫描页没有文本层，画不出精确框，只高亮整页。没有 `locations` 时也要给一句原因，不能静默不画——那会让人以为“没有坐标”。

## 知识库的五个 tab

`/kb/:kbId` 的 tab 以 `?tab=` 为唯一真相，不放进 React state，否则“复制链接给同事”打开的会是另一个页面。tab 有五个：

| tab | 内容 |
|---|---|
| `docs` | 上传、文档列表、状态徽章、进度、回收站入口 |
| `search` | 检索与调试面板，四路召回与 RRF 明细 |
| `timeline` | 聊天按天聚合与照片，只对 chat / photo 库出现 |
| `photos` | 照片墙，只对 photo 库出现 |
| `settings` | 切分策略、可见性、PII、精排等库级设置 |

只有 `chat` 与 `photo` 库渲染时间线 tab，只有 `photo` 库有照片 tab，而且对不适用的库**不发**注定 400 的请求。时间线的 `days` 是子集，“某天不在日历里”不等于“那天没记录”；照片墙不丢无日期的照片，把它们放最后一档。

切分策略卡明确写出边界：六种策略、“不回溯”提示、`contextual` 没配 LLM 时会降级、`late` 更慢、`semantic` 对 PDF 不生效，并给出“重新解析全部 N 份”的出口。改策略只对之后摄取的文档生效，没有这个出口，那句提示就是空话。

上传单请求最多 20 份，超过要分批。上传阶段不给百分比，因为还没有 `jobs` 行；汇总只算这一批，平均耗时排除失败。

## 设置的七个分区

`src/features/settings/sections.ts` 是唯一清单，导航与页面渲染都读它：

| key | 标签 | 可见性 |
|---|---|---|
| `keys` | API Key | 所有人 |
| `members` | 工作区与成员 | 所有人 |
| `trash` | 回收站 | 所有人 |
| `profile` | 我的资料 | 所有人 |
| `users` | 用户管理 | 仅超管 |
| `llm` | 外部大模型 | 所有人 |
| `appearance` | 外观 | 所有人 |

`?section=` 是唯一真相，缺省或非法值回落到第一个 `keys`。外部大模型分区对应 `GET/PUT /admin/llm` 与 `POST /admin/llm/test`，API Key 只回掩码。回收站分容器与文档两层：软删 30 天可恢复，三态是 restore / expired / blocked；“清空”是唯一不可撤销的动作，所以先 `dry_run` 预览再确认。

## 样式与主题

五条规矩只在 `src/styles.css` 一处维护：颜色只出现在 `:root` 与 `.dark` 两段；token 名沿用 RAGFlow；原生控件优先；`cn()` 要注册自定义字号；`RichText` 渲染 `**粗体**`，危险动作走原生 `confirm`。

这套实现特有的三点：

- `@custom-variant dark (&:where(.dark, .dark *))`。照搬来的 RAGFlow 组件里有 `dark:` 变体，Tailwind v4 默认把它绑 `prefers-color-scheme`；不绑类的话，用户手选浅色时那几处还是暗的。
- 圆角用 v4 默认刻度：控件 `rounded-md/lg`，卡片 `rounded-2xl`，气泡与输入条 `rounded-3xl`。
- 主题在 `localStorage["pkh.theme"]` 里存**意图**（`light` / `dark` / `system`），生效开关是 `<html class="dark">`。

主题判定有两份拷贝：`src/app/theme.ts` 与 `index.html` 首绘前那段普通内联脚本。所以 `theme.test.ts` 有 6 条守卫扫 `index.html` 钉住它们。单一入口（只在“设置 → 外观”）与首绘不闪是两条质量属性，别动。

## 必须保住的坑与验收

| 坑 | 一句话 | 落点 |
|---|---|---|
| 画框坐标 | bbox 归一化，`toRect` 是唯一换算点，按形状分派 | `lib/boxes.ts`、`PageOverlay.tsx` |
| 页图 | 端点要 Bearer，不能用 `<img src>` | `features/chat/useBlobUrl.ts` |
| 进度 | 百分比只能来自 `jobs.progress`；没有 job 行就不显示；done / failed 不画条 | `lib/progress.ts` |
| 上传 | 单请求最多 20 份，分批；上传阶段不给百分比；平均排除失败 | `lib/upload.ts` |
| SSE | POST 不能用 EventSource；帧序 sources → delta* → done | `lib/sse.ts` |
| 历史角标 | 后端只存编号，历史 `[n]` 点不出原文，界面如实说明 | `features/chat/types.ts` |
| 回收站 | 三态可恢复；清空先 dry_run | `lib/trash.ts`、`lib/containerTrash.ts` |
| 时间线 | days 是子集；对 file 库不发请求；照片墙保留无日期照片 | `lib/timeline.ts`、`lib/photos.ts` |
| 结果正文 | 用 `hit.text`，`snippets` 只用来打高亮 | `lib/api.ts` |

开发与验收：

```bash
cd web
npm install
npm run dev -- --host 127.0.0.1   # :5173，/api 代理到 :8010
npm test                          # vitest，当前 316 项
npm run typecheck
npm run build
```

前端不维护 DOM 或 class 契约，唯一的自动化验收线是接口对齐：`src/types/api.ts` 与 `docs/DESIGN-API.md` 一致，后端 `eval/contract_check.py` 守路径、方法与参数面。目测用 `scripts/screenshot.mjs`，安全网用 `scripts/smoke.mjs` 抓白屏、未捕获异常与静默 401。演示数据用 `demo-data.mjs` 合成，别拿真实数据截图。

已知取舍：不引入 pdf.js，底图用后端渲染的页图，代价是页面不能选中文字；API Key 存 localStorage，key 等同密码；不自动重登，只在 401 时清登录态；零新增依赖，状态用 context、`t()` 自写、下拉用原生 `<select>`。

## 相关文档

- 目录、样式系统与开发工具：[web/README.md](../web/README.md)
- SSE 字段与检索响应：[API 契约](DESIGN-API.md)
- 引用坐标与证据组装：[检索与问答](retrieval.md)
- 表格与坐标从哪里来：[文档摄取](ingestion.md)
- 前端测试在验收里的位置：[测试与验收](testing.md)
