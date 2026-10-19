/**
 * 极简 `t()`（D188：照搬 RAGFlow 组件时用它顶替 react-i18next）。
 *
 * ## 为什么有这个东西
 *
 * 参考项目 RAGFlow 的组件里到处是 `react-i18next` 的 `t('key')`。我们要**照搬**
 * 它的组件，但**不必真引 i18n**（内网自部署、只有中文一种语言）。
 * ⇒ 用这个"直接返回中文"的函数顶替 `useTranslation()`，拷进来的组件改一行 import 即可。
 *
 * 🔑 它**不是** i18n 框架：没有语言切换、没有复数、没有命名空间。
 *    查不到 key 就返回 key 本身（照搬来的组件在没登记的 key 上会显示 key ——
 *    一眼能看出漏了哪条，比静默显示空串好）。
 *
 * 插值只支持 `{{name}}`（RAGFlow 的那一种）。
 */

const zh: Record<string, string> = {
  "common.search": "搜索",
  "common.cancel": "取消",
  "common.confirm": "确定",
  "common.delete": "删除",
  "common.save": "保存",
  "common.edit": "编辑",
  "common.close": "关闭",
  "common.loading": "加载中",
  "common.empty": "暂无数据",
  "common.retry": "重试",
  "common.copy": "复制",
  "common.copied": "已复制",
  "common.name": "名称",
  "common.description": "描述",
  "common.actions": "操作",
  "common.create": "新建",
  "common.back": "返回",
};

export type TVars = Record<string, string | number>;

export function t(key: string, vars?: TVars): string {
  const raw = zh[key] ?? key;
  if (!vars) return raw;
  return raw.replace(/\{\{(\w+)\}\}/g, (m, name: string) =>
    name in vars ? String(vars[name]) : m,
  );
}

/** 给已登记 key 的组件用（照搬 RAGFlow 时的习惯写法）。 */
export function useTranslation(): { t: typeof t } {
  return { t };
}
