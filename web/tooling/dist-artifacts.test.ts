// @vitest-environment node
/**
 * 构建产物与「仓库卫生」断言。
 *
 * 这些用例做的是**文件系统 / git** 检查，所以跑在 node 环境（jsdom 会替换全局对象，
 * 而构建工具链依赖 `TextEncoder`/`Uint8Array` 的原始一致）。
 *
 * `npm run build` 末尾有 `scripts/verify-dist.mjs` 兜底；这里再断言一次，
 * 是为了让「产物里有没有令牌」出现在测试报告里，而不是只留在构建日志里。
 * 未构建时相关用例跳过（CI 的 Python job 不要求 Node）。
 */
import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { TOKEN_PLACEHOLDER, findTokenShaped, tokenPlaceholderPlugin } from "./token-placeholder";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, "..");
const repoRoot = join(webRoot, "..");

describe("令牌占位（Vite 插件）", () => {
  const plugin = tokenPlaceholderPlugin();
  const handler = (plugin.transformIndexHtml as { handler: (html: string) => string }).handler;

  it("源 HTML 已含占位时原样返回（不重复插入）", () => {
    const html = `<head>${TOKEN_PLACEHOLDER}</head>`;
    expect(handler(html)).toBe(html);
    expect(handler(html).split(TOKEN_PLACEHOLDER).length - 1).toBe(1);
  });

  it("压缩器吃掉注释后重新插回 </head> 之前", () => {
    const minified = "<html><head><title>x</title></head><body></body></html>";
    const out = handler(minified);
    expect(out.split(TOKEN_PLACEHOLDER).length - 1).toBe(1);
    expect(out.indexOf(TOKEN_PLACEHOLDER)).toBeLessThan(out.indexOf("</head>"));
  });

  it("连 </head> 都没有时插到最前面（注入点永不丢失）", () => {
    expect(handler("<div>裸片段</div>").startsWith(TOKEN_PLACEHOLDER)).toBe(true);
  });

  it("在 post 阶段执行（早于它的压缩可能把注释吃掉）", () => {
    expect((plugin.transformIndexHtml as { order: string }).order).toBe("post");
  });

  it("令牌形态判据：真令牌命中，内容哈希放行", () => {
    // 真令牌：43 字符左右的混合大小写 + 数字。
    // 刻意在**运行时**拼出来 —— 否则下面那条「源码不含令牌」的扫描会抓到本文件自己。
    const token = ["aB3xY9", "_zQ7wE", "rT1uIo", "P2aSdF", "4gH6jK", "8lM0nB", "2vC4xD", "6yZ8"].join("");
    expect(token.length).toBeGreaterThanOrEqual(40);
    expect(findTokenShaped(`window.__TOON_TUNER_TOKEN__ = "${token}";`)).toEqual([token]);

    // 纯小写十六进制长串 = 内容哈希
    expect(findTokenShaped(`"${"0".repeat(64)}"`)).toEqual([]);

    // Vite 实际产出的文件名形态（8 位哈希）本来就不构成 40+ 连续串
    expect(findTokenShaped("assets/index-Db4q6b91.js")).toEqual([]);

    // 已知取舍：**长**哈希前面粘着标识符时会被一起吃掉而误报。
    // 这是刻意的（宁可误报也不漏报），用例把它固定下来，免得日后有人「顺手放宽」。
    expect(findTokenShaped(`assets/index-${"0".repeat(64)}.js`)).toHaveLength(1);
  });

  it("防漂移：构建守卫脚本与插件用的是同一个占位与同一套判据", () => {
    const script = readFileSync(join(webRoot, "scripts", "verify-dist.mjs"), "utf8");
    // 脚本刻意不 import TS（Node 加载 TS 依赖实验性类型擦除），因此这里钉住一致性。
    expect(script).toContain(`"${TOKEN_PLACEHOLDER}"`);
    expect(script).toContain("/next/assets/");
    expect(script).toContain("sourcemap");
  });
});

function listFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...listFiles(full));
    } else {
      out.push(full);
    }
  }
  return out;
}

const dist = join(webRoot, "dist");
let distFiles: string[] = [];
try {
  distFiles = listFiles(dist);
} catch {
  distFiles = [];
}
const built = distFiles.length > 0;

describe("构建产物", () => {
  it.skipIf(!built)("dist/index.html 含唯一令牌占位", () => {
    const html = readFileSync(join(dist, "index.html"), "utf8");
    expect(html.split(TOKEN_PLACEHOLDER).length - 1).toBe(1);
  });

  it.skipIf(!built)("dist 内不含令牌形态的随机串", () => {
    for (const file of distFiles) {
      const name = relative(dist, file).replace(/\\/g, "/");
      const hits = findTokenShaped(readFileSync(file, "utf8"));
      expect(hits, `${name} 里出现了疑似令牌的长随机串`).toEqual([]);
    }
  });

  it.skipIf(!built)("dist 里没有 sourcemap（避免把源码形态串一起带出去）", () => {
    expect(distFiles.filter((file) => file.endsWith(".map"))).toEqual([]);
  });

  it.skipIf(!built)("资源引用带 /next/ 前缀（由 FastAPI 托管）", () => {
    const html = readFileSync(join(dist, "index.html"), "utf8");
    expect(html).toContain('src="/next/assets/');
    expect(html).toContain('href="/next/assets/');
  });
});

describe("源码不含令牌", () => {
  it("web/ 下没有令牌形态的长随机串", () => {
    const sources = [
      ...listFiles(join(webRoot, "src")),
      ...listFiles(join(webRoot, "tooling")),
      join(webRoot, "index.html"),
    ].filter((file) => /\.(ts|tsx|css|html)$/.test(file));

    for (const file of sources) {
      const hits = findTokenShaped(readFileSync(file, "utf8")).filter(
        (item) => !item.startsWith("job") && !item.startsWith("bl")
      );
      expect(hits, `${relative(webRoot, file)} 里出现了疑似令牌的长随机串`).toEqual([]);
    }
  });
});

/**
 * 仓库卫生：`web/` 下待提交的文件**不得被 .gitignore 忽略**。
 *
 * 这条用例来自一次真实的踩坑：`.gitignore` 里的 `build/` 规则把 `web/build/`
 * 与 `web/src/build/` 一并忽略了 —— 那两个目录里的文件会**静默地不进仓库**，
 * 本地一切正常，别人拉下来就缺文件。名字越像常见构建产物，越容易中招。
 */
describe("仓库卫生", () => {
  it("web/ 下的源码与配置都没有被 .gitignore 忽略", () => {
    const candidates = [...listFiles(join(webRoot, "src")), ...listFiles(join(webRoot, "tooling"))]
      .map((file) => relative(repoRoot, file).replace(/\\/g, "/"))
      .concat(
        [
          "index.html",
          "package.json",
          "package-lock.json",
          "vite.config.ts",
          "vitest.config.ts",
          "tsconfig.json",
          "scripts/verify-dist.mjs",
        ].map((name) => `web/${name}`)
      );

    let stdout = "";
    try {
      stdout = execFileSync("git", ["check-ignore", "--stdin"], {
        cwd: repoRoot,
        input: candidates.join("\n"),
        encoding: "utf8",
      });
    } catch (error) {
      // git check-ignore 在「一条都没忽略」时退出码为 1 —— 那正是我们想要的
      const failure = error as { status?: number; stdout?: string };
      expect(failure.status).toBe(1);
      stdout = failure.stdout ?? "";
    }
    const ignored = stdout.trim().split("\n").filter(Boolean);
    expect(ignored, "以下文件被 .gitignore 忽略了").toEqual([]);
  });
});
