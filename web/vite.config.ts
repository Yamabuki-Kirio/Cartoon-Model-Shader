import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { tokenPlaceholderPlugin } from "./tooling/token-placeholder";

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: here,
  // 构建产物由 FastAPI 挂在 /next 下，因此资源引用也必须带这个前缀。
  base: "/next/",
  plugins: [tokenPlaceholderPlugin()],
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
        target: "http://127.0.0.1:8765",
        changeOrigin: false,
      },
    },
  },
});
