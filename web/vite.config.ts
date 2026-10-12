import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { tokenPlaceholderPlugin } from "./tooling/token-placeholder";
import { devTokenBridgePlugin } from "./tooling/dev-token-bridge";

const here = dirname(fileURLToPath(import.meta.url));

/** 后端（FastAPI）地址：开发代理与开发期令牌桥共用同一个值。 */
const BACKEND_ORIGIN = process.env.TOON_TUNER_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  root: here,
  // 工作台由 FastAPI 挂在 `/` 下，因此资源引用就是根路径的 `/assets/*`。
  // 生产入口始终是 FastAPI；`npm run dev` 走 Vite dev server，两者路径一致。
  base: "/",
  plugins: [
    tokenPlaceholderPlugin(),
    // 仅开发：把 FastAPI 运行时注入的令牌转交给 dev server 上的页面，
    // 否则 `npm run dev` 下所有写请求都会 401。不参与构建。
    devTokenBridgePlugin({ backendOrigin: BACKEND_ORIGIN }),
  ],
  resolve: {
    alias: { "@": resolve(here, "src") },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // 产物不进仓库（见 .gitignore）；这里只保证体积可控、便于人工核对。
    chunkSizeWarningLimit: 700,
  },
  server: {
    // 开发时用 Vite dev server，把 /api 代理到 FastAPI（生产入口始终是 FastAPI）。
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND_ORIGIN,
        changeOrigin: false,
      },
    },
  },
});
