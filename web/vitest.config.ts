import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: { "@": resolve(here, "src") },
  },
  test: {
    environment: "jsdom",
    // jsdom 缺少 `onpointer*` IDL 属性，会让 Preact 注册驼峰事件名、
    // 指针事件在测试里打不中（见该文件说明）。所有环境都跑一遍，node 环境下自行跳过。
    setupFiles: ["./src/testing/setup-dom.ts"],
    // `tooling/` 放构建侧的东西（令牌占位插件、产物断言）；它们不属于应用源码，
    // 但同样需要测试覆盖。
    include: ["src/**/*.test.ts", "src/**/*.test.tsx", "tooling/**/*.test.ts"],
    // 不启用 globals：测试里显式 import { describe, it, expect }，依赖关系更清楚。
    globals: false,
    restoreMocks: true,
    clearMocks: true,
  },
});
