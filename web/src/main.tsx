import { render } from "preact";
import { App } from "./app/App";
import { createWorkspace } from "./state/workspace";
import { createApiClient, readInjectedToken } from "./api/client";
import { Endpoints } from "./api/endpoints";
import { parseSchema } from "./schema/parse";
import "./styles/index.css";

const client = createApiClient();
const endpoints = new Endpoints(client);

/**
 * 工作台能用的接口**逐条列出来**，而不是把整个 `Endpoints` 直接传进去。
 *
 * 这是一份能力清单：工作台能调用哪些接口，在这里一眼看得完。
 * 传整个对象的话，任何一个新增接口都会自动对工作台可见 —— 那就没有边界了。
 */
const workspace = createWorkspace({
  endpoints: {
    surfaceSchema: (options) => endpoints.surfaceSchema(options),
    surfaceBaseline: (options) => endpoints.surfaceBaseline(options),
    createBaseline: (body, options) => endpoints.createBaseline(body, options),
    submitPreview: (payload, options) => endpoints.submitPreview(payload, options),
    job: (jobId, options) => endpoints.job(jobId, options),
    blenderStatus: () => endpoints.blenderStatus(),
    framingContext: () => endpoints.framingContext(),
    listPresets: (options) => endpoints.listPresets(options),
    readPreset: (presetId, options) => endpoints.readPreset(presetId, options),
    createPreset: (body, options) => endpoints.createPreset(body, options),
    updatePreset: (presetId, body, options) => endpoints.updatePreset(presetId, body, options),
    deletePreset: (presetId, options) => endpoints.deletePreset(presetId, options),
    commitPrepare: (body, options) => endpoints.commitPrepare(body, options),
    commitApply: (body, options) => endpoints.commitApply(body, options),
    materialGraphSchema: (options) => endpoints.materialGraphSchema(options),
    parseSchema,
  },
});

/**
 * 挂载前先确保令牌就位。
 *
 * 生产：令牌已由 FastAPI 注入，这里什么都不做。
 * 开发：页面由 Vite dev server 提供，令牌要从 dev server 的令牌桥取一次
 * （见 `api/dev-token.ts`）；否则所有写请求都会 401。
 *
 * `import.meta.env.DEV` 在生产构建里会被替换成 `false`，整段（含动态 import）
 * 随之被移除，因此 dev-only 代码不会进入 `dist/`。
 */
async function mount(): Promise<void> {
  if (import.meta.env.DEV && !readInjectedToken()) {
    const { installDevToken } = await import("./api/dev-token");
    await installDevToken();
  }
  const target = document.getElementById("app");
  if (target) {
    render(<App workspace={workspace} />, target);
  }
}

void mount();

export {};
