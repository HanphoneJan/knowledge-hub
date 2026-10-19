/**
 * 手动截图工具（D187 之后前端唯一的"验收"方式 —— 目测）。
 *
 * ```bash
 * node scripts/screenshot.mjs /chat /kb/6134 /settings?section=appearance
 * node scripts/screenshot.mjs --mobile /chat
 * ```
 *
 * - 用 `/usr/bin/google-chrome`（playwright-core 不自带浏览器）
 * - 直接往 localStorage 写登录态（`pkh.api_key` / `pkh.user`），跳过登录表单
 * - 每条路由出两张：浅色 + 深色（`pkh.theme` 决定首绘）；输出到 `web/.shots/`
 *
 * ⚠️ 这只是**开发工具**，不是契约测试（D187 已删 e2e，别再复活选择器契约）。
 */
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = process.env.PKH_OUT ? resolve(process.cwd(), process.env.PKH_OUT) : resolve(HERE, "../.shots");

const BASE = process.env.PKH_WEB ?? "http://127.0.0.1:5173";
const TOKEN = process.env.PKH_TOKEN ?? "";
const USER = JSON.stringify({
  id: 5695,
  email: process.env.PKH_EMAIL ?? "shot@local.test",
  display_name: "截图账号",
  nickname: "shot",
  role: "superadmin",
});

const argv = process.argv.slice(2);
const mobile = argv.includes("--mobile");
/** `--anon`：不注入登录态（用来看登录页）。 */
const anon = argv.includes("--anon");
/** `--ask=问题`：真的发一轮问答再截图（验 SSE 接线 + 引用卡）。 */
const askArg = argv.find((a) => a.startsWith("--ask="));
const ask = askArg ? askArg.slice(6) : null;
/** `--search=关键词`：在检索 tab 里真查一次（命中卡 + 四腿调试面板）。 */
const searchArg = argv.find((a) => a.startsWith("--search="));
const search = searchArg ? searchArg.slice(9) : null;
const routes = argv.filter((a) => !a.startsWith("--"));
const targets = routes.length ? routes : ["/chat"];

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.PKH_CHROME ?? "/usr/bin/google-chrome",
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-device-scale-factor=1"],
});

const ctx = await browser.newContext({
  viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  locale: "zh-CN",
});

const WS = process.env.PKH_WS ?? "";
const KBS = process.env.PKH_KBS ?? "";

await ctx.addInitScript(
  ([token, user, ws, kbs]) => {
    try {
      if (ws) localStorage.setItem("pkh.ws", ws);
      if (kbs) localStorage.setItem("pkh.kbs", kbs);
      if (token && token !== "anon") {
        localStorage.setItem("pkh.api_key", token);
        localStorage.setItem("pkh.user", user);
      }
    } catch {
      /* ignore */
    }
  },
  [anon ? "anon" : TOKEN, USER, WS, KBS],
);

const page = await ctx.newPage();

for (const route of targets) {
  for (const theme of ["light", "dark"]) {
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    await page.evaluate((th) => localStorage.setItem("pkh.theme", th), theme);
    await page.goto(`${BASE}${route}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(900);

    if (ask) {
      // 真的问一轮：Enter 发送 → 等流式结束（停止按钮消失）
      const box = page.locator("textarea").first();
      await box.click();
      await box.fill(ask);
      await box.press("Enter");
      await page.waitForTimeout(1500);
      await page.screenshot({
        path: resolve(OUT, `ask-streaming${mobile ? "-mobile" : ""}-${theme}.png`),
      });
      for (let i = 0; i < 40; i++) {
        const busy = await page.locator('button[aria-label="停止生成"]').count();
        if (busy === 0) break;
        await page.waitForTimeout(500);
      }
      await page.waitForTimeout(600);
    }

    if (search && route.includes("tab=search")) {
      // 检索 tab：填 query → 点「检索」→ 等结果与调试面板画出来
      await page.fill('input[placeholder^="在「"]', search);
      await page.click('button[type="submit"]');
      await page.waitForTimeout(3500);
      await page.screenshot({ path: resolve(OUT, `search-top-${theme}.png`) });
      // 内容区自己滚动（外壳 overflow-hidden），fullPage 抓不到下面
      await page.evaluate(() => {
        const el = document.querySelector("main .overflow-y-auto");
        if (el) el.scrollTop = el.scrollHeight;
      });
      await page.waitForTimeout(700);
      await page.screenshot({ path: resolve(OUT, `search-debug-${theme}.png`) });
      console.log("saved search-debug", theme);
    }

    const name = `${route.replace(/[/?=&]/g, "_").replace(/^_/, "") || "root"}${mobile ? "-mobile" : ""}-${theme}.png`;
    await page.screenshot({ path: resolve(OUT, name), fullPage: false });
    console.log("saved", name);

    if (ask) {
      // 引用抽屉（非模态）也截一张：验画框
      const cite = page.locator('button[aria-label="查看引用来源"]').first();
      if (await cite.isEnabled()) {
        await cite.click();
        await page.waitForTimeout(2500);
        await page.screenshot({
          path: resolve(OUT, `citations${mobile ? "-mobile" : ""}-${theme}.png`),
        });
        console.log("saved citations", theme);
      }
    }
  }
}

await browser.close();
