/**
 * 仅开发：把 Vite dev server 代取的会话令牌装进**与生产同一个**全局变量。
 *
 * 关键点是「同一个来源」：前端自始至终只有 `window.__TOON_TUNER_TOKEN__` 一处读令牌
 * （见 `client.ts::readInjectedToken`）。开发期不新增第二条读取路径，
 * 否则「生产 401」这类问题在开发时永远暴露不出来。
 *
 * 这个模块只在 `import.meta.env.DEV` 为真时被动态导入，因此不会进入生产产物；
 * 它本身也不含任何令牌值。
 */

import { readInjectedToken, TOKEN_GLOBAL } from "./client";

/** 开发期令牌接口；与 `tooling/dev-token-bridge.ts` 的 `DEV_TOKEN_PATH` 必须一致。 */
export const DEV_TOKEN_URL = "/__dev/token";

export async function installDevToken(
  fetchImpl: typeof fetch = (...args) => fetch(...args)
): Promise<boolean> {
  if (readInjectedToken()) {
    // 生产（或已装过）：不覆盖运行时注入的值。
    return true;
  }
  try {
    const response = await fetchImpl(DEV_TOKEN_URL, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      return false;
    }
    const body = (await response.json()) as { token?: unknown };
    if (typeof body.token !== "string" || body.token.length === 0) {
      return false;
    }
    (window as unknown as Record<string, unknown>)[TOKEN_GLOBAL] = body.token;
    return true;
  } catch {
    // 取不到令牌不该让页面白屏：照常渲染，写操作会 401 并在横幅里给出原因。
    return false;
  }
}
