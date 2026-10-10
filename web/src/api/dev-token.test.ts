/**
 * 开发期令牌装载：装进**与生产同一个**全局变量，前端始终只有一处读令牌。
 *
 * 关键断言是最后那条：装完之后用普通的 `ApiClient` 发写请求，请求头里真的带上了令牌 ——
 * 否则「开发能用」只是看起来能用。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEV_TOKEN_URL, installDevToken } from "./dev-token";
import { ApiClient, TOKEN_GLOBAL, TOKEN_HEADER, readInjectedToken } from "./client";

const TOKEN = "dev-token-abc";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function clearGlobalToken(): void {
  delete (window as unknown as Record<string, unknown>)[TOKEN_GLOBAL];
}

afterEach(() => {
  clearGlobalToken();
  vi.unstubAllGlobals();
});

describe("开发期令牌装载", () => {
  it("从 dev server 取到令牌后，写请求带上它；读请求不带", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ token: TOKEN }));
    await expect(installDevToken(fetchImpl as unknown as typeof fetch)).resolves.toBe(true);
    expect(readInjectedToken()).toBe(TOKEN);
    expect(fetchImpl).toHaveBeenCalledWith(DEV_TOKEN_URL, expect.anything());

    const calls: RequestInit[] = [];
    const client = new ApiClient({
      fetchImpl: (async (_url: string, init: RequestInit) => {
        calls.push(init);
        return jsonResponse({ ok: true });
      }) as unknown as typeof fetch,
    });
    await client.get("/api/v4/surface/schema");
    await client.post("/api/v4/preview", {});

    const headersOf = (init: RequestInit) => (init.headers ?? {}) as Record<string, string>;
    expect(headersOf(calls[0])[TOKEN_HEADER]).toBeUndefined();
    expect(headersOf(calls[1])[TOKEN_HEADER]).toBe(TOKEN);
  });

  it("已有运行时注入值时不再请求（生产路径不受影响）", async () => {
    (window as unknown as Record<string, unknown>)[TOKEN_GLOBAL] = "injected";
    const fetchImpl = vi.fn(async () => jsonResponse({ token: TOKEN }));
    await expect(installDevToken(fetchImpl as unknown as typeof fetch)).resolves.toBe(true);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(readInjectedToken()).toBe("injected");
  });

  it("dev server 不可达时返回 false，且不写入全局变量", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    await expect(installDevToken(fetchImpl as unknown as typeof fetch)).resolves.toBe(false);
    expect(readInjectedToken()).toBeNull();
  });

  it("接口报错或没有令牌时不写入全局变量", async () => {
    for (const response of [jsonResponse({ error: "后端未启动" }, 502), jsonResponse({ token: "" })]) {
      const fetchImpl = vi.fn(async () => response);
      await expect(installDevToken(fetchImpl as unknown as typeof fetch)).resolves.toBe(false);
      expect(readInjectedToken()).toBeNull();
    }
  });
});
