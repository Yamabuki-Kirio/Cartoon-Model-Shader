/**
 * 构建产物校验（`npm run build` 的最后一步）。
 *
 * 令牌是**运行时**注入的：磁盘上的产物里只有占位注释，绝不能出现令牌本身。
 * 这些断言如果只在测试里做，就必须先跑构建才能验；放在构建脚本末尾，
 * 「构建出来的东西能不能直接交给 FastAPI」当场就有结论。
 *
 * 退出码非 0 即构建失败 —— CI 里 `npm run build` 红了，就说明产物不可用。
 *
 * 注意：这里**刻意不 import** `tooling/token-placeholder.ts`。
 * 用 Node 直接加载 TS 依赖实验性的类型擦除，构建守卫不该建立在这个前提上。
 * 两个字面量的一致性由 `tooling/dist-artifacts.test.ts` 的一条防漂移用例钉住。
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const TOKEN_PLACEHOLDER = "<!--TOON_TUNER_TOKEN-->";
const TOKEN_SHAPED = /[A-Za-z0-9_-]{40,}/g;
const HASH = /^[0-9a-f]{40,}$/;

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "..", "dist");

const problems = [];

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full));
    } else {
      out.push(full);
    }
  }
  return out;
}

let files = [];
try {
  files = walk(dist);
} catch (error) {
  console.error(`[verify-dist] 找不到构建产物目录 ${dist}：${error.message}`);
  process.exit(1);
}

const indexPath = join(dist, "index.html");
let html = "";
try {
  html = readFileSync(indexPath, "utf8");
} catch (error) {
  problems.push(`dist/index.html 不存在：${error.message}`);
}

if (html) {
  const occurrences = html.split(TOKEN_PLACEHOLDER).length - 1;
  if (occurrences !== 1) {
    problems.push(`dist/index.html 里令牌占位应恰好出现 1 次，实际 ${occurrences} 次`);
  }
  if (!html.includes('src="/next/assets/') || !html.includes('href="/next/assets/')) {
    problems.push("dist/index.html 的资源引用没有 /next/ 前缀（FastAPI 挂在 /next 下）");
  }
}

for (const file of files) {
  const name = relative(dist, file).replace(/\\/g, "/");
  if (name.endsWith(".map")) {
    problems.push(`${name}：构建产物不应带 sourcemap（会把源码形态串一起带出去）`);
  }
  const hits = (readFileSync(file, "utf8").match(TOKEN_SHAPED) ?? []).filter(
    (item) => !HASH.test(item)
  );
  if (hits.length > 0) {
    problems.push(`${name} 里出现疑似令牌的长随机串（${hits.length} 处，首例长度 ${hits[0].length}）`);
  }
}

if (problems.length > 0) {
  console.error("[verify-dist] 构建产物校验失败：");
  for (const problem of problems) {
    console.error(`  - ${problem}`);
  }
  process.exit(1);
}

console.log(
  `[verify-dist] OK：${files.length} 个产物文件；index.html 含唯一令牌占位；未发现令牌形态串。`
);
