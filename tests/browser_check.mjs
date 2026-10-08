/**
 * 真实浏览器验收：用本机已安装的 Chrome/Edge（CDP 驱动）+ 真实 Blender。
 *
 * 只依赖 Node 内置能力（global fetch / global WebSocket），不需要 puppeteer、
 * 也不需要下载 Chromium。
 *
 * 覆盖验收点：
 *   1. 建立基线后自动渲染首张预览（右侧出现画面）
 *   2. 图片 URL 带 job_id 防缓存
 *   3. 完成后状态为「基线已建立，可开始调参」
 *   4. 拖动曝光滑块能触发新预览，且新图替换旧图
 *   5. 预览图加载失败时显示明确错误，且**不再显示成功状态**
 *
 * 用法：
 *   node tests/browser_check.mjs [baseUrl] [screenshotDir]
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE_URL = process.argv[2] || "http://127.0.0.1:8765";
const SHOT_DIR = process.argv[3] || join(tmpdir(), "toon-tuner-browser-check");

// 从环境变量推导浏览器安装位置，避免在仓库里写死本机绝对路径
const INSTALL_ROOTS = [
  process.env.PROGRAMFILES,
  process.env["PROGRAMFILES(X86)"],
  process.env.LOCALAPPDATA,
].filter(Boolean);

const CANDIDATES = INSTALL_ROOTS.flatMap((root) => [
  `${root}/Google/Chrome/Application/chrome.exe`,
  `${root}/Microsoft/Edge/Application/msedge.exe`,
]);

const DEBUG_PORT = 9333;
const results = [];

function record(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  for (const path of CANDIDATES) {
    if (path && existsSync(path)) return path;
  }
  throw new Error("未找到 Chrome 或 Edge");
}

async function waitForDebugger(port, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return await res.json();
    } catch {
      /* 还没起来 */
    }
    await sleep(200);
  }
  throw new Error("调试端口未就绪");
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      const text = typeof ev.data === "string" ? ev.data : Buffer.from(ev.data).toString("utf8");
      let msg;
      try {
        msg = JSON.parse(text);
      } catch {
        return;
      }
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
    });
  }

  send(method, params = {}, sessionId) {
    const id = ++this.seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`CDP 超时：${method}`));
      }, 180000);
    });
  }
}

async function main() {
  mkdirSync(SHOT_DIR, { recursive: true });
  const profileDir = join(tmpdir(), `toon-tuner-chrome-${Date.now()}`);
  const browserPath = findBrowser();
  console.log(`浏览器：${browserPath}`);
  console.log(`目标页：${BASE_URL}\n`);

  const child = spawn(
    browserPath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--no-proxy-server",
      "--hide-scrollbars",
      "--window-size=1400,2200",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${profileDir}`,
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  let cdp;
  let session;
  try {
    const version = await waitForDebugger(DEBUG_PORT);
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", () => reject(new Error("CDP 连接失败")));
    });
    cdp = new CDP(ws);

    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const attached = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    session = attached.sessionId;

    await cdp.send("Page.enable", {}, session);
    await cdp.send("Runtime.enable", {}, session);
    await cdp.send("Network.enable", {}, session);

    const evaluate = async (expression, awaitPromise = true) => {
      const result = await cdp.send(
        "Runtime.evaluate",
        { expression, awaitPromise, returnByValue: true },
        session
      );
      if (result.exceptionDetails) {
        throw new Error(result.exceptionDetails.text + " :: " + expression.slice(0, 120));
      }
      return result.result.value;
    };
    const shot = async (name) => {
      const { data } = await cdp.send(
        "Page.captureScreenshot",
        { format: "png", captureBeyondViewport: true },
        session
      );
      const file = join(SHOT_DIR, `${name}.png`);
      writeFileSync(file, Buffer.from(data, "base64"));
      return file;
    };

    await cdp.send("Page.navigate", { url: BASE_URL }, session);
    await sleep(500);

    // ---- 1/2/3. 基线 → 首张预览 → 图片出现且 URL 带 job_id ----
    const waitImage = `new Promise((resolve) => {
      const img = document.getElementById("preview-img");
      const status = document.getElementById("preview-status");
      const t0 = Date.now();
      const tick = () => {
        const shown = img && !img.classList.contains("hidden") && img.naturalWidth > 0;
        if (shown) return resolve({ shown: true, src: img.src, status: status.textContent, w: img.naturalWidth, h: img.naturalHeight });
        if (Date.now() - t0 > 90000) return resolve({ shown: false, src: img ? img.src : null, status: status.textContent });
        setTimeout(tick, 120);
      };
      tick();
    })`;

    const first = await evaluate(waitImage);
    await shot("01-baseline-preview");

    record("建立基线后自动渲染首张预览", !!first.shown, first.shown ? `${first.w}×${first.h}` : `状态=${first.status}`);
    if (!first.shown) {
      console.log("\n首张预览未出现，后续用例无意义，提前结束。");
      return;
    }

    const firstJobId = (String(first.src).match(/\/api\/preview\/([^?]+)\?v=([^&]+)/) || [])[2];
    record("图片 URL 带 job_id 防缓存", !!firstJobId, String(first.src).replace(BASE_URL, ""));
    record(
      "图片经 HTTP 文件端点提供（非本机临时路径）",
      String(first.src).startsWith(BASE_URL + "/api/preview/"),
      String(first.src).replace(BASE_URL, "")
    );
    record("首张完成后显示「可开始调参」", first.status.includes("基线已建立，可开始调参"), first.status);

    // ---- 4. 曝光滑块触发新预览，新图替换旧图 ----
    const moved = await evaluate(`(() => {
      const input = document.getElementById("ctl-color.exposure");
      if (!input) return { ok: false, reason: "未找到曝光滑块" };
      input.value = "1.25";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return { ok: true, value: input.value };
    })()`);
    record("找到曝光滑块并触发 input", !!moved.ok, moved.ok ? `exposure=${moved.value}` : moved.reason);

    const waitNewImage = `new Promise((resolve) => {
      const img = document.getElementById("preview-img");
      const status = document.getElementById("preview-status");
      const t0 = Date.now();
      const tick = () => {
        const shown = img && !img.classList.contains("hidden") && img.naturalWidth > 0;
        const id = (String(img.src).match(/[?&]v=([^&]+)/) || [])[1] || null;
        if (shown && id && id !== ${JSON.stringify(firstJobId)}) {
          return resolve({ shown: true, src: img.src, status: status.textContent, jobId: id });
        }
        if (Date.now() - t0 > 90000) return resolve({ shown: false, src: img.src, status: status.textContent, jobId: id });
        setTimeout(tick, 120);
      };
      tick();
    })`;
    const second = await evaluate(waitNewImage);
    await shot("02-slider-preview");
    record(
      "拖动滑块自动预览并替换旧图",
      !!second.shown && second.jobId !== firstJobId,
      `status=${second.status}`
    );

    // ---- 5. 图片加载失败：必须报错，且不得显示成功 ----
    await cdp.send(
      "Network.setBlockedURLs",
      { urls: ["*://*/api/preview/*"] },
      session
    );
    await evaluate(`(() => { document.getElementById("baseline-btn").click(); return true; })()`);

    const waitFailure = `new Promise((resolve) => {
      const status = document.getElementById("preview-status");
      const t0 = Date.now();
      const tick = () => {
        const text = status.textContent || "";
        if (text.includes("预览失败") || text.includes("无法加载")) {
          return resolve({ text, imgHidden: document.getElementById("preview-img").classList.contains("hidden") });
        }
        if (Date.now() - t0 > 90000) return resolve({ text, imgHidden: document.getElementById("preview-img").classList.contains("hidden") });
        setTimeout(tick, 120);
      };
      tick();
    })`;
    const failed = await evaluate(waitFailure);
    await shot("03-preview-load-failed");
    record(
      "预览图加载失败时显示明确错误",
      failed.text.includes("预览失败") || failed.text.includes("无法加载"),
      failed.text
    );
    record(
      "加载失败时不再显示成功状态",
      !failed.text.includes("可开始调参") && !failed.text.includes("预览完成"),
      failed.text
    );

    // ---- 恢复阻断，确认页面可自愈 ----
    await cdp.send("Network.setBlockedURLs", { urls: [] }, session);
    await evaluate(`(() => { document.getElementById("baseline-btn").click(); return true; })()`);
    const recovered = await evaluate(waitImage);
    await shot("04-recovered");
    record("解除阻断后可重新拿到基线预览", !!recovered.shown, recovered.status);
  } finally {
    try {
      if (cdp && session) await cdp.send("Browser.close");
    } catch {
      /* ignore */
    }
    child.kill();
    await sleep(400);
    try {
      rmSync(profileDir, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
}

main()
  .then(() => {
    const failed = results.filter((r) => !r.ok);
    console.log(`\n截图目录：${SHOT_DIR}`);
    console.log(`结果：${results.length - failed.length}/${results.length} 通过`);
    process.exit(failed.length === 0 ? 0 : 1);
  })
  .catch((err) => {
    console.error("浏览器验收失败：", err);
    process.exit(2);
  });
