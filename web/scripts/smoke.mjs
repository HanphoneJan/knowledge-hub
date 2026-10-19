/**
 * 冒烟巡检（**开发工具**，不是 e2e 契约：D187 已删 e2e，别再复活选择器断言）。
 *
 * 它只回答两个问题：
 * 1. 每个路由**能不能打开**（没有白屏 / 没有未捕获异常）；
 * 2. 有没有**控制台 error 与失败请求**（4xx/5xx）—— 尤其是"静默 401"那一类
 *    （页图 / 缩略图端点带不了 Bearer 时就是它在叫）。
 *
 * ```bash
 * node scripts/smoke.mjs                 # 用真实登录态跑全部路由
 * node scripts/smoke.mjs --login         # 额外验一次"表单登录"这条路
 * ```
 */
import { chromium } from "playwright-core";

const BASE = process.env.PKH_WEB ?? "http://127.0.0.1:5173";
const EMAIL = process.env.PKH_EMAIL ?? "shot@local.test";
const PASSWORD = process.env.PKH_PASSWORD ?? "shot12345";
const TOKEN = process.env.PKH_TOKEN ?? "";

const USER = JSON.stringify({
  id: 5695,
  email: EMAIL,
  display_name: "截图账号",
  nickname: "shot",
  role: "superadmin",
});

/** 覆盖：外壳三区 + 库详情五个 tab + 设置七个分区 + 登录页。 */
const ROUTES = [
  "/chat",
  "/kb",
  "/kb/6133",
  "/kb/6133?tab=search",
  "/kb/6133?tab=settings",
  "/kb/6134?tab=timeline",
  "/kb/6135?tab=photos",
  "/settings?section=keys",
  "/settings?section=members",
  "/settings?section=trash",
  "/settings?section=profile",
  "/settings?section=users",
  "/settings?section=llm",
  "/settings?section=appearance",
];

/** `--login`：先走一遍**表单登录**（那就不能预注入 token，否则 /login 会直接跳走）。 */
const WITH_FORM = process.argv.includes("--login");

const browser = await chromium.launch({
  executablePath: process.env.PKH_CHROME ?? "/usr/bin/google-chrome",
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
await ctx.addInitScript(
  ([token, user]) => {
    try {
      if (token) {
        localStorage.setItem("pkh.api_key", token);
        localStorage.setItem("pkh.user", user);
        localStorage.setItem("pkh.theme", "light");
      }
    } catch {
      /* ignore */
    }
  },
  [WITH_FORM ? "" : TOKEN, USER],
);

const page = await ctx.newPage();
let failures = 0;

// ---- 1) 表单登录这条路（可选）----
if (WITH_FORM) {
  const errs = [];
  page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
  await page.goto(`${BASE}/login`, { waitUntil: "networkidle" });
  await page.fill("#login-email", EMAIL);
  await page.fill("#login-pw", PASSWORD);
  await page.click('button[type="submit"]');
  try {
    await page.waitForURL(/\/chat/, { timeout: 8000 });
    console.log("✓ 表单登录 → /chat");
  } catch {
    failures += 1;
    console.log("✗ 表单登录没有跳到 /chat");
    const alert = await page.locator('[role="status"]').first().textContent().catch(() => null);
    if (alert) console.log("  页面提示：", alert);
  }
  for (const e of errs) console.log("  ", e);
}

// ---- 2) 逐路由：控制台 error + 失败请求 ----
for (const route of ROUTES) {
  const problems = [];
  const onMsg = (m) => {
    if (m.type() === "error") problems.push(`console: ${m.text().slice(0, 200)}`);
  };
  const onErr = (e) => problems.push(`pageerror: ${String(e.message).slice(0, 200)}`);
  const onResp = (r) => {
    const s = r.status();
    if (s >= 400 && !r.url().includes("favicon")) problems.push(`HTTP ${s} ${r.url()}`);
  };
  page.on("console", onMsg);
  page.on("pageerror", onErr);
  page.on("response", onResp);

  await page.goto(`${BASE}${route}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  // 白屏判据：根节点里有没有可读文本
  const text = (await page.locator("#root").innerText().catch(() => "")) || "";
  const blank = text.trim().length < 5;

  page.off("console", onMsg);
  page.off("pageerror", onErr);
  page.off("response", onResp);

  const bad = problems.length > 0 || blank;
  if (bad) failures += 1;
  console.log(`${bad ? "✗" : "✓"} ${route}${blank ? " [空白页]" : ""}`);
  for (const p of problems.slice(0, 6)) console.log("    ", p);
}

await browser.close();
console.log(failures === 0 ? "\n全部路由通过。" : `\n有 ${failures} 处问题。`);
process.exit(failures === 0 ? 0 : 1);
