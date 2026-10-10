import type { Plugin } from "vite";

/**
 * 会话令牌占位。
 *
 * FastAPI 在**响应时**把 `<!--TOON_TUNER_TOKEN-->` 换成真正的令牌脚本，
 * 因此磁盘上的构建产物里永远没有令牌。但 HTML 压缩器会把注释吃掉 ——
 * 一旦占位消失，`GET /next` 注入失败、**所有写操作 401**。
 *
 * 所以占位必须在 `transformIndexHtml` 的 `post` 阶段重新插一次，
 * 而不是指望源文件里的注释活下来。
 *
 * 单独成模块（而不是写在 `vite.config.ts` 里）有两个原因：
 * 一是测试可以直接断言这段逻辑，不必把整个 Vite 拉进运行时；
 * 二是 `import type` 在编译后被抹掉，因此这个模块没有任何运行时依赖。
 */

export const TOKEN_PLACEHOLDER = "<!--TOON_TUNER_TOKEN-->";

export function tokenPlaceholderPlugin(placeholder: string = TOKEN_PLACEHOLDER): Plugin {
  return {
    name: "toon-tuner:token-placeholder",
    transformIndexHtml: {
      order: "post",
      handler(html: string): string {
        if (html.includes(placeholder)) {
          return html;
        }
        if (html.includes("</head>")) {
          return html.replace("</head>", `${placeholder}\n</head>`);
        }
        return `${placeholder}\n${html}`;
      },
    },
  };
}

/** 产物里禁止出现的形态：40+ 连续 base64url 字符（会话令牌是 32 字节 urlsafe base64 ≈ 43 字符）。 */
export const TOKEN_SHAPED = /[A-Za-z0-9_-]{40,}/g;

/**
 * 纯小写十六进制长串视为内容哈希（构建产物的文件名 / 指纹），不是令牌。
 *
 * 这里的取舍是**宁可误报也不漏报**：判据只放行「整串都是小写十六进制」这一种情况。
 * 例如 `index-<64 位十六进制>` 这种「标识符 + 短横 + 长哈希」会被一起吃掉、从而误报，
 * 那是可以接受的 —— 构建失败一次、人看一眼即可；漏掉一个真令牌则不可接受。
 */
export function looksLikeHash(candidate: string): boolean {
  return /^[0-9a-f]{40,}$/.test(candidate);
}

/** 在文本里找疑似令牌的长随机串。 */
export function findTokenShaped(text: string): string[] {
  return (text.match(TOKEN_SHAPED) ?? []).filter((item) => !looksLikeHash(item));
}
