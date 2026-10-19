# 第三方代码与设计来源（web/）

> D188 明确允许**逐字照搬**同栈参考项目的代码；这份文件就是那条"保留版权 / NOTICE"的落地。
> 只列**真的拷了代码或色值**的来源；只借"设计"（布局/间距/交互）的不列代码版权，但要写明来源。

## RAGFlow（Apache License 2.0）

- 上游：<https://github.com/infiniflow/ragflow> · 本地只读参考：`.research/ragflow/`
- 许可：Apache-2.0（允许复制、修改、再分发，需保留版权与许可声明）
- 我们的技术栈与它同栈（React + TypeScript + Tailwind + shadcn/ui 模式）⇒ **可以逐字照搬**。
- 🔑 **许可证全文随附在本仓库**：[`THIRD-PARTY-LICENSES/Apache-2.0.txt`](./THIRD-PARTY-LICENSES/Apache-2.0.txt)
  （Apache-2.0 §4(a) 要求把许可证副本交给接收方）。
- ⚠️ 上游这 6 个文件**没有逐文件版权头**（`web/src/components/ui/*.tsx` 直接从 `import` 开始），
  RAGFlow 仓库里也**没有 NOTICE 文件** ⇒ 我们能遵行的就是：① 随附许可证全文；② 在**我们这边**
  的每个拷贝文件头部写明"照搬自谁 + 改了什么"（§4(b) 的 modified-files 声明）；
  ③ 这份清单写明出处与改动（§4(c) 的归属声明）。**不要**在文件头编造上游版权行。

### 逐字照搬的文件（改动只有 import 路径与 Tailwind v4 的写法差异）

| 我们的文件 | 上游文件 | 实际改动 |
|---|---|---|
| `src/components/ui/button.tsx` | `web/src/components/ui/button.tsx` | `cn` 的 import；去掉 react-router 的 `asLink`；`border-0.5` → `border-[0.5px]` |
| `src/components/ui/badge.tsx` | `web/src/components/ui/badge.tsx` | `cn` 的 import；补一个 `dot`（本项目状态胶囊用） |
| `src/components/ui/card.tsx` | `web/src/components/ui/card.tsx` | `cn` 的 import；`CardHeader` 的默认内边距 `p-6` → `p-4`（本项目是紧凑卡） |
| `src/components/ui/table.tsx` | `web/src/components/ui/table.tsx` | `cn` 的 import；`border-b-0.5` → `border-b-[0.5px]`；补 `TableEmptyRow` |
| `src/components/ui/input.tsx` | `web/src/components/ui/input.tsx` | `cn` 的 import；去掉 `react-i18next` 的 `SearchInput` 包装（保留 prefix/suffix/密码眼睛的逻辑） |
| `src/components/ui/textarea.tsx` | `web/src/components/ui/textarea.tsx` | `cn` 的 import；`border` → `border-[0.5px]`（`autoSize` 自增高逻辑照搬） |
| `src/shell/SidePanel.tsx` 的行/分组写法 | `web/src/components/ui/*` 的组合方式 | 结构参考，非逐字 |

### 照搬的**设计 token 命名与色值**（`src/styles.css`）

RAGFlow 的语义 token 名（`--bg-base` / `--bg-card` / `--bg-input` / `--bg-title` / `--text-primary` /
`--text-secondary` / `--text-disabled` / `--border-default` / `--border-button` / `--accent-primary` /
`--state-success|warning|error` / `--background-card` …）与其亮/暗两套色值，直接沿用；
理由写在该文件头部（照搬来的组件里写的就是这些类名，改名等于逐行改代码）。

### 没有照搬的部分（有意）

- **它的 `hooks/` 与 `services/`**（打它自己的后端）⇒ 一律换成我们的 `src/lib/api.ts`。
- **它的 i18n**（`react-i18next`）⇒ 用 `src/app/i18n.ts` 的极简 `t()`（直接返回中文）。
- **它的 zustand store** ⇒ 用 React state / context。
- **它的类型**（`IDataset` / `ITenant` …）⇒ 一律用我们的 `src/types/api.ts`。
- **它的 Tailwind v3 配置**（`tailwind.config.js` 里的 `borderRadius` 覆盖等）⇒ 我们用 Tailwind v4 默认刻度。

## Open WebUI（BSD-3-Clause + 品牌条款）

- 上游：<https://github.com/open-webui/open-webui> · 本地只读参考：`.research/open-webui/`
- 它是 **SvelteKit**：`.svelte` 源码拷进 React 编不过 ⇒ **只 1:1 还原设计，不拷代码**。

还原了设计的部分（**无代码复制**）：
对话页的顶部细栏、居中阅读列、用户右对齐气泡、助手「头像 + 无气泡正文 + 可点 `[n]` 角标」、
底部悬浮圆角输入条（`rounded-3xl` + 细边框 + `shadow-lg` + 圆形发送钮）、空态「品牌标 + 大标题 + 建议卡网格」、
二级面板「新对话 + 最近」与 hover 才显操作。

⚠️ 上游另有一份品牌使用条款（logo / 名称）⇒ 我们不使用它的任何商标或美术资源，
品牌标是我们自己画的一个占位 glyph。
