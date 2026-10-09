/**
 * 统一的 HTTP 客户端。
 *
 * 三条约定：
 *
 * 1. **只有写请求带令牌**。`GET` 永远不带 —— 服务端的读接口也不校验它，
 *    少传一个请求头就少一处泄漏面。
 * 2. **令牌不进日志、不进错误信息、不进 URL**。它只出现在请求头里，
 *    而错误信息一律来自服务端错误信封（`AppError`），本模块从不拼接令牌。
 * 3. **支持取消**：预览是「后一次取代前一次」，旧请求必须能被打断，
 *    否则慢响应会把过期结果写回界面。
 */

import { AppError, isAbortError, type ErrorDetail } from "./types";

export const TOKEN_HEADER = "X-Toon-Tuner-Token";

/** 令牌的读取处：只从运行时注入的全局变量取。 */
export const TOKEN_GLOBAL = "__TOON_TUNER_TOKEN__";

declare global {
  interface Window {
    __TOON_TUNER_TOKEN__?: string;
  }
}

export function readInjectedToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  const value = window[TOKEN_GLOBAL];
  return typeof value === "string" && value.length > 0 ? value : null;
}

export type HttpMethod = "GET" | "POST";

export interface RequestOptions {
  method?: HttpMethod;
  body?: unknown;
  signal?: AbortSignal;
  /** 附加查询参数；一律经 `encodeURIComponent`，不拼接裸字符串。 */
  query?: Record<string, string | number | boolean | undefined>;
}

export interface ApiClientOptions {
  baseUrl?: string;
  getToken?: () => string | null;
  fetchImpl?: typeof fetch;
}

function buildUrl(path: string, query: RequestOptions["query"], baseUrl: string): string {
  let url = `${baseUrl}${path}`;
  if (!query) {
    return url;
  }
  const parts: string[] = [];
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) {
      continue;
    }
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  if (parts.length > 0) {
    url += `${url.includes("?") ? "&" : "?"}${parts.join("&")}`;
  }
  return url;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly getToken: () => string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = options.baseUrl ?? "";
    this.getToken = options.getToken ?? readInjectedToken;
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  }

  /** 写请求用；读请求用 `get`。 */
  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method: HttpMethod = options.method ?? "GET";
    const headers: Record<string, string> = { Accept: "application/json" };
    if (method !== "GET") {
      const token = this.getToken();
      if (token) {
        headers[TOKEN_HEADER] = token;
      }
      headers["Content-Type"] = "application/json";
    }
    const init: RequestInit = {
      method,
      headers,
      ...(options.signal ? { signal: options.signal } : {}),
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
    };

    let response: Response;
    try {
      response = await this.fetchImpl(buildUrl(path, options.query, this.baseUrl), init);
    } catch (error) {
      if (isAbortError(error)) {
        throw error;
      }
      throw new AppError(
        {
          code: "NETWORK_ERROR",
          message: "无法连接本地控制服务。请确认服务仍在运行。",
          retryable: true,
        },
        0
      );
    }

    let payload: unknown = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }

    if (!response.ok || (payload !== null && (payload as { ok?: boolean }).ok === false)) {
      const detail = extractError(payload, response.status);
      throw new AppError(detail, response.status);
    }
    if (payload === null) {
      throw new AppError(
        { code: "INTERNAL_ERROR", message: "服务端返回了无法解析的响应。", retryable: true },
        response.status
      );
    }
    return payload as T;
  }

  get<T>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "GET" });
  }

  post<T>(path: string, body?: unknown, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "POST", body: body ?? {} });
  }
}

/**
 * FastAPI 的**请求校验错误**形状：默认是 `{detail: [...]}`（HTTP 422），
 * 少数被自行包装过的路径是顶层数组。两种都要认，否则 422 会被显示成 INTERNAL_ERROR。
 */
function isValidationErrorPayload(payload: unknown): boolean {
  if (Array.isArray(payload)) {
    return true;
  }
  if (payload !== null && typeof payload === "object") {
    return Array.isArray((payload as { detail?: unknown }).detail);
  }
  return false;
}

function extractError(payload: unknown, status: number): Partial<ErrorDetail> {
  const envelope = payload as { error?: Partial<ErrorDetail> } | null;
  if (envelope && typeof envelope === "object" && envelope.error) {
    return envelope.error;
  }
  if (isValidationErrorPayload(payload)) {
    // 形状与错误信封不同，这里统一成稳定结构（含原始 detail，便于排查）。
    return {
      code: "REQUEST_INVALID",
      message: "请求格式不正确（服务端拒绝了该请求体）。",
      retryable: false,
      details: { status, detail: payload },
    };
  }
  return {
    code: status === 401 ? "SESSION_TOKEN_INVALID" : "INTERNAL_ERROR",
    message: `请求失败（HTTP ${status}）。`,
    retryable: status >= 500,
  };
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  return new ApiClient(options);
}

/** 防缓存：预览图 URL 一定带 `v=<job_id>`，否则浏览器会拿旧图。 */
export function previewImageUrl(previewUrl: string, jobId: string): string {
  const separator = previewUrl.includes("?") ? "&" : "?";
  return `${previewUrl}${separator}v=${encodeURIComponent(jobId)}`;
}
