/**
 * 仅开发期的令牌桥。
 *
 * 生产路径：FastAPI 托管构建产物，并在页面响应里注入会话令牌 —— 前端只从
 * `window.__TOON_TUNER_TOKEN__` 读。
 *
 * 开发路径：页面由 Vite dev server 提供，而令牌只存在于 FastAPI 的响应里，
 * 于是 `npm run dev` 下**所有写请求都会 401**。这个插件负责把那一份令牌代取过来。
 *
 * 三点约束：
 *
 * * `apply: "serve"` —— 只在 `vite dev` 存在，**不进构建产物**（`dist/` 里没有它）。
 * * **不引入新的暴露面**：令牌本来就写在 FastAPI 页面响应里，同机进程直接请求那个页面
 *   即可读到；这里只是替浏览器代取一次。dev server 也只在开发时启动、只监听本机。
 * * 不落日志、不回显令牌：失败时只回原因文本。
 */

import type { Plugin } from "vite";

/** 会话令牌在页面里的注入形态；与 `src/server/security.py::inject_token` 保持一致。 */
const TOKEN_SCRIPT = /window\.__TOON_TUNER_TOKEN__\s*=\s*"([^"]+)"/;

/** 开发期令牌接口路径（只在 dev server 上存在）。 */
export const DEV_TOKEN_PATH = "/__dev/token";

/** 从 FastAPI 返回的 HTML 里取出运行时注入的令牌；取不到返回 `null`。 */
export function extractInjectedToken(html: string): string | null {
  const match = html.match(TOKEN_SCRIPT);
  return match ? match[1] : null;
}

export interface DevTokenBridgeOptions {
  /** 后端地址；应与 `server.proxy` 的 target 用同一个值。 */
  backendOrigin?: string;
}

export function devTokenBridgePlugin(options: DevTokenBridgeOptions = {}): Plugin {
  const backendOrigin = options.backendOrigin ?? "http://127.0.0.1:8765";
  return {
    name: "toon-tuner-dev-token",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(DEV_TOKEN_PATH, (_request, response) => {
        const send = (status: number, body: unknown): void => {
          response.statusCode = status;
          response.setHeader("content-type", "application/json; charset=utf-8");
          // 令牌一律不缓存。
          response.setHeader("cache-control", "no-store");
          response.end(JSON.stringify(body));
        };

        void (async () => {
          try {
            const page = await fetch(`${backendOrigin}/`, { headers: { accept: "text/html" } });
            const token = extractInjectedToken(await page.text());
            if (!token) {
              send(502, {
                error: `未能从 ${backendOrigin} 取到会话令牌，请确认后端（FastAPI）已启动。`,
              });
              return;
            }
            send(200, { token });
          } catch (error) {
            send(502, {
              error: `连接后端失败：${error instanceof Error ? error.message : String(error)}`,
            });
          }
        })();
      });
    },
  };
}
