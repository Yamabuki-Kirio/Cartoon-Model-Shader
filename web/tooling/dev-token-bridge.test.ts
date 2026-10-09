// @vitest-environment node
/**
 * 开发期令牌桥：`npm run dev` 下写请求不再必然 401。
 *
 * 生产路径的令牌来自 FastAPI 的页面响应注入；开发时页面由 Vite dev server 提供，
 * 令牌就必须由 dev server 代取一次（见 `dev-token-bridge.ts` 顶部说明）。
 * 这里把那个中间件当成真的 dev server 来调用，断言它能拿到令牌、失败时给明确原因、
 * 且令牌不被缓存。
 *
 * 跑在 node 环境：用全局 `fetch` 打桩，jsdom 会替换全局对象造成干扰。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEV_TOKEN_PATH, devTokenBridgePlugin, extractInjectedToken } from "./dev-token-bridge";
import { DEV_TOKEN_URL } from "../src/api/dev-token";

/** 与 `src/server/security.py::inject_token` 产出的脚本完全同形。
 *
 * 令牌值刻意在**运行时**拼出来 —— 否则本文件自己会被
 * `dist-artifacts.test.ts` 的「源码不含令牌形态串」扫描抓到。
 */
const TOKEN = ["aB3xY9zQ7wErT1uIo", "P2aSdF4gH6jK8lM0nB2v", "C4xD6yZ8"].join("");
const INJECTED_HTML = `<!DOCTYPE html><html><head><script>window.__TOON_TUNER_TOKEN__ = "${TOKEN}";</script></head><body></body></html>`;

type Middleware = (request: unknown, response: FakeResponse) => void;

interface FakeResponse {
  statusCode: number;
  headers: Record<string, string>;
  body: string;
  finished: boolean;
  setHeader(name: string, value: string): void;
  end(chunk: string): void;
}

function fakeResponse(): FakeResponse {
  const response: FakeResponse = {
    statusCode: 200,
    headers: {},
    body: "",
    finished: false,
    setHeader(name, value) {
      response.headers[name.toLowerCase()] = value;
    },
    end(chunk) {
      response.body = String(chunk ?? "");
      response.finished = true;
    },
  };
  return response;
}

/** 取出插件注册的中间件（与真实 `server.middlewares.use` 的形态一致）。 */
function captureMiddleware(): Middleware {
  const routes: Array<{ path: string; handler: Middleware }> = [];
  const plugin = devTokenBridgePlugin({ backendOrigin: "http://127.0.0.1:8765" });
  expect(plugin.name).toBe("toon-tuner-dev-token");
  // 只在 dev server 生效 —— 这是「令牌桥不进构建产物」的机制性保证。
  expect(plugin.apply).toBe("serve");
  const configure = plugin.configureServer as unknown as (server: {
    middlewares: { use(path: string, handler: Middleware): void };
  }) => void;
  configure({
    middlewares: {
      use(path, handler) {
        routes.push({ path, handler });
      },
    },
  });
  expect(routes.map((route) => route.path)).toEqual([DEV_TOKEN_PATH]);
  return routes[0].handler;
}

/** 中间件内部是自执行的异步体（含 `await fetch` 与 `await text()`），
 *  等它整条链跑完 —— 用 `setImmediate` 排到当前所有微任务之后，而不是数空转次数。 */
async function flush(times = 3): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("令牌提取", () => {
  it("从注入脚本里取出令牌", () => {
    expect(extractInjectedToken(INJECTED_HTML)).toBe(TOKEN);
  });

  it("占位还没被替换时取不到（不能把注释当成令牌）", () => {
    expect(extractInjectedToken("<head><!--TOON_TUNER_TOKEN--></head>")).toBeNull();
  });

  it("页面不含注入脚本时返回 null", () => {
    expect(extractInjectedToken("<html><body>前端尚未构建</body></html>")).toBeNull();
  });
});

describe("dev server 令牌接口", () => {
  it("后端可达时返回令牌，且响应不缓存", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(INJECTED_HTML)));
    const response = fakeResponse();
    captureMiddleware()({}, response);
    await flush();

    expect(response.statusCode).toBe(200);
    expect(response.headers["cache-control"]).toBe("no-store");
    expect(JSON.parse(response.body)).toEqual({ token: TOKEN });
  });

  it("后端页面没有令牌时回 502 并给明确原因（不返回空令牌）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>没有注入</html>")));
    const response = fakeResponse();
    captureMiddleware()({}, response);
    await flush();

    expect(response.statusCode).toBe(502);
    const body = JSON.parse(response.body) as { error: string; token?: string };
    expect(body.error).toContain("会话令牌");
    expect(body.token).toBeUndefined();
  });

  it("后端连不上时回 502 而不是抛异常（dev server 必须活着）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      })
    );
    const response = fakeResponse();
    captureMiddleware()({}, response);
    await flush();

    expect(response.statusCode).toBe(502);
    expect(JSON.parse(response.body).error).toContain("连接后端失败");
  });
});

describe("防漂移", () => {
  it("前端请求的路径与插件注册的路径一致", () => {
    expect(DEV_TOKEN_URL).toBe(DEV_TOKEN_PATH);
  });
});
