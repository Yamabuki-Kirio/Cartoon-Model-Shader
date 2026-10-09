/**
 * 真实浏览器验收：用本机已安装的 Chrome/Edge（CDP 驱动）+ 真实 Blender。
 *
 * 只依赖 Node 内置能力（global fetch / global WebSocket / node:net），不需要 puppeteer、
 * 也不需要下载 Chromium。
 *
 * 覆盖验收点：
 *   1. 建立基线后自动渲染首张预览（右侧出现画面）
 *   2. 图片 URL 带 job_id 防缓存
 *   3. 完成后状态为「基线已建立，可开始调参」
 *   4. 拖动曝光滑块能触发新预览，且新图替换旧图
 *   5. 预览图加载失败时显示明确错误，且**不再显示成功状态**
 *   6. 顶部取景栏：当前帧 / 当前相机 / 相机是否有动画 / 角色是否完整入画
 *   7. 当前相机预览 vs 临时自动取景的标识
 *   8. 「重新取景」后角色完整入画，且**用户原相机一个字段都没变**
 *   9. 用户切帧后：自动预览停止、出现「请刷新基线」，刷新后恢复
 *  10. 依赖枚举：Look 下拉框的 value 取自 Blender 真实 identifier、
 *      显示文本取自 label（逐项与后端能力表比对）
 *  11. 切换视图变换后 Look 列表立即刷新，旧值按规范化名称迁移
 *      （"AgX - High Contrast" ⇄ "High Contrast"），迁移后给出说明文案
 *  12. 切换视图后完整草稿可正常预览，不再出现 BLENDER_SCRIPT_ERROR；
 *      任务记录里 effective_value 是真实 identifier，且 display_label 独立保存
 *  13. 快速连续切换视图变换后，Look 列表对应**最后一次**选择（旧结果不覆盖）
 *  14. 非法 Look/视图组合返回 INVALID_DEPENDENT_ENUM（不退化成脚本错误），
 *      且前端把「哪个参数 / 依赖谁 / 允许什么」摊开显示
 *
 * 用法：
 *   node tests/browser_check.mjs [baseUrl] [screenshotDir]
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE_URL = process.argv[2] || "http://127.0.0.1:8765";
const SHOT_DIR = process.argv[3] || join(tmpdir(), "toon-tuner-browser-check");
// Blender MCP 的固定端口（与 config 默认一致）；仅用于「读相机状态 / 切帧」这两件事
const MCP_HOST = process.env.TOON_TUNER_MCP_HOST || "127.0.0.1";
const MCP_PORT = Number(process.env.TOON_TUNER_MCP_PORT || 9876);

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

/**
 * 直接连 Blender MCP 执行一小段代码。
 * 仅用于两件事：读取相机/帧状态、把当前帧切到别处（验收「基线失效」）。
 * 切帧是对用户工程**临时**的改动，脚本结束前会恢复原帧。
 */
function mcpExecute(code, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: MCP_HOST, port: MCP_PORT });
    let buffer = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        sock.destroy();
        reject(new Error("Blender MCP 超时"));
      }
    }, timeoutMs);
    sock.on("connect", () => {
      sock.write(JSON.stringify({ type: "execute_code", params: { code } }) + "\n");
    });
    sock.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      try {
        const parsed = JSON.parse(buffer);
        settled = true;
        clearTimeout(timer);
        sock.end();
        resolve(parsed);
      } catch {
        /* 响应无换行分隔符，继续累积 */
      }
    });
    sock.on("error", (err) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(err);
      }
    });
  });
}

const CAMERA_STATE_CODE = `
import json
import bpy

scene = bpy.context.scene
cam = scene.camera
print("__CAM_STATE__" + json.dumps({
    "frame": scene.frame_current,
    "camera": cam.name if cam is not None else None,
    "location": [round(float(v), 6) for v in cam.location] if cam is not None else None,
    "world": [[round(float(v), 6) for v in row] for row in cam.matrix_world] if cam is not None else None,
    "rotation": [round(float(v), 6) for v in cam.rotation_euler] if cam is not None else None,
    "lens": round(float(cam.data.lens), 6) if cam is not None else None,
    "shift": [round(float(cam.data.shift_x), 6), round(float(cam.data.shift_y), 6)] if cam is not None else None,
    "temporary_cameras": [o.name for o in bpy.data.objects if o.name.startswith("__TOON_TUNER_PREVIEW_CAM__")],
    "camera_count": len([o for o in scene.objects if o.type == "CAMERA"]),
}, ensure_ascii=False))
`;

async function readCameraState() {
  const response = await mcpExecute(CAMERA_STATE_CODE);
  const stdout = (response && response.result && response.result.result) || "";
  const line = stdout.split("\n").find((l) => l.startsWith("__CAM_STATE__"));
  if (!line) throw new Error("拿不到相机状态：" + stdout.slice(0, 200));
  return JSON.parse(line.slice("__CAM_STATE__".length));
}

async function setFrame(frame) {
  const response = await mcpExecute(
    `import bpy\nbpy.context.scene.frame_set(${Number(frame)})\nprint("__FRAME__" + str(bpy.context.scene.frame_current))\n`
  );
  const stdout = (response && response.result && response.result.result) || "";
  if (!stdout.includes("__FRAME__")) throw new Error("切帧失败：" + stdout.slice(0, 200));
  return stdout;
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
  let originalFrame = null;
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

    // ================= 取景（构图）=====================================
    const readBar = `(() => {
      const bar = document.getElementById("framing-bar");
      const txt = (id) => (document.getElementById(id) || {}).textContent || "";
      return {
        visible: !!bar && !bar.classList.contains("hidden"),
        frame: txt("framing-frame"),
        camera: txt("framing-camera"),
        anim: txt("framing-camera-anim"),
        inside: txt("framing-inside"),
        source: txt("framing-source"),
        fitMessage: txt("framing-fit-message"),
        label: txt("preview-framing-label"),
        mode: (document.getElementById("framing-mode") || {}).value,
        margin: (document.getElementById("framing-margin") || {}).value,
        staleShown: (() => { const b = document.getElementById("framing-stale"); return !!b && !b.classList.contains("hidden"); })(),
        staleText: txt("framing-stale"),
        reframeDisabled: (document.getElementById("reframe-btn") || {}).disabled === true,
        previewJobId: (String(document.getElementById("preview-img").src).match(/[?&]v=([^&]+)/) || [])[1] || null,
        statusText: txt("preview-status"),
      };
    })()`;

    // ---- 6. 顶部取景栏四项信息 ----
    const bar = await evaluate(readBar);
    await shot("05-framing-bar");
    record("顶部取景栏可见", !!bar.visible, bar.visible ? "" : "未显示");
    record("显示当前帧", /\d+/.test(bar.frame), bar.frame);
    record("显示当前相机", bar.camera.length > 1 && bar.camera !== "—", bar.camera);
    record("显示相机是否有动画", /有动画|无/.test(bar.anim), bar.anim);
    record("显示角色是否完整入画", /是|否/.test(bar.inside), bar.inside);
    record(
      "默认取景方式为「当前相机」并明确标识",
      bar.mode === "current_camera" && bar.label.includes("当前相机预览"),
      `${bar.mode} / ${bar.label}`
    );
    record("相机带动画时给出告警文案", bar.fitMessage.includes("动画"), bar.fitMessage.slice(0, 120));

    // ---- 8. 重新取景：角色入画，且用户原相机一个字段都不变 ----
    const camBefore = await readCameraState();
    originalFrame = camBefore.frame;
    record(
      "重新取景前：无残留临时相机",
      camBefore.temporary_cameras.length === 0,
      `${camBefore.camera} / 相机数=${camBefore.camera_count}`
    );
    const jobBefore = bar.previewJobId;

    await evaluate(`(() => {
      const mode = document.getElementById("framing-mode");
      mode.value = "auto_full_body";
      mode.dispatchEvent(new Event("change", { bubbles: true }));
      return mode.value;
    })()`);
    await sleep(500);
    await evaluate(`(() => { document.getElementById("reframe-btn").click(); return true; })()`);

    const waitAutoFramed = `new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        const img = document.getElementById("preview-img");
        const label = document.getElementById("preview-framing-label");
        const id = (String(img.src).match(/[?&]v=([^&]+)/) || [])[1] || null;
        const shown = img && !img.classList.contains("hidden") && img.naturalWidth > 0;
        if (shown && id && id !== ${JSON.stringify(jobBefore)} && label.textContent.includes("临时自动取景")) {
          return resolve({ ok: true, id: id, label: label.textContent, status: document.getElementById("preview-status").textContent });
        }
        if (Date.now() - t0 > 120000) {
          return resolve({ ok: false, id: id, label: label.textContent, status: document.getElementById("preview-status").textContent });
        }
        setTimeout(tick, 150);
      };
      tick();
    })`;
    const auto = await evaluate(waitAutoFramed);
    await shot("06-auto-full-body");
    record("自动全身取景产出新画面", !!auto.ok, `label=${auto.label} / ${auto.status}`);

    const barAfter = await evaluate(readBar);
    record(
      "明确标识为「临时自动取景」且注明不改动用户相机",
      barAfter.label.includes("临时自动取景") && barAfter.label.includes("不改动用户相机"),
      barAfter.label
    );
    record("自动取景后角色完整入画", barAfter.inside.includes("是"), `${barAfter.inside} ｜ ${barAfter.fitMessage.slice(0, 120)}`);
    record(
      "入画判据明确标注参照的是临时取景相机",
      barAfter.inside.includes("按临时取景相机"),
      barAfter.inside
    );
    // 顶部「当前相机」描述的是场景状态：临时相机渲染完即销毁，这里必须仍显示工程相机，
    // 不能显示 __TOON_TUNER_PREVIEW_CAM__，否则用户会误以为场景相机被换掉了。
    record(
      "自动取景后顶部「当前相机」仍是工程相机",
      barAfter.camera === camBefore.camera && !barAfter.camera.includes("__TOON_TUNER_PREVIEW_CAM__"),
      `${camBefore.camera} → ${barAfter.camera}`
    );
    record(
      "说明文案点名了本次使用的临时相机与已恢复的原相机",
      barAfter.fitMessage.includes("临时预览相机") && barAfter.fitMessage.includes(camBefore.camera),
      barAfter.fitMessage.slice(0, 200)
    );

    const camAfter = await readCameraState();
    // 该相机带关键帧（且带父级），本地 location 与基线记录的 matrix_world 不在同一空间，
    // 因此不可变性断言必须以世界矩阵为准（需求 5「不修改原相机」）。
    const sameCamera =
      camAfter.camera === camBefore.camera &&
      camAfter.frame === camBefore.frame &&
      JSON.stringify(camAfter.world) === JSON.stringify(camBefore.world) &&
      camAfter.lens === camBefore.lens &&
      JSON.stringify(camAfter.shift) === JSON.stringify(camBefore.shift);
    record("自动取景后用户原相机未被改动（世界变换/焦距/shift）", sameCamera,
      `before=${JSON.stringify(camBefore.world)} lens=${camBefore.lens} shift=${JSON.stringify(camBefore.shift)}`);
    record("自动取景后当前帧未被改动", camAfter.frame === camBefore.frame, `${camBefore.frame} → ${camAfter.frame}`);
    record("临时预览相机已清理，没有残留", camAfter.temporary_cameras.length === 0,
      `残留=${camAfter.temporary_cameras.length}`);
    record("相机数量未变（没有多出对象）", camAfter.camera_count === camBefore.camera_count,
      `${camBefore.camera_count} → ${camAfter.camera_count}`);

    // 状态轮询每 5s 会重新拉一次 /api/framing/context；自动取景模式下它**不得**用
    // 「当前相机」的诊断覆盖掉实际渲染结果（否则会出现判据与画面互相打架）。
    await sleep(7000);
    const barSettled = await evaluate(readBar);
    record(
      "轮询一轮后，判据仍是自动取景的结果（未被当前相机诊断覆盖）",
      barSettled.inside.includes("按临时取景相机") && barSettled.inside.includes("是"),
      `${barSettled.inside} ｜ ${barSettled.camera}`
    );
    record(
      "轮询一轮后，顶部「当前相机」仍是工程相机",
      barSettled.camera === camBefore.camera,
      `${camBefore.camera} → ${barSettled.camera}`
    );

    // ---- 9. 用户切帧 → 自动预览停止 + 提示刷新基线 ----
    await setFrame(Number(camBefore.frame) + 435);
    const stale = await evaluate(`new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        const box = document.getElementById("framing-stale");
        const shown = box && !box.classList.contains("hidden");
        const id = (String(document.getElementById("preview-img").src).match(/[?&]v=([^&]+)/) || [])[1] || null;
        if (shown) {
          return resolve({
            shown: true, id: id, banner: box.textContent,
            reframeDisabled: document.getElementById("reframe-btn").disabled === true,
            status: document.getElementById("preview-status").textContent,
          });
        }
        if (Date.now() - t0 > 40000) {
          return resolve({ shown: false, id: id, banner: box ? box.textContent : "", reframeDisabled: false, status: document.getElementById("preview-status").textContent });
        }
        setTimeout(tick, 200);
      };
      tick();
    })`);
    await shot("07-framing-stale");
    record("切帧后出现失效横幅", !!stale.shown, stale.banner.slice(0, 140));
    record("横幅提示「请刷新基线」", stale.banner.includes("请刷新基线"), stale.banner.slice(0, 80));
    record("失效时停止自动预览", stale.status.includes("自动预览已停止"), stale.status);
    record("失效时「重新取景」被禁用", !!stale.reframeDisabled, String(stale.reframeDisabled));
    record("失效期间画面没有被新构图顶替", stale.id === auto.id, `${auto.id} → ${stale.id}`);

    // ---- 恢复：把帧还原 → 刷新基线 → 重新拿到预览 ----
    await setFrame(camBefore.frame);
    await evaluate(`(() => { document.getElementById("framing-refresh-baseline-btn").click(); return true; })()`);
    const clean = await evaluate(`new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        const box = document.getElementById("framing-stale");
        const img = document.getElementById("preview-img");
        const text = document.getElementById("preview-status").textContent || "";
        /* 必须等到「可开始调参」：刷新基线是异步的，captureBaseline 末尾还会
           applyValuesToControls(基线值)。提前返回会让后一步的断言读到正在被重置的界面。 */
        const shown = box && box.classList.contains("hidden") &&
          img && !img.classList.contains("hidden") && img.naturalWidth > 0 &&
          text.includes("可开始调参");
        if (shown) return resolve({ ok: true, status: text });
        if (Date.now() - t0 > 120000) return resolve({ ok: false, status: text });
        setTimeout(tick, 150);
      };
      tick();
    })`);
    await shot("08-refreshed");
    record("还原帧并刷新基线后可继续预览", !!clean.ok, clean.status);
    const camFinal = await readCameraState();
    record("整轮验收结束后原相机仍然完好", camFinal.camera === camBefore.camera &&
      JSON.stringify(camFinal.world) === JSON.stringify(camBefore.world) &&
      camFinal.lens === camBefore.lens &&
      JSON.stringify(camFinal.shift) === JSON.stringify(camBefore.shift), camFinal.camera);

    // ================= 依赖枚举：view_transform ↔ look =====================
    // 回归用：视图变换决定 Look 的合法档位。切换后候选必须**立即**刷新、旧值按
    // 规范化名称迁移；写进 Blender 的只能是 value（真实 identifier）。
    // 旧 bug：切换视图后仍提交 "AgX - High Contrast" → BLENDER_SCRIPT_ERROR。

    const readLookPanel = `(() => {
      const vt = document.getElementById("ctl-color.view_transform");
      const look = document.getElementById("ctl-color.look");
      if (!vt || !look) return { ok: false, reason: "未找到视图变换或 Look 控件" };
      const control = look.closest(".control");
      const note = control ? control.querySelector(".control-look-note") : null;
      return {
        ok: true,
        viewTransform: vt.value,
        viewOptions: Array.from(vt.options).map((o) => o.value),
        lookValue: look.value,
        lookText: look.selectedOptions.length ? look.selectedOptions[0].textContent : "",
        options: Array.from(look.options).map((o) => ({ value: o.value, text: o.textContent })),
        note: note ? note.textContent : "",
        status: (document.getElementById("preview-status") || {}).textContent || "",
        jobId: (String(document.getElementById("preview-img").src).match(/[?&]v=([^&]+)/) || [])[1] || null,
      };
    })()`;

    const fetchJsonAs = (url) => evaluate(`fetch(${JSON.stringify(url)}).then((r) => r.json())`);
    const currentJobId = async () => (await evaluate(readLookPanel)).jobId;

    const setViewTransform = (value) =>
      evaluate(`(() => {
        const vt = document.getElementById("ctl-color.view_transform");
        if (!vt) return { ok: false, reason: "未找到视图变换下拉框" };
        vt.value = ${JSON.stringify(value)};
        vt.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: vt.value === ${JSON.stringify(value)}, value: vt.value };
      })()`);

    const setLookValue = (value) =>
      evaluate(`(() => {
        const look = document.getElementById("ctl-color.look");
        if (!look) return { ok: false, reason: "未找到 Look 下拉框" };
        look.value = ${JSON.stringify(value)};
        look.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: look.value === ${JSON.stringify(value)}, value: look.value };
      })()`);

    const waitLookList = (predicate) => `new Promise((resolve) => {
      const read = () => {
        const vt = document.getElementById("ctl-color.view_transform");
        const look = document.getElementById("ctl-color.look");
        return {
          viewTransform: vt ? vt.value : null,
          lookValue: look ? look.value : null,
          options: look ? Array.from(look.options).map((o) => ({ value: o.value, text: o.textContent })) : [],
        };
      };
      const t0 = Date.now();
      const tick = () => {
        const snap = read();
        if (${predicate}) return resolve(Object.assign({ ok: true }, snap));
        if (Date.now() - t0 > 30000) return resolve(Object.assign({ ok: false }, read()));
        setTimeout(tick, 100);
      };
      tick();
    })`;

    const waitNewPreview = (prevJobId) => `new Promise((resolve) => {
      const img = document.getElementById("preview-img");
      const status = document.getElementById("preview-status");
      const t0 = Date.now();
      const tick = () => {
        const text = status.textContent || "";
        const id = (String(img.src).match(/[?&]v=([^&]+)/) || [])[1] || null;
        const shown = img && !img.classList.contains("hidden") && img.naturalWidth > 0;
        if (text.includes("INVALID_DEPENDENT_ENUM")) {
          return resolve({ ok: false, mode: "invalid", jobId: id, status: text });
        }
        if (shown && id && id !== ${JSON.stringify(prevJobId)} && text.includes("预览完成")) {
          return resolve({ ok: true, mode: "done", jobId: id, status: text });
        }
        if (Date.now() - t0 > 120000) return resolve({ ok: false, mode: "timeout", jobId: id, status: text });
        setTimeout(tick, 150);
      };
      tick();
    })`;

    const panelBefore = await evaluate(readLookPanel);
    const originalViewTransform = panelBefore.ok ? panelBefore.viewTransform : null;
    record(
      "找到视图变换与 Look 下拉框",
      !!panelBefore.ok,
      panelBefore.ok
        ? `view=${panelBefore.viewTransform} look=${panelBefore.lookValue}`
        : panelBefore.reason
    );

    // 「前端 option.value 与显示文本分离」：value 必须逐项等于后端能力表里的
    // Blender 真实 identifier，显示文本必须逐项等于 label。
    // （Blender 5.2.1 下两者恰好同形，但来源必须各自独立。）
    const apiLooks = await fetchJsonAs("/api/color/looks");
    const sameList =
      panelBefore.ok &&
      panelBefore.options.length === (apiLooks.options || []).length &&
      panelBefore.options.every((opt, i) => {
        const ref = (apiLooks.options || [])[i];
        return ref && opt.value === ref.value && opt.text === ref.label;
      });
    record(
      "Look 下拉框：value 取 Blender 真实 identifier，显示文本取 label",
      sameList,
      `DOM=${JSON.stringify(panelBefore.options.slice(0, 3))} API=${JSON.stringify((apiLooks.options || []).slice(0, 3))}`
    );

    // ---- 显式切到 AgX，把回归起点固定下来（族前缀档位才暴露旧 bug）----
    const hasAgX = panelBefore.ok && (panelBefore.viewOptions || []).includes("AgX");
    await setViewTransform("AgX");
    const agxReady = await evaluate(
      waitLookList(`snap.viewTransform === "AgX" && snap.options.some((o) => o.value.startsWith("AgX - "))`)
    );
    record(
      "AgX 可用且档位带族前缀（旧 bug 的复现前提）",
      hasAgX && !!agxReady.ok,
      agxReady.options ? agxReady.options.slice(1, 4).map((o) => o.value).join("、") : String(agxReady)
    );

    if (agxReady.ok) {
      // 直接以「带族前缀的真实 identifier」为起点提交一次完整草稿 ——
      // 这正是旧实现会炸成 BLENDER_SCRIPT_ERROR 的那一步。
      await setLookValue("AgX - High Contrast");
      const beforeDirect = await currentJobId();
      const direct = await evaluate(waitNewPreview(beforeDirect));
      record(
        "AgX + 「AgX - High Contrast」完整草稿预览成功",
        !!direct.ok && direct.jobId !== beforeDirect,
        direct.status
      );

      // ---- AgX → Standard：Look 列表必须立即刷新，旧值按规范化名称迁移 ----
      const beforeStd = await currentJobId();
      const panelAgx = await evaluate(readLookPanel);
      record(
        "切换前 Look 取值为 AgX - High Contrast（迁移起点正确）",
        panelAgx.lookValue === "AgX - High Contrast",
        `look=${panelAgx.lookValue} note=${JSON.stringify(panelAgx.note)}`
      );
      await setViewTransform("Standard");
      const standardList = await evaluate(
        waitLookList(`snap.viewTransform === "Standard" && snap.options.length > 0 &&
          !snap.options.some((o) => o.value.startsWith("AgX - "))`)
      );
      const stdApi = await fetchJsonAs("/api/color/looks?view_transform=Standard");
      record(
        "切到 Standard 后 Look 列表立即刷新（AgX 专属档位已移除）",
        !!standardList.ok &&
          standardList.options.length === (stdApi.options || []).length &&
          standardList.options.every((opt, i) => opt.value === (stdApi.options || [])[i].value),
        `${standardList.options.length} 项 / ${standardList.options.slice(1, 4).map((o) => o.value).join("、")}`
      );
      record(
        "旧 Look 按规范化名称迁移（AgX - High Contrast → High Contrast）",
        standardList.lookValue === "High Contrast",
        `look=${standardList.lookValue}`
      );
      const panelStd = await evaluate(readLookPanel);
      record(
        "迁移后给出明确说明文案",
        panelStd.note.includes("迁移") && panelStd.note.includes("High Contrast"),
        panelStd.note || "(无说明)"
      );

      // ---- Standard → AgX：迁移回族前缀 identifier，并再次出预览 ----
      await setViewTransform("AgX");
      const agxList = await evaluate(
        waitLookList(`snap.viewTransform === "AgX" && snap.lookValue === "AgX - High Contrast"`)
      );
      record(
        "切回 AgX 后迁移回 AgX - High Contrast",
        !!agxList.ok && agxList.lookValue === "AgX - High Contrast",
        `look=${agxList.lookValue}`
      );

      const afterSwitch = await evaluate(waitNewPreview(beforeStd));
      await shot("09-look-after-view-switch");
      record(
        "切换视图变换后的完整草稿可正常预览（不再 BLENDER_SCRIPT_ERROR）",
        !!afterSwitch.ok && !String(afterSwitch.status).includes("BLENDER_SCRIPT_ERROR"),
        afterSwitch.status
      );

      if (afterSwitch.jobId && afterSwitch.jobId !== beforeStd) {
        const job = await fetchJsonAs("/api/jobs/" + afterSwitch.jobId);
        const rec = (((job || {}).result || {}).parameters || {})["color.look"] || {};
        record(
          "任务记录里 effective_value 是 Blender 真实 identifier",
          rec.effective_value === "AgX - High Contrast",
          `configured=${JSON.stringify(rec.configured_value)} effective=${JSON.stringify(rec.effective_value)} label=${JSON.stringify(rec.display_label)}`
        );
        record(
          "任务记录同时保留 display_label（与 value 分开保存）",
          Object.prototype.hasOwnProperty.call(rec, "display_label") &&
            Object.prototype.hasOwnProperty.call(rec, "configured_value"),
          JSON.stringify(rec)
        );
      }
    }

    // ---- 快速连续切换：不等待刷新，旧候选/旧状态不得覆盖最后一次选择 ----
    const rapidFinal = await evaluate(`(() => {
      const vt = document.getElementById("ctl-color.view_transform");
      if (!vt) return null;
      const available = new Set(Array.from(vt.options).map((o) => o.value));
      const wanted = ["Standard", "Filmic Log", "AgX", "Standard"].filter((v) => available.has(v));
      if (wanted[wanted.length - 1] !== "Standard") wanted.push("Standard");
      wanted.forEach((v) => {
        vt.value = v;
        vt.dispatchEvent(new Event("change", { bubbles: true }));
      });
      return { sent: wanted, value: vt.value };
    })()`);
    const raced = await evaluate(
      waitLookList(`snap.viewTransform === "Standard" && snap.options.length > 0 &&
        !snap.options.some((o) => o.value.startsWith("AgX - "))`)
    );
    record(
      "快速连续切换视图后，Look 列表对应最后一次选择（未被旧结果覆盖）",
      !!raced.ok && raced.viewTransform === "Standard" && raced.lookValue === "High Contrast",
      `发序列=${JSON.stringify(rapidFinal && rapidFinal.sent)} 落点=${raced.viewTransform}/look=${raced.lookValue}`
    );

    // ---- 非法组合：前端必须展示依赖详情，而不是笼统的脚本错误 ----
    await evaluate(`(() => {
      const look = document.getElementById("ctl-color.look");
      if (!look) return false;
      const bad = document.createElement("option");
      bad.value = "AgX - Punchy";   // Standard 下不合法
      bad.textContent = "AgX - Punchy";
      look.appendChild(bad);
      look.value = "AgX - Punchy";
      look.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    })()`);
    const invalid = await evaluate(`new Promise((resolve) => {
      const status = document.getElementById("preview-status");
      const t0 = Date.now();
      const tick = () => {
        const text = status.textContent || "";
        if (text.includes("INVALID_DEPENDENT_ENUM")) return resolve({ ok: true, text });
        if (Date.now() - t0 > 90000) return resolve({ ok: false, text });
        setTimeout(tick, 150);
      };
      tick();
    })`);
    await shot("10-invalid-dependent-enum");
    record(
      "非法 Look/视图组合报 INVALID_DEPENDENT_ENUM（不是 BLENDER_SCRIPT_ERROR）",
      !!invalid.ok,
      invalid.text
    );
    record(
      "错误提示摊开了依赖关系与可选值",
      invalid.text.includes("color.look") && invalid.text.includes("color.view_transform") &&
        invalid.text.includes("High Contrast"),
      invalid.text.slice(0, 200)
    );

    // ---- 收尾：丢掉临时塞进去的非法项，把视图变换还原成进入验收前的取值 ----
    await evaluate(`(() => {
      const look = document.getElementById("ctl-color.look");
      if (!look) return false;
      look.value = "High Contrast";
      return look.value === "High Contrast";
    })()`);
    if (originalViewTransform && originalViewTransform !== "Standard") {
      await setViewTransform(originalViewTransform);
    }
    const restoredPanel = await evaluate(
      waitLookList(`snap.viewTransform === ${JSON.stringify(originalViewTransform)} && snap.options.length > 0`)
    );
    await shot("11-view-transform-restored");
    record(
      "验收结束把视图变换还原为进入前的取值",
      !!restoredPanel.ok && restoredPanel.viewTransform === originalViewTransform,
      `${originalViewTransform} → ${restoredPanel.viewTransform}（look=${restoredPanel.lookValue}）`
    );
  } finally {
    // 把用户工程里被临时切走的当前帧放回去（切帧只用于验证「基线失效」）
    if (originalFrame !== null) {
      try {
        await setFrame(originalFrame);
        console.log(`已恢复原帧：${originalFrame}`);
      } catch (err) {
        console.log(`⚠ 恢复原帧失败，请在 Blender 里手动回到第 ${originalFrame} 帧：${err.message}`);
      }
    }
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
