/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

/**
 * 后端地址。走 **dev proxy**（同源）而不是让前端直接 fetch `http://127.0.0.1:8010`：
 *
 * 1. 页图端点（`/documents/{id}/pages/{n}`）要带 `Authorization: Bearer`，
 *    所以那张图必须用 `fetch` 拿 blob 再 `URL.createObjectURL`，
 *    不能用 `<img src>`。同源之后连 blob URL 都是同源的。
 * 2. CORS 已经放行 5173（`services/api/main.py`），但同源少一层变量：
 *    代理不缓冲，SSE（`/ask` 的 delta 帧）能逐帧到达（D46 的"逐字输出"）。
 */
const API_TARGET = process.env.PKH_API ?? "http://127.0.0.1:8010";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});