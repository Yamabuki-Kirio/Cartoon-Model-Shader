import { render } from "preact";
import { App } from "./app/App";
import { createWorkspace } from "./state/workspace";
import { createApiClient, readInjectedToken } from "./api/client";
import { Endpoints } from "./api/endpoints";
import { parseSchema } from "./schema/parse";
import "./styles/index.css";

const client = createApiClient();
const endpoints = new Endpoints(client);

const workspace = createWorkspace({
  endpoints: {
    surfaceSchema: (options) => endpoints.surfaceSchema(options),
    surfaceBaseline: (options) => endpoints.surfaceBaseline(options),
    createBaseline: (body, options) => endpoints.createBaseline(body, options),
    submitPreview: (payload, options) => endpoints.submitPreview(payload, options),
    job: (jobId, options) => endpoints.job(jobId, options),
    blenderStatus: () => endpoints.blenderStatus(),
    framingContext: () => endpoints.framingContext(),
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
