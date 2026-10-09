import { describe, expect, it, vi } from "vitest";
import { ApiClient, TOKEN_HEADER, previewImageUrl, readInjectedToken } from "./client";
import { AppError, isAbortError } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeClient(fetchImpl: typeof fetch, token: string | null = "tok-123") {
  return new ApiClient({ fetchImpl, getToken: () => token });
}

describe("API 客户端：令牌与错误", () => {
  it("写请求带令牌，读请求不带", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const fake = (async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), init });
      return jsonResponse({ ok: true, job_id: "j1", seq: 1, status: "queued" });
    }) as unknown as typeof fetch;
    const client = makeClient(fake);

    await client.get("/api/v4/surface/schema");
    await client.post("/api/v4/preview", { draft: {} });

    const headersOf = (init: RequestInit) => (init.headers ?? {}) as Record<string, string>;
    expect(calls[0].init.method).toBe("GET");
    expect(headersOf(calls[0].init)[TOKEN_HEADER]).toBeUndefined();
    expect(calls[1].init.method).toBe("POST");
    expect(headersOf(calls[1].init)[TOKEN_HEADER]).toBe("tok-123");
    expect(calls[1].init.body).toBe(JSON.stringify({ draft: {} }));
  });

  it("没有令牌时不发空令牌头（服务端会明确 401，而不是被空串掩盖）", async () => {
    let seen: Record<string, string> = {};
    const fake = (async (_url: string, init: RequestInit) => {
      seen = (init.headers ?? {}) as Record<string, string>;
      return jsonResponse({ ok: true });
    }) as unknown as typeof fetch;
    const client = makeClient(fake, null);
    await client.post("/api/v4/preview", {});
    expect(seen[TOKEN_HEADER]).toBeUndefined();
  });

  it("查询参数一律编码，不做裸拼接", async () => {
    let url = "";
    const fake = (async (target: string) => {
      url = String(target);
      return jsonResponse({ ok: true });
    }) as unknown as typeof fetch;
    await makeClient(fake).get("/api/x", { query: { view: "AgX - High Contrast", n: 3, skip: undefined } });
    expect(url).toContain("view=AgX%20-%20High%20Contrast");
    expect(url).toContain("n=3");
    expect(url).not.toContain("skip");
  });

  it("服务端错误信封被翻译成 AppError（含 details 与 hint）", async () => {
    const fake = (async () =>
      jsonResponse(
        {
          ok: false,
          error: {
            code: "STRUCTURE_CHANGED",
            message: "工程结构已变化",
            retryable: true,
            hint: "刷新基线",
            details: { structure_changed: ["Cel_Skin"] },
          },
        },
        409
      )) as unknown as typeof fetch;

    await expect(makeClient(fake).get("/api/v4/session/baseline")).rejects.toMatchObject({
      code: "STRUCTURE_CHANGED",
      status: 409,
      retryable: true,
      hint: "刷新基线",
    });
    try {
      await makeClient(fake).get("/api/v4/session/baseline");
    } catch (error) {
      const appError = error as AppError;
      expect(appError.isStructureFatal).toBe(true);
      expect(appError.details).toEqual({ structure_changed: ["Cel_Skin"] });
    }
  });

  it("IDENTITY_MISSING 也归入结构致命类", () => {
    const error = new AppError({ code: "IDENTITY_MISSING", message: "x", retryable: true }, 409);
    expect(error.isStructureFatal).toBe(true);
    expect(new AppError({ code: "PARAM_INVALID", message: "y" }).isStructureFatal).toBe(false);
  });

  it("401 / 502 都会抛出（不会被当成成功）", async () => {
    for (const status of [401, 502]) {
      const fake = (async () =>
        jsonResponse({ ok: false, error: { code: "X", message: "boom", retryable: true } }, status)) as unknown as typeof fetch;
      await expect(makeClient(fake).post("/api/v4/preview", {})).rejects.toBeInstanceOf(AppError);
    }
  });

  it("FastAPI 的 422 数组响应被统一成 REQUEST_INVALID", async () => {
    const fake = (async () => jsonResponse([{ loc: ["body"], msg: "extra" }], 422)) as unknown as typeof fetch;
    await expect(makeClient(fake).post("/api/v4/preview", {})).rejects.toMatchObject({
      code: "REQUEST_INVALID",
      status: 422,
      retryable: false,
    });
  });

  it("网络异常翻译成 NETWORK_ERROR（不是静默成功）", async () => {
    const fake = (async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch;
    await expect(makeClient(fake).get("/api/health")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  });

  it("取消原样抛出（由调用方判断是取消而不是失败）", async () => {
    const fake = (async () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      throw error;
    }) as unknown as typeof fetch;
    try {
      await makeClient(fake).get("/api/health");
      throw new Error("应当抛出");
    } catch (error) {
      expect(isAbortError(error)).toBe(true);
      expect(error).not.toBeInstanceOf(AppError);
    }
  });

  it("响应不是 JSON 时报错而不是猜内容", async () => {
    const fake = (async () => new Response("<html>", { status: 200 })) as unknown as typeof fetch;
    await expect(makeClient(fake).get("/api/health")).rejects.toMatchObject({ code: "INTERNAL_ERROR" });
  });

  it("令牌不进 URL / 不代表错误信息", async () => {
    let url = "";
    const fake = (async () =>
      jsonResponse({ ok: false, error: { code: "SESSION_TOKEN_INVALID", message: "写接口需要有效的本机会话令牌。", retryable: false } }, 401)) as unknown as typeof fetch;
    const client = new ApiClient({
      fetchImpl: (async (target: string, init: RequestInit) => {
        url = String(target);
        return (fake as unknown as (a: string, b: RequestInit) => Promise<Response>)(target, init);
      }) as unknown as typeof fetch,
      getToken: () => "super-secret-token",
    });
    try {
      await client.post("/api/v4/preview", {});
    } catch (error) {
      expect(String((error as Error).message)).not.toContain("super-secret-token");
    }
    expect(url).not.toContain("super-secret-token");
  });
});

describe("运行时令牌读取", () => {
  it("从 window 上的注入值读取", () => {
    (globalThis as unknown as { window: Window }).window.__TOON_TUNER_TOKEN__ = "abc";
    expect(readInjectedToken()).toBe("abc");
    (globalThis as unknown as { window: Window }).window.__TOON_TUNER_TOKEN__ = "";
    expect(readInjectedToken()).toBeNull();
    delete (globalThis as unknown as { window: Window }).window.__TOON_TUNER_TOKEN__;
    expect(readInjectedToken()).toBeNull();
  });
});

describe("预览图防缓存", () => {
  it("URL 追加 v=<job_id>", () => {
    expect(previewImageUrl("/api/preview/abc", "abc")).toBe("/api/preview/abc?v=abc");
    expect(previewImageUrl("/api/preview/abc?x=1", "abc")).toBe("/api/preview/abc?x=1&v=abc");
  });
});

describe("默认注入 fetch 的路径", () => {
  it("未传 fetchImpl 时用全局 fetch", async () => {
    const spy = vi.fn(async () => jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", spy);
    await new ApiClient({ getToken: () => null }).get("/api/health");
    expect(spy).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
