/* Cartoon-Model-Shader · 前端
 * 连接状态 + 当前工程/场景摘要 + L0 曝光调参与预览 + 预设保存 / 应用到工程。
 * 所有动态文本一律用 textContent 写入，绝不拼接 HTML，避免对象名注入。
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var JOB_POLL_INTERVAL_MS = 200;
  /* L0 参数停止调整后触发预览的去抖窗口（需求：250–400ms） */
  var DEBOUNCE_MS = 300;
  var DEFAULT_TARGET = "127.0.0.1:9876";
  /* 本机会话令牌：由服务端在 GET / 的响应里注入（静态文件里没有它）。
     所有写接口都必须带上它，否则后端一律 401 拒绝。 */
  var SESSION_TOKEN =
    typeof window.__TOON_TUNER_TOKEN__ === "string" ? window.__TOON_TUNER_TOKEN__ : "";
  var TOKEN_HEADER = "X-Toon-Tuner-Token";

  function el(id) {
    return document.getElementById(id);
  }

  var nodes = {
    dot: el("status-dot"),
    statusText: el("status-text"),
    target: el("target"),
    checkedAt: el("checked-at"),
    latency: el("latency"),
    hint: el("status-hint"),
    reconnect: el("reconnect-btn"),
    refreshScene: el("refresh-scene-btn"),

    errorCard: el("error-card"),
    errorMessage: el("error-message"),
    errorHint: el("error-hint"),
    errorCode: el("error-code"),
    errorRetryable: el("error-retryable"),

    blenderCard: el("blender-card"),
    objectsCard: el("objects-card"),
    bVersion: el("b-version"),
    bFile: el("b-file"),
    bSaved: el("b-saved"),
    bFullPath: el("b-fullpath"),
    rawJson: el("raw-json"),
    sceneName: el("scene-name"),
    sceneEngine: el("scene-engine"),
    sceneCamera: el("scene-camera"),
    sceneResolution: el("scene-resolution"),
    sceneFrame: el("scene-frame"),
    objectStats: el("object-stats"),
    candidatesBody: el("candidates-body"),
    candidatesEmpty: el("candidates-empty")
  };

  var candidatesTable = document.querySelector(".table-wrap");

  var tuner = {
    card: el("tuner-card"),
    baselineBtn: el("baseline-btn"),
    restoreBtn: el("restore-btn"),
    resetBtn: el("reset-ui-btn"),
    baselineInfo: el("baseline-info"),
    controls: el("param-controls"),
    img: el("preview-img"),
    placeholder: el("preview-placeholder"),
    status: el("preview-status"),
    framingLabel: el("preview-framing-label"),
    jobRaw: el("job-raw")
  };

  /* 取景（构图）界面 */
  var framingView = {
    bar: el("framing-bar"),
    source: el("framing-source"),
    frame: el("framing-frame"),
    camera: el("framing-camera"),
    cameraAnim: el("framing-camera-anim"),
    inside: el("framing-inside"),
    fitMessage: el("framing-fit-message"),
    staleBox: el("framing-stale"),
    staleDetail: el("framing-stale-detail"),
    refreshBaseline: el("framing-refresh-baseline-btn"),
    mode: el("framing-mode"),
    margin: el("framing-margin"),
    marginValue: el("framing-margin-value"),
    reframe: el("reframe-btn")
  };

  /* 取景方式的中文名（与服务端白名单一致） */
  var FRAMING_LABELS = {
    current_camera: "当前相机预览",
    auto_full_body: "临时自动取景 · 全身",
    auto_upper_body: "临时自动取景 · 半身",
    auto_headshot: "临时自动取景 · 头像"
  };

  /* 保存预设 / 应用到工程 */
  var saveView = {
    presetCard: el("preset-card"),
    presetName: el("preset-name"),
    presetSave: el("preset-save-btn"),
    presetRefresh: el("preset-refresh-btn"),
    presetStatus: el("preset-status"),
    presetBody: el("preset-body"),
    presetEmpty: el("preset-empty"),
    presetTable: document.querySelector("#preset-card .table-wrap"),

    card: el("commit-card"),
    modeSaveAs: el("commit-mode-save-as"),
    modeOverwrite: el("commit-mode-overwrite"),
    targetRow: el("commit-target-row"),
    target: el("commit-target"),
    paths: el("commit-paths"),
    absTarget: el("commit-abs-target"),
    absBackup: el("commit-abs-backup"),
    warning: el("commit-warning-text"),
    confirm: el("commit-confirm"),
    apply: el("commit-apply-btn"),
    status: el("commit-status"),
    applied: el("commit-applied"),
    readback: el("commit-readback"),
    backup: el("commit-backup"),
    saved: el("commit-saved"),
    rollback: el("commit-rollback"),
    raw: el("commit-raw")
  };
  var SAVE_MODE_SAVE_AS = "save_as";
  var SAVE_MODE_OVERWRITE = "overwrite";

  var lastStatus = null;
  var sceneLoaded = false;
  var pollTimer = null;

  // -- 调参状态 ---------------------------------------------------------
  var schemaLoaded = false;
  var controlsById = {};
  var baselineValues = null;
  var debounceTimer = null;
  var activeJobId = null;
  /* 单调递增的「预览代次」：只有最新一次提交允许改写画面，
     从而保证旧任务的结果永远不会覆盖新图片。 */
  var previewToken = 0;
  /* 基线首张预览的 URL（带 job_id 防缓存），供「恢复基线」与后续 A/B 对比复用。 */
  var baselinePreviewUrl = null;
  var baselinePreviewJobId = null;
  /* 同一时刻只允许一次基线采集（连接状态轮询会重复触发）。 */
  var baselinePromise = null;
  /* 轮询上限，避免后端异常时前端无限轮询。 */
  var MAX_POLL_ATTEMPTS = 400;

  // -- 取景状态 ---------------------------------------------------------
  /* 基线建立后被外部切帧/换相机时置位：用于**停止自动预览**。 */
  var framingStale = false;
  var framingStaleDetail = "";
  var framingContext = null;

  // -- 依赖枚举状态（view_transform -> look）----------------------------
  /* look 的合法档位随「视图变换」变化，候选一律来自 Blender 能力表。
     旧实现直接拿 OCIO 全局名单当候选，把 "AgX - High Contrast" 写进了只接受
     通用档位的视图，炸成 enum not found。 */
  var LOOK_PARAM_ID = "color.look";
  var VIEW_TRANSFORM_PARAM_ID = "color.view_transform";
  var lookMap = {};
  /* 每次视图变换变化都自增；能力请求回来时比对，**旧候选请求不得覆盖新列表**。 */
  var lookRequestToken = 0;
  var lookMapLoading = false;

  function setText(node, value, fallback) {
    if (!node) {
      return;
    }
    var text = value === null || value === undefined || value === "" ? (fallback || "—") : value;
    node.textContent = String(text);
  }

  function formatTime(iso) {
    if (!iso) {
      return "—";
    }
    var date = new Date(iso);
    if (isNaN(date.getTime())) {
      return String(iso);
    }
    return date.toLocaleTimeString();
  }

  function statusLabel(status) {
    switch (status) {
      case "connected":
        return "已连接";
      case "disconnected":
        return "未连接";
      case "timeout":
        return "连接超时";
      case "protocol_error":
        return "协议错误";
      case "blender_error":
        return "Blender 执行错误";
      default:
        return "未连接";
    }
  }

  async function request(path, options) {
    var opts = options || {};
    var headers = {};
    var index;
    var keys = Object.keys(opts.headers || {});
    for (index = 0; index < keys.length; index += 1) {
      headers[keys[index]] = opts.headers[keys[index]];
    }
    /* 令牌挂在**每一条**请求上：写接口没有它就是 401，读接口带了也无害。
       这样前端只有一处需要关心令牌，不会漏掉某个新接口。 */
    if (SESSION_TOKEN) {
      headers[TOKEN_HEADER] = SESSION_TOKEN;
    }
    opts = Object.assign({}, opts, { headers: headers });
    try {
      var response = await fetch(path, opts);
      var body = null;
      try {
        body = await response.json();
      } catch (parseError) {
        body = null;
      }
      return { httpOk: response.ok, status: response.status, body: body };
    } catch (networkError) {
      return { httpOk: false, status: 0, body: null };
    }
  }

  function setDot(kind) {
    nodes.dot.classList.remove("dot-connected", "dot-checking", "dot-error");
    nodes.dot.classList.add("dot-" + kind);
  }

  function setBusy(busy) {
    nodes.reconnect.disabled = busy;
    nodes.refreshScene.disabled = busy;
  }

  function hideSceneCards() {
    nodes.blenderCard.classList.add("hidden");
    nodes.objectsCard.classList.add("hidden");
    tuner.card.classList.add("hidden");
    framingView.bar.classList.add("hidden");
    saveView.presetCard.classList.add("hidden");
    saveView.card.classList.add("hidden");
    sceneLoaded = false;
  }

  function hideErrorCard() {
    nodes.errorCard.classList.add("hidden");
  }

  function showErrorCard(error) {
    var detail = error || {};
    setText(nodes.errorMessage, detail.message, "发生未知错误");
    setText(nodes.errorHint, detail.hint, "请确认 Blender 已打开并已启动 MCP 服务，然后点「重新连接」。");
    setText(nodes.errorCode, detail.code, "UNKNOWN");
    setText(nodes.errorRetryable, detail.retryable === true ? "是" : "否");
    nodes.errorCard.classList.remove("hidden");
  }

  function statBlock(value, label) {
    var wrap = document.createElement("div");
    wrap.className = "stat";
    var num = document.createElement("span");
    num.className = "num";
    num.textContent = String(value);
    var text = document.createElement("span");
    text.className = "label";
    text.textContent = label;
    wrap.appendChild(num);
    wrap.appendChild(text);
    return wrap;
  }

  function renderScene(payload) {
    var blender = payload.blender || {};
    var scene = payload.scene || {};
    var objects = payload.objects || {};

    setText(nodes.bVersion, blender.version, "unknown");
    setText(nodes.bFile, blender.file_name, "（未保存的工程）");
    if (blender.file_name) {
      setText(nodes.bSaved, blender.is_saved ? "已保存" : "有未保存修改");
    } else {
      setText(nodes.bSaved, "尚未保存到磁盘");
    }
    setText(nodes.bFullPath, blender.file_path, "—");
    nodes.rawJson.textContent = JSON.stringify(payload, null, 2);

    setText(nodes.sceneName, scene.name, "（无）");
    setText(nodes.sceneEngine, scene.render_engine, "unknown");
    setText(nodes.sceneCamera, scene.camera, "无相机");

    var resolution = scene.resolution || [];
    if (resolution.length >= 2) {
      var percent = resolution.length >= 3 && resolution[2] !== 100 ? " (" + resolution[2] + "%)" : "";
      setText(nodes.sceneResolution, resolution[0] + " × " + resolution[1] + percent);
    } else {
      setText(nodes.sceneResolution, "—");
    }
    setText(nodes.sceneFrame, scene.frame_current);

    nodes.objectStats.textContent = "";
    nodes.objectStats.appendChild(statBlock(objects.total, "对象总数"));
    nodes.objectStats.appendChild(statBlock(objects.mesh_count, "网格"));
    nodes.objectStats.appendChild(statBlock(objects.visible_mesh_count, "可见网格"));
    nodes.objectStats.appendChild(statBlock(objects.light_count, "灯光"));
    nodes.objectStats.appendChild(statBlock(objects.camera_count, "相机"));

    var candidates = payload.role_candidates || [];
    nodes.candidatesBody.textContent = "";
    if (candidates.length === 0) {
      nodes.candidatesTable.classList.add("hidden");
      nodes.candidatesEmpty.classList.remove("hidden");
    } else {
      nodes.candidatesTable.classList.remove("hidden");
      nodes.candidatesEmpty.classList.add("hidden");
      candidates.forEach(function (item) {
        var row = document.createElement("tr");
        [item.name, item.polygons, item.material_slots, item.visible ? "是" : "否", item.hide_render ? "否" : "是"].forEach(
          function (value, index) {
            var cell = document.createElement("td");
            cell.textContent = String(value);
            if (index === 1 || index === 2) {
              cell.className = "num";
            }
            row.appendChild(cell);
          }
        );
        nodes.candidatesBody.appendChild(row);
      });
    }

    nodes.blenderCard.classList.remove("hidden");
    nodes.objectsCard.classList.remove("hidden");
    sceneLoaded = true;
  }

  function applyStatusPayload(body) {
    setText(nodes.target, body.target, DEFAULT_TARGET);
    setText(nodes.checkedAt, formatTime(body.checked_at));
    setText(nodes.latency, body.latency_ms === null || body.latency_ms === undefined ? "—" : body.latency_ms + " ms");

    if (body.ok && body.status === "connected") {
      setDot("connected");
      nodes.statusText.textContent = "已连接 Blender MCP";
      nodes.hint.textContent = "";
      hideErrorCard();
      var justConnected = lastStatus !== "connected";
      lastStatus = "connected";
      if (justConnected || !sceneLoaded) {
        loadScene();
      }
      loadFramingContext();
      initTuner();
      return true;
    }

    setDot("error");
    nodes.statusText.textContent = statusLabel(body.status);
    nodes.hint.textContent = body.error && body.error.hint ? body.error.hint : "";
    showErrorCard(body.error);
    hideSceneCards();
    lastStatus = body.status;
    return false;
  }

  async function refreshStatus() {
    setDot("checking");
    nodes.statusText.textContent = "正在检测…";

    var result = await request("/api/blender/status");
    if (!result.body) {
      setDot("error");
      nodes.statusText.textContent = "无法连接本地控制服务";
      setText(nodes.target, DEFAULT_TARGET);
      setText(nodes.checkedAt, "—");
      setText(nodes.latency, "—");
      nodes.hint.textContent = "本地服务可能未启动。请在项目目录运行 uvicorn，再点「重新连接」。";
      showErrorCard({
        code: "SERVICE_UNREACHABLE",
        message: "无法访问 /api/blender/status",
        retryable: true,
        hint: "请确认本地控制服务已启动（uvicorn src.server.app:app --host 127.0.0.1 --port 8765）。"
      });
      hideSceneCards();
      return;
    }
    applyStatusPayload(result.body);
  }

  async function loadScene() {
    var result = await request("/api/blender/scene");
    if (!result.body) {
      showErrorCard({
        code: "SERVICE_UNREACHABLE",
        message: "无法访问 /api/blender/scene",
        retryable: true,
        hint: "请确认本地控制服务已启动。"
      });
      return;
    }
    if (!result.body.ok) {
      showErrorCard(result.body.error);
      hideSceneCards();
      return;
    }
    hideErrorCard();
    renderScene(result.body);
  }

  async function onReconnect() {
    setBusy(true);
    setDot("checking");
    nodes.statusText.textContent = "正在重新连接…";
    var result = await request("/api/blender/reconnect", { method: "POST" });
    if (!result.body) {
      await refreshStatus();
      setBusy(false);
      return;
    }
    sceneLoaded = false;
    lastStatus = null;
    applyStatusPayload(result.body);
    setBusy(false);
  }

  async function onRefreshScene() {
    setBusy(true);
    await loadScene();
    await loadFramingContext();
    setBusy(false);
  }

  // -- 取景（构图）--------------------------------------------------------
  function framingLabel(mode) {
    return FRAMING_LABELS[mode] || "当前相机预览";
  }

  function currentFramingOptions() {
    var margin = Number(framingView.margin.value);
    if (!isFinite(margin)) {
      margin = 15;
    }
    return {
      mode: framingView.mode.value || "current_camera",
      margin: Math.min(0.4, Math.max(0, margin / 100))
    };
  }

  function updateMarginReadout() {
    framingView.marginValue.textContent = Number(framingView.margin.value) + "%";
  }

  function setFramingSource(mode) {
    var usesTemp = mode !== "current_camera";
    framingView.source.textContent = usesTemp
      ? "临时自动取景（" + framingLabel(mode).replace("临时自动取景 · ", "") + "）"
      : "当前相机预览";
    framingView.source.classList.remove("source-current", "source-temp");
    framingView.source.classList.add(usesTemp ? "source-temp" : "source-current");

    tuner.framingLabel.textContent = usesTemp
      ? "临时自动取景 · 不改动用户相机"
      : "当前相机预览";
    tuner.framingLabel.classList.remove("label-current", "label-temp");
    tuner.framingLabel.classList.add(usesTemp ? "label-temp" : "label-current");

    /* 判据必须跟着取景方式走：切到自动取景先置为「取景中」（等实际渲染结果），
       切回当前相机则立刻用缓存的上文判据，不让上一次的结论滞留。 */
    if (framingView.inside) {
      if (usesTemp) {
        setInsidePlaceholder(mode);
      } else if (framingContext) {
        applyInsideVerdict(framingContext.fit, mode);
      }
    }
  }

  function insideText(fit) {
    if (!fit || fit.inside === null || fit.inside === undefined) {
      if (fit && fit.reason === "no_camera") {
        return { text: "无法判断（场景没有活动相机）", cls: "framing-inside-warn" };
      }
      if (fit && fit.reason === "no_character") {
        return { text: "无法判断（没找到角色网格）", cls: "framing-inside-warn" };
      }
      return { text: "无法判断", cls: "framing-inside-warn" };
    }
    if (fit.inside) {
      return { text: "是（完整落在画面内）", cls: "framing-inside-ok" };
    }
    return { text: "否（已出画）", cls: "framing-inside-bad" };
  }

  /* 「是否完整入画」的判定**永远描述当前选中的取景方式**，并显式标注参照的是哪台相机。
     否则会出现「自动取景实拍完整入画、判据却说已出画」这种自相矛盾的界面。 */
  function framingBasisLabel(mode) {
    return mode && mode !== "current_camera" ? "按临时取景相机" : "按当前相机";
  }

  function applyInsideVerdict(fit, mode) {
    var verdict = insideText(fit);
    framingView.inside.textContent = verdict.text + "（" + framingBasisLabel(mode) + "）";
    framingView.inside.classList.remove("framing-inside-ok", "framing-inside-bad", "framing-inside-warn");
    framingView.inside.classList.add(verdict.cls);
  }

  function setInsidePlaceholder(mode) {
    framingView.inside.textContent = "取景中…（" + framingBasisLabel(mode) + "）";
    framingView.inside.classList.remove("framing-inside-ok", "framing-inside-bad", "framing-inside-warn");
    framingView.inside.classList.add("framing-inside-warn");
  }

  /* 相机带关键帧时必须显式告警（需求 6）。 */
  function cameraAnimationWarning() {
    var baseline = (framingContext && framingContext.baseline) || {};
    var found = (baseline.warnings || []).filter(function (item) {
      return item && item.code === "camera_animated";
    });
    return found.length ? found[0].message : "";
  }

  function renderFramingContext(context) {
    framingContext = context;
    framingView.bar.classList.remove("hidden");

    var mode = framingView.mode.value || "current_camera";
    var frameText = String(context.frame_current);
    if (context.frame_start !== null && context.frame_end !== null &&
        context.frame_start !== undefined && context.frame_end !== undefined) {
      frameText += "（工程范围 " + context.frame_start + "–" + context.frame_end + "）";
    }
    setText(framingView.frame, frameText, "—");
    setText(framingView.camera, context.camera, "无相机");

    var animation = (context.camera_info || {}).animation || {};
    if (animation.has_animation) {
      var sources = (animation.sources || []).join("、");
      var curves = animation.fcurves ? "，共 " + animation.fcurves + " 条曲线" : "";
      setText(framingView.cameraAnim, "有动画（" + sources + curves + "）", "有动画");
    } else if (context.camera) {
      setText(framingView.cameraAnim, "无（未检测到关键帧 / 驱动器）", "无");
    } else {
      setText(framingView.cameraAnim, "—");
    }

    /* 入画判据只属于「当前相机」这条取景路径：自动取景的判据来自实际渲染结果，
       由 applyJobFraming 写入。这里若不加区分地覆盖，就会出现判据与画面互相打架。 */
    var judgeCurrentCamera = mode === "current_camera";

    var messages = [];
    if (judgeCurrentCamera) {
      applyInsideVerdict(context.fit, mode);
      if (context.fit && context.fit.message) {
        messages.push(context.fit.message);
      }
      if (context.fit && context.fit.margin_hint) {
        messages.push(context.fit.margin_hint);
      }
    }
    var character = context.character || {};
    if (character.group) {
      var objectNames = (character.objects || []).map(function (item) {
        return item.name;
      });
      messages.push("取景对象：骨架「" + character.group + "」下的 " + objectNames.join("、") +
        "（已排除刚体代理 / 描边壳 / 隐藏对象）");
    }
    var baseline = context.baseline || {};
    if (baseline.established && baseline.frame_current !== null && baseline.frame_current !== undefined) {
      messages.push("基线锁定于第 " + baseline.frame_current + " 帧 · 相机 " + (baseline.camera || "（无）"));
    }
    var animWarning = cameraAnimationWarning();
    if (animWarning) {
      messages.push(animWarning);
    }
    framingView.fitMessage.textContent = messages.join(" ｜ ");

    /* 基线失效：停止自动预览并提示刷新基线 */
    setFramingStale(!!baseline.stale, (baseline.reasons || []).map(function (item) {
      return item.message;
    }).join("；"));
  }

  function setFramingStale(stale, detail) {
    framingStale = !!stale;
    framingStaleDetail = detail || "";
    if (stale) {
      framingView.staleBox.classList.remove("hidden");
      framingView.staleDetail.textContent = framingStaleDetail;
      framingView.reframe.disabled = true;
      tuner.status.classList.remove("status-ok", "status-busy");
      tuner.status.classList.add("status-err");
      tuner.status.textContent = "自动预览已停止：当前帧/相机已变化，请刷新基线" +
        (framingStaleDetail ? "（" + framingStaleDetail + "）" : "");
    } else {
      framingView.staleBox.classList.add("hidden");
      framingView.staleDetail.textContent = "";
      framingView.reframe.disabled = false;
    }
  }

  async function loadFramingContext() {
    var result = await request("/api/framing/context");
    if (!result.body || result.body.ok !== true) {
      return null;
    }
    renderFramingContext(result.body);
    return result.body;
  }

  async function onFramingModeChange() {
    setFramingSource(framingView.mode.value);
    if (framingStale) {
      setFramingStale(true, framingStaleDetail);
      return;
    }
    if (!baselineValues) {
      return;
    }
    schedulePreview();
  }

  function onFramingMarginInput() {
    updateMarginReadout();
    if (framingStale || !baselineValues) {
      return;
    }
    schedulePreview();
  }

  async function onReframe() {
    if (framingStale) {
      setFramingStale(true, framingStaleDetail);
      return;
    }
    if (!baselineValues) {
      var ok = await ensureBaseline(false);
      if (!ok) {
        return;
      }
    }
    await submitPreview();
  }

  // -- L0 曝光调参 -------------------------------------------------------
  function setPreviewStatus(text, kind) {
    tuner.status.textContent = text;
    tuner.status.classList.remove("status-ok", "status-busy", "status-err");
    if (kind) {
      tuner.status.classList.add("status-" + kind);
    }
  }

  function showTunerError(error) {
    var detail = error || {};
    var suffix = "";
    if (detail.code === "INVALID_DEPENDENT_ENUM") {
      /* 依赖枚举错误：把「哪个参数 / 什么值 / 依赖谁 / 允许什么」直接摊开，
         而不是笼统地报一句失败。 */
      var depends = detail.depends_on || {};
      var parent = Object.keys(depends)[0];
      var allowed = (detail.allowed || []).join("、");
      suffix =
        "　（" + (detail.parameter || "参数") + " = " + JSON.stringify(detail.value) +
        "，在 " + (parent || "依赖参数") + " = " + JSON.stringify(parent ? depends[parent] : "") +
        " 下不合法；可选：" + allowed + "）";
    }
    setPreviewStatus(
      "(" + (detail.code || "ERROR") + ") " + (detail.message || "预览失败") + suffix,
      "err"
    );
    tuner.jobRaw.textContent = JSON.stringify(detail, null, 2);
  }

  function setPlaceholder(text) {
    tuner.placeholder.textContent = text;
    tuner.placeholder.classList.remove("hidden");
  }

  function delay(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  /* 只有 onload 之后才显示图片；onerror 一律落到错误态，绝不显示成功。 */
  function showImage(url, onErrorStatus) {
    return new Promise(function (resolve) {
      var img = tuner.img;
      img.onload = function () {
        img.classList.remove("hidden");
        tuner.placeholder.classList.add("hidden");
        resolve(true);
      };
      img.onerror = function () {
        img.classList.add("hidden");
        setPlaceholder("预览图无法加载");
        setPreviewStatus(onErrorStatus, "err");
        resolve(false);
      };
      img.src = url;
    });
  }

  /* 枚举项可能是旧格式（纯字符串）或新格式（{value,label}）。
     写进 Blender 的**永远**是 value；label 只用来显示。 */
  function optionValue(option) {
    if (option && typeof option === "object") {
      return String(option.value);
    }
    return String(option);
  }

  function optionLabel(option) {
    if (option && typeof option === "object" && option.label !== undefined && option.label !== null) {
      return String(option.label);
    }
    return optionValue(option);
  }

  function setSelectOptions(select, options) {
    var previous = select.value;
    select.textContent = "";
    (options || []).forEach(function (option) {
      var opt = document.createElement("option");
      opt.value = optionValue(option);
      opt.textContent = optionLabel(option);
      select.appendChild(opt);
    });
    return previous;
  }

  function buildControl(spec) {
    var wrap = document.createElement("div");
    wrap.className = "control";

    var head = document.createElement("div");
    head.className = "control-head";
    var label = document.createElement("label");
    label.setAttribute("for", "ctl-" + spec.id);
    label.textContent = spec.label;
    var readout = document.createElement("span");
    readout.className = "control-value mono";
    head.appendChild(label);
    head.appendChild(readout);
    wrap.appendChild(head);

    var input;
    if (spec.type === "float") {
      input = document.createElement("input");
      input.type = "range";
      input.min = String(spec.minimum);
      input.max = String(spec.maximum);
      input.step = String(spec.step || 0.01);
    } else {
      input = document.createElement("select");
      setSelectOptions(input, spec.options);
    }
    input.id = "ctl-" + spec.id;
    input.dataset.paramId = spec.id;
    wrap.appendChild(input);

    var meta = document.createElement("p");
    meta.className = "control-meta";
    if (spec.type === "float") {
      meta.textContent = "范围 [" + spec.minimum + ", " + spec.maximum + "] · " + spec.target;
    } else if (spec.depends_on) {
      meta.textContent = spec.target + " · 可选档位由 Blender 按「" + spec.depends_on + "」实时给出";
    } else {
      meta.textContent = spec.options_dynamic ? spec.target + " · 候选取自 Blender 实时配置" : spec.target;
    }
    wrap.appendChild(meta);

    if (spec.note) {
      var note = document.createElement("p");
      note.className = "control-note";
      note.textContent = spec.note;
      wrap.appendChild(note);
    }

    return { element: wrap, input: input, readout: readout, spec: spec };
  }

  function updateReadout(entry) {
    var value = entry.input.value;
    if (entry.spec.type === "float") {
      entry.readout.textContent = Number(value).toFixed(2);
    } else {
      entry.readout.textContent = "";
    }
  }

  function buildControls(groups) {
    tuner.controls.textContent = "";
    controlsById = {};
    (groups || []).forEach(function (group) {
      var heading = document.createElement("h3");
      heading.textContent = group.name;
      tuner.controls.appendChild(heading);
      (group.params || []).forEach(function (spec) {
        var entry = buildControl(spec);
        controlsById[spec.id] = entry;
        updateReadout(entry);
        entry.input.addEventListener(entry.spec.type === "float" ? "input" : "change", function () {
          updateReadout(entry);
          if (entry.spec.id === VIEW_TRANSFORM_PARAM_ID) {
            /* 视图变换决定 Look 的合法档位：先刷新 Look 列表并迁移旧值，
               再触发预览 —— 绝不能把已经不合法或还是旧标签的值发给 Blender。 */
            onViewTransformChange();
            return;
          }
          schedulePreview();
        });
        tuner.controls.appendChild(entry.element);
      });
    });
    var hint = document.createElement("p");
    hint.className = "control-note";
    hint.textContent = "提示：预览使用的分辨率低于正式导出；参数改动只影响预览，不会写入工程。";
    tuner.controls.appendChild(hint);
  }

  // -- 依赖枚举联动（view_transform -> look）-----------------------------

  function lookOptionsFor(viewTransform) {
    var options = viewTransform ? lookMap[viewTransform] : null;
    return options && options.length ? options : null;
  }

  /* 把任意形状的旧 look 迁移到目标视图。
     等价项按「真实 identifier → 显示标签 → 族前缀短名 → 反向短名」顺序找；
     都没有就回退 None。 */
  function normalizeLook(raw, viewTransform) {
    var options = lookOptionsFor(viewTransform);
    if (!options) {
      return null;
    }
    function firstMatch(predicate) {
      for (var i = 0; i < options.length; i += 1) {
        if (predicate(optionValue(options[i]), optionLabel(options[i]))) {
          return options[i];
        }
      }
      return null;
    }
    function suffixOf(text) {
      var at = text.indexOf(" - ");
      return at >= 0 ? text.slice(at + 3) : null;
    }
    function fallbackOption() {
      return (
        firstMatch(function (value) {
          return value === "None";
        }) || options[0]
      );
    }

    if (raw === null || raw === undefined || String(raw).trim() === "") {
      var none = fallbackOption();
      return { value: optionValue(none), label: optionLabel(none), migrated: false, empty: true };
    }

    var text = String(raw);
    var short = suffixOf(text);
    var hit =
      firstMatch(function (value) {
        return value === text;
      }) ||
      firstMatch(function (value, label) {
        return label === text;
      }) ||
      (short
        ? firstMatch(function (value) {
            return value === short;
          })
        : null) ||
      firstMatch(function (value) {
        return suffixOf(value) === text;
      });

    if (hit) {
      return {
        value: optionValue(hit),
        label: optionLabel(hit),
        migrated: optionValue(hit) !== text
      };
    }
    var fb = fallbackOption();
    return { value: optionValue(fb), label: optionLabel(fb), migrated: true, fallback: true };
  }

  /* 用给定能力表重刷 Look 下拉框，并按规范化规则迁移当前值。
     返回 true 表示列表已就绪（调用方才可以安全地提交草稿）。 */
  function refreshLookOptions(viewTransform) {
    var entry = controlsById[LOOK_PARAM_ID];
    var options = lookOptionsFor(viewTransform);
    if (!entry || !options) {
      return false;
    }
    var previous = entry.input.value;
    setSelectOptions(entry.input, options);
    var normalized = normalizeLook(previous, viewTransform);
    if (normalized && normalized.value) {
      entry.input.value = normalized.value;
      if (normalized.migrated && !normalized.empty) {
        setLookNote(
          normalized.fallback
            ? "「" + previous + "」在「" + viewTransform + "」下没有对应档位，已回退为「" + normalized.label + "」。"
            : "Look 已随「" + viewTransform + "」迁移为「" + normalized.label + "」。"
        );
      } else {
        setLookNote("");
      }
    }
    return true;
  }

  function setLookNote(text) {
    var entry = controlsById[LOOK_PARAM_ID];
    if (!entry) {
      return;
    }
    var note = entry.element.querySelector(".control-look-note");
    if (!text) {
      if (note) {
        note.remove();
      }
      return;
    }
    if (!note) {
      note = document.createElement("p");
      note.className = "control-note control-look-note";
      entry.element.appendChild(note);
    }
    note.textContent = text;
  }

  /* 能力表里没有这个视图时，才去问 Blender（通常是零调用）。
     代次守卫：快速连点视图变换时，**旧候选请求不得覆盖新列表**。 */
  async function loadLookOptions(viewTransform) {
    var token = ++lookRequestToken;
    var result = await request(
      "/api/color/looks?view_transform=" + encodeURIComponent(viewTransform)
    );
    if (token !== lookRequestToken) {
      return false;
    }
    if (!result.body || !result.body.ok) {
      return false;
    }
    applyLookMap(result.body.look_map);
    return true;
  }

  function applyLookMap(map) {
    if (!map) {
      return;
    }
    lookMap = map;
  }

  async function onViewTransformChange() {
    var entry = controlsById[VIEW_TRANSFORM_PARAM_ID];
    var viewTransform = entry ? entry.input.value : null;
    if (!refreshLookOptions(viewTransform)) {
      // 本地能力表没有这个视图：先拿到列表再继续，期间不提交任何草稿
      setLookNote("正在向 Blender 确认「" + viewTransform + "」下可用的 Look…");
      await loadLookOptions(viewTransform);
      refreshLookOptions(viewTransform);
    }
    if (framingStale || !baselineValues) {
      return;
    }
    schedulePreview();
  }

  function collectLookMapFromBaseline(body) {
    if (body && body.look_map) {
      applyLookMap(body.look_map);
    }
  }

  async function loadSchema() {
    var result = await request("/api/params/schema");
    if (!result.body || !result.body.ok) {
      tuner.controls.textContent = "";
      var failed = document.createElement("p");
      failed.className = "muted";
      failed.textContent = "参数表读取失败。请确认本地服务与 Blender 均正常后重试。";
      tuner.controls.appendChild(failed);
      return false;
    }
    buildControls(result.body.groups);
    schemaLoaded = true;
    return true;
  }

  function collectDraft() {
    var draft = {};
    Object.keys(controlsById).forEach(function (paramId) {
      var entry = controlsById[paramId];
      draft[paramId] = entry.spec.type === "float" ? Number(entry.input.value) : entry.input.value;
    });
    return draft;
  }

  function applyValuesToControls(values) {
    if (!values) {
      return;
    }
    /* 先按「视图变换」刷新 Look 候选，再落值 —— 否则可能把一个当前视图
       并不接受的 look 塞进下拉框，并顺着草稿发给 Blender。 */
    if (values[VIEW_TRANSFORM_PARAM_ID] !== undefined) {
      var vtEntry = controlsById[VIEW_TRANSFORM_PARAM_ID];
      if (vtEntry) {
        vtEntry.input.value = String(values[VIEW_TRANSFORM_PARAM_ID]);
      }
      refreshLookOptions(String(values[VIEW_TRANSFORM_PARAM_ID]));
    }

    Object.keys(controlsById).forEach(function (paramId) {
      var entry = controlsById[paramId];
      if (!(paramId in values)) {
        return;
      }
      var value = values[paramId];
      if (entry.spec.type === "float") {
        entry.input.value = String(value);
      } else {
        var exists = false;
        for (var i = 0; i < entry.input.options.length; i += 1) {
          if (entry.input.options[i].value === String(value)) {
            exists = true;
            break;
          }
        }
        if (!exists) {
          // 候选表里还没有这个值（例如能力表尚未加载）：补一项，但**保留原样**，
          // 让后端去判定它是否合法，绝不在这里伪造 label。
          var opt = document.createElement("option");
          opt.value = String(value);
          opt.textContent = String(value);
          entry.input.appendChild(opt);
        }
        entry.input.value = String(value);
      }
      updateReadout(entry);
    });
  }

  async function ensureBaseline(force) {
    if (baselineValues && !force) {
      return true;
    }
    if (baselinePromise) {
      return baselinePromise;
    }
    baselinePromise = captureBaseline();
    try {
      return await baselinePromise;
    } finally {
      baselinePromise = null;
    }
  }

  async function captureBaseline() {
    setPreviewStatus("正在建立内存基线…", "busy");
    var options = currentFramingOptions();
    /* 刷新基线 = 以当前帧/相机为准重新锁定，因此先解除失效状态 */
    setFramingStale(false, "");
    var result = await request("/api/session/baseline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ framing: options })
    });
    if (!result.body || !result.body.ok) {
      var error = result.body && result.body.error;
      if (error && error.code === "FRAMING_STALE") {
        /* 采集期间帧/相机又变了：仍然按「需要刷新基线」提示 */
        setFramingStale(true, staleDetailFromError(error));
        setPreviewStatus("建立基线失败：当前帧/相机已变化，请再试一次", "err");
        return false;
      }
      showTunerError(error);
      return false;
    }
    baselineValues = result.body.values || {};
    /* 先把 look 能力表装进来，再落值：否则 applyValuesToControls 会在
       Look 下拉框里看到一个当前视图并不接受的旧档位。 */
    collectLookMapFromBaseline(result.body);
    tuner.baselineInfo.textContent =
      "基线 " + result.body.baseline_id + " · " + formatTime(result.body.captured_at) +
      " · Blender " + (result.body.blender || "?") +
      (result.body.framing && result.body.framing.frame_current !== null
        ? " · 锁定第 " + result.body.framing.frame_current + " 帧"
        : "");
    applyValuesToControls(baselineValues);
    setFramingSource(options.mode);
    loadFramingContext();

    if (!result.body.job_id) {
      /* 兜底：后端未返回首张预览任务时，至少保持界面可用。 */
      setPreviewStatus("基线已建立，可开始调参", "ok");
      return true;
    }

    /* 需求：基线成功后立即用基线参数渲染一次预览；
       只有图片 load 成功才显示「可开始调参」。 */
    var token = ++previewToken;
    tuner.img.classList.add("hidden");
    setPlaceholder("正在渲染基线预览…");
    setPreviewStatus("正在渲染基线预览…", "busy");
    await runPreviewJob(result.body.job_id, { baseline: true }, token);
    return true;
  }

  async function onRestoreBaseline() {
    ++previewToken; /* 让在途的预览轮询失效，避免旧结果覆盖基线画面 */
    setPreviewStatus("正在恢复基线…", "busy");
    var result = await request("/api/session/restore", { method: "POST" });
    if (!result.body) {
      setPreviewStatus("无法访问本地服务", "err");
      return;
    }
    if (result.body.ok !== true) {
      showTunerError(result.body.error);
      return;
    }
    applyValuesToControls(result.body.readback);
    tuner.jobRaw.textContent = JSON.stringify(result.body, null, 2);
    if (!result.body.verified) {
      setPreviewStatus("已恢复，但存在不一致项（见任务详情）", "err");
      return;
    }
    if (baselinePreviewUrl) {
      /* 场景已回到基线，直接复用保存的基线预览图，无需重复渲染。 */
      var loaded = await showImage(baselinePreviewUrl, "基线预览图无法加载，请点「建立 / 刷新基线」重试");
      if (loaded) {
        setPreviewStatus("已恢复到基线，逐项校验通过（显示基线预览）", "ok");
      }
      return;
    }
    setPreviewStatus("已恢复到基线，逐项校验通过", "ok");
  }

  function schedulePreview() {
    if (framingStale) {
      /* 需求 3：当前帧/相机已变 => 停止自动预览，绝不把新构图当成参数效果。 */
      setFramingStale(true, framingStaleDetail);
      return;
    }
    if (debounceTimer) {
      window.clearTimeout(debounceTimer);
    }
    setPreviewStatus("待预览…", "busy");
    debounceTimer = window.setTimeout(function () {
      debounceTimer = null;
      submitPreview();
    }, DEBOUNCE_MS);
  }

  async function submitPreview() {
    if (framingStale) {
      setFramingStale(true, framingStaleDetail);
      return;
    }
    if (!baselineValues) {
      var ok = await ensureBaseline(false);
      if (!ok) {
        return;
      }
    }
    var token = ++previewToken;
    var options = currentFramingOptions();
    setPreviewStatus("已提交预览，等待 Blender…", "busy");
    var result = await request("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: collectDraft(), framing: options })
    });
    if (token !== previewToken) {
      return; /* 期间已有更新的提交，本次结果直接丢弃 */
    }
    if (!result.body || !result.body.ok) {
      var error = result.body && result.body.error;
      if (error && error.code === "FRAMING_STALE") {
        /* 服务端闸门拦下：立即停止自动预览并显示横幅 */
        setFramingStale(true, staleDetailFromError(error));
        loadFramingContext();
        return;
      }
      showTunerError(error);
      return;
    }
    setFramingSource(options.mode);
    activeJobId = result.body.job_id;
    await runPreviewJob(activeJobId, { baseline: false }, token);
  }

  function staleDetailFromError(error) {
    if (!error) {
      return "";
    }
    if (error.details && error.details.reasons && error.details.reasons.length) {
      return error.details.reasons.map(function (item) {
        return item.message;
      }).join("；");
    }
    return error.message || "";
  }

  /* 轮询到终态。被更新的提交取代时返回 null —— 此时绝不能改写画面。 */
  async function pollJob(jobId, token) {
    var attempts = 0;
    while (true) {
      if (token !== previewToken) {
        return null;
      }
      var result = await request("/api/jobs/" + jobId);
      if (result.body) {
        var job = result.body;
        tuner.jobRaw.textContent = JSON.stringify(job, null, 2);
        if (job.status !== "queued" && job.status !== "running") {
          return job;
        }
        if (token === previewToken) {
          setPreviewStatus("Blender 处理中…（" + (job.steps.join(" → ") || job.status) + "）", "busy");
        }
      }
      attempts += 1;
      if (attempts >= MAX_POLL_ATTEMPTS) {
        return {
          status: "failed",
          error: { code: "JOB_TIMEOUT", message: "等待 Blender 完成任务超时。", retryable: true }
        };
      }
      await delay(JOB_POLL_INTERVAL_MS);
    }
  }

  async function runPreviewJob(jobId, opts, token) {
    var options = opts || {};
    var job = await pollJob(jobId, token);
    if (job === null || token !== previewToken) {
      return null; /* 已被更新的提交接管，不触碰画面与状态 */
    }
    if (job.status === "superseded") {
      return job; /* 画面与状态归最新那次提交所有 */
    }
    if (job.status === "failed") {
      var detail = job.error || {};
      if (detail.code === "FRAMING_STALE") {
        setFramingStale(true, staleDetailFromError(detail));
        loadFramingContext();
        return job;
      }
      if (options.baseline) {
        /* 需求：基线参数仍然保留，但 UI 必须明确显示预览失败。 */
        setPlaceholder("基线预览失败");
        setPreviewStatus(
          "基线已建立，预览失败：(" + (detail.code || "ERROR") + ") " + (detail.message || "预览未完成"),
          "err"
        );
      } else {
        showTunerError(job.error);
      }
      return job;
    }
    if (job.status === "done" && job.result && job.result.preview_url) {
      /* 防缓存：URL 带上本次 job_id，避免浏览器复用同路径的旧图。 */
      var url = job.result.preview_url + "?v=" + encodeURIComponent(job.job_id);
      var framingResult = job.result.framing || null;
      var loaded;
      if (options.baseline) {
        loaded = await showImage(url, "基线已建立，预览失败：预览图无法加载");
        if (loaded && token === previewToken) {
          baselinePreviewUrl = url;
          baselinePreviewJobId = job.job_id;
          setPreviewStatus("基线已建立，可开始调参", "ok");
          applyJobFraming(framingResult);
        }
      } else {
        loaded = await showImage(url, "(PREVIEW_IMAGE_LOAD_FAILED) 预览图无法加载，请刷新后重试");
        if (loaded && token === previewToken) {
          var resolution = job.result.render_resolution || [];
          var verified = job.result.restore_verified ? "已回滚基线（校验通过）" : "回滚校验未通过";
          setPreviewStatus(
            "预览完成 · " + resolution[0] + "×" + resolution[1] + " · " + verified,
            job.result.restore_verified ? "ok" : "err"
          );
          applyJobFraming(framingResult);
        }
      }
      return job;
    }
    setPreviewStatus("预览未完成（状态：" + job.status + "）", "err");
    return job;
  }

  /* 用任务结果里的取景信息回填界面：明确「当前相机预览 / 临时自动取景」。 */
  function applyJobFraming(framingResult) {
    if (!framingResult) {
      return;
    }
    setFramingSource(framingResult.mode);
    if (framingResult.temporary_camera && framingResult.camera_restored === false) {
      tuner.framingLabel.textContent = "临时自动取景 · 原相机未恢复（请检查 Blender）";
      tuner.framingLabel.classList.remove("label-current", "label-temp");
      tuner.framingLabel.classList.add("label-temp");
    }
    if (framingResult.frame !== undefined && framingResult.frame !== null) {
      setText(framingView.frame, framingResult.frame, "—");
    }
    setText(
      framingView.camera,
      // 「当前相机」一栏描述的是**场景状态**：临时预览相机渲染完即被销毁并已恢复原相机，
      // 因此这里显示工程相机（original_camera），而不是 camera_used，避免让人误以为场景相机被换掉。
      framingResult.original_camera || framingResult.camera_used,
      "无相机"
    );

    var fit = framingResult.fit;
    if (fit) {
      applyInsideVerdict(fit, framingResult.mode);
      var notes = [];
      if (fit.message) {
        notes.push(fit.message);
      }
      if (framingResult.temporary_camera) {
        notes.push("本次取景使用临时预览相机 " +
          (framingResult.temporary_camera_name || "（未具名）") +
          "，渲染后已恢复原相机 " +
          (framingResult.original_camera || "（无）") + "（不改动用户相机）");
      }
      var warning = cameraAnimationWarning();
      if (warning) {
        notes.push(warning);
      }
      framingView.fitMessage.textContent = notes.join(" ｜ ");
    }
  }

  function onResetUi() {
    if (!baselineValues) {
      setPreviewStatus("尚未建立基线，无法复位", "err");
      return;
    }
    applyValuesToControls(baselineValues);
    schedulePreview();
  }

  // -- 保存预设 / 应用到工程 ---------------------------------------------
  function setSaveStatus(node, text, kind) {
    if (!node) {
      return;
    }
    node.textContent = String(text === null || text === undefined || text === "" ? "—" : text);
    node.classList.remove("is-ok", "is-err", "is-busy");
    if (kind) {
      node.classList.add("is-" + kind);
    }
  }

  /* 诊断面板里隐去令牌：它在页面里本来就可见，但没必要写进可复制的文本块。 */
  function redactedForDisplay(value) {
    if (Array.isArray(value)) {
      return value.map(redactedForDisplay);
    }
    if (value && typeof value === "object") {
      var out = {};
      Object.keys(value).forEach(function (key) {
        out[key] = key === "token" ? "<已隐去>" : redactedForDisplay(value[key]);
      });
      return out;
    }
    return value;
  }

  function describeSaveError(error) {
    if (!error) {
      return "本地服务无响应，请稍后重试。";
    }
    if (error.code === "SESSION_TOKEN_INVALID") {
      return "会话令牌失效（服务可能已重启）：请刷新页面（F5）后重试。";
    }
    return error.message || error.code || "保存失败。";
  }

  function currentSaveMode() {
    return saveView.modeOverwrite && saveView.modeOverwrite.checked
      ? SAVE_MODE_OVERWRITE
      : SAVE_MODE_SAVE_AS;
  }

  function resetCommitStatuses() {
    setSaveStatus(saveView.applied, "—");
    setSaveStatus(saveView.readback, "—");
    setSaveStatus(saveView.backup, "—");
    setSaveStatus(saveView.saved, "—");
    setSaveStatus(saveView.rollback, "—");
  }

  /* 应用 / 回读 / 备份 / 保存 / 失败后回滚：失败时后端也会把这份状态带回来。 */
  function renderCommitStatuses(status) {
    var block = status || {};
    if (block.applied === true) {
      setSaveStatus(saveView.applied, "已写入 Blender", "ok");
    } else {
      setSaveStatus(saveView.applied, "未执行", "err");
    }
    if (block.readback_verified === true) {
      setSaveStatus(saveView.readback, "逐项一致", "ok");
    } else if (block.readback_verified === false && block.mismatches && block.mismatches.length) {
      var items = block.mismatches.map(function (item) {
        return item.id + "（期望 " + JSON.stringify(item.expected) + "，实得 " + JSON.stringify(item.actual) + "）";
      });
      setSaveStatus(saveView.readback, "不一致：" + items.join("；"), "err");
    } else {
      setSaveStatus(saveView.readback, "未执行", "err");
    }
    var backup = block.backup || {};
    if (!backup.required) {
      setSaveStatus(saveView.backup, "无需备份（目标不存在）");
    } else if (backup.created) {
      setSaveStatus(saveView.backup, "已生成备份", "ok");
    } else {
      setSaveStatus(saveView.backup, "备份失败，已拒绝覆盖", "err");
    }
    setSaveStatus(
      saveView.saved,
      block.saved === true ? "已保存到磁盘" : "未保存（草稿已保留）",
      block.saved === true ? "ok" : "err"
    );
    /* 回滚状态必须显式呈现：回滚没确认成功时，Blender 里的取值可能不是提交前的状态。 */
    var rollback = block.rollback;
    if (!rollback) {
      setSaveStatus(saveView.rollback, "—");
    } else if (rollback.verified === true) {
      setSaveStatus(saveView.rollback, "已恢复到提交前的基线，回读校验通过", "ok");
    } else if (rollback.attempted === true) {
      setSaveStatus(
        saveView.rollback,
        "未能确认恢复（" + (rollback.code || "ROLLBACK_FAILED") + "）：请到 Blender 里人工核对该取值",
        "err"
      );
    } else if (block.saved === true) {
      setSaveStatus(saveView.rollback, "未触发");
    } else {
      setSaveStatus(saveView.rollback, rollback.reason || "未触发");
    }
  }

  function renderConfirmBlock(source, resetCheck) {
    var data = source || {};
    if (!data.target_path && !data.backup_path) {
      return;
    }
    setText(saveView.absTarget, data.target_path, "—");
    setText(saveView.absBackup, data.backup_path || "（目标文件不存在，无需备份）");
    saveView.paths.classList.remove("hidden");
    /* 第一次只「给用户看」：此时复选框必须是未勾选状态，逼出一次真正的二次确认。
       apply 流程里用户已经勾过并提交了确认，这里就不要把他的勾去掉。 */
    if (resetCheck !== false) {
      saveView.confirm.checked = false;
    }
    if (data.warnings && data.warnings.length) {
      setText(saveView.warning, data.warnings.join(" ｜ "));
    } else {
      setText(saveView.warning, "覆盖后原文件内容将被本次草稿替换。");
    }
  }

  function hideConfirmBlock() {
    saveView.paths.classList.add("hidden");
    saveView.confirm.checked = false;
  }

  /* 覆盖模式：在用户点按钮之前就把「会写到哪、备份放哪」摊开。 */
  async function probeCommitTargets() {
    if (!baselineValues) {
      return;
    }
    var mode = currentSaveMode();
    if (mode !== SAVE_MODE_OVERWRITE) {
      hideConfirmBlock();
      return;
    }
    var result = await request("/api/session/commit/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode, draft: collectDraft(), confirm: false })
    });
    if (!result.body) {
      setSaveStatus(saveView.status, "无法访问本地服务。", "err");
      return;
    }
    if (result.body.ok) {
      renderConfirmBlock(result.body);
      setSaveStatus(saveView.status, "覆盖前需要二次确认：请核对绝对路径后勾选确认。", "busy");
      return;
    }
    var error = result.body.error || {};
    if (error.code === "SAVE_CONFIRM_REQUIRED") {
      renderConfirmBlock(error.details || {});
      setSaveStatus(saveView.status, "覆盖前需要二次确认：请核对绝对路径后勾选确认。", "busy");
      return;
    }
    hideConfirmBlock();
    setSaveStatus(saveView.status, describeSaveError(error), "err");
  }

  function onSaveModeChange() {
    var mode = currentSaveMode();
    /* 另存为：目标由用户填写；覆盖：目标由 Blender 当前工程决定，输入框没有意义 */
    if (mode === SAVE_MODE_SAVE_AS) {
      saveView.targetRow.classList.remove("hidden");
      hideConfirmBlock();
    } else {
      saveView.targetRow.classList.add("hidden");
    }
    saveView.confirm.checked = false;
    resetCommitStatuses();
    probeCommitTargets();
  }

  async function onCommitApply() {
    if (!baselineValues) {
      setSaveStatus(saveView.status, "尚未建立基线：请先点「建立 / 刷新基线」。", "err");
      return;
    }
    var mode = currentSaveMode();
    var targetPath = saveView.target.value ? saveView.target.value.trim() : "";
    if (mode === SAVE_MODE_SAVE_AS && !targetPath) {
      setSaveStatus(saveView.status, "请先填写目标文件的绝对路径（须以 .blend 结尾）。", "err");
      return;
    }
    /* ★ 草稿只取一次，prepare 与 commit 必须用完全相同的那一份：
       后端把令牌绑在完整草稿上，两次取值之间任何抖动都会被判为篡改。 */
    var draft = collectDraft();
    var confirmed = saveView.confirm.checked;

    saveView.apply.disabled = true;
    resetCommitStatuses();
    setSaveStatus(saveView.status, "正在准备保存…", "busy");

    var prepareBody = { mode: mode, draft: draft, confirm: confirmed };
    if (mode === SAVE_MODE_SAVE_AS) {
      prepareBody.target_path = targetPath;
    }
    var prepared = await request("/api/session/commit/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prepareBody)
    });
    if (!prepared.body) {
      saveView.apply.disabled = false;
      setSaveStatus(saveView.status, "无法访问本地服务。", "err");
      return;
    }
    if (prepared.body.ok !== true) {
      var error = prepared.body.error || {};
      renderCommitStatuses(error.details && error.details.status);
      saveView.raw.textContent = JSON.stringify(redactedForDisplay(prepared.body), null, 2);
      if (error.code === "SAVE_CONFIRM_REQUIRED") {
        var details = error.details || {};
        if (details.target_path) {
          renderConfirmBlock(details);
          setSaveStatus(
            saveView.status,
            "即将覆盖已有文件：请核对上面的绝对路径与预计备份路径，勾选确认后再点一次「应用到工程」。",
            "busy"
          );
        } else {
          /* 例如「确认之后目标文件才出现」：此时没有可展示的已确认路径，
             必须让用户重新走一遍准备流程（会带上新的备份路径）。 */
          hideConfirmBlock();
          setSaveStatus(
            saveView.status,
            describeSaveError(error) + "请重新点「应用到工程」走一遍确认。",
            "err"
          );
        }
        saveView.apply.disabled = false;
        return;
      }
      saveView.apply.disabled = false;
      setSaveStatus(saveView.status, describeSaveError(error), "err");
      return;
    }

    renderConfirmBlock(prepared.body, false);
    setSaveStatus(saveView.status, "正在应用草稿并回读校验…", "busy");

    var commitBody = { token: prepared.body.token, mode: mode, draft: draft };
    if (mode === SAVE_MODE_SAVE_AS) {
      commitBody.target_path = targetPath;
    }
    var committed = await request("/api/session/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(commitBody)
    });
    saveView.apply.disabled = false;
    if (!committed.body) {
      setSaveStatus(saveView.status, "无法访问本地服务。", "err");
      return;
    }
    saveView.raw.textContent = JSON.stringify(redactedForDisplay(committed.body), null, 2);

    if (committed.body.ok !== true) {
      var commitError = committed.body.error || {};
      renderCommitStatuses(commitError.details && commitError.details.status);
      setSaveStatus(saveView.status, describeSaveError(commitError), "err");
      return;
    }

    renderCommitStatuses(committed.body.status);
    var backupPath = committed.body.backup_path;
    setSaveStatus(
      saveView.status,
      "已保存到 " + committed.body.target_path + (backupPath ? "（覆盖前备份：" + backupPath + "）" : "（新文件，无需备份）"),
      "ok"
    );
    /* 工程内容现在就是这份草稿：把基线换成刚写入的实际取值，
       后续预览才会从「磁盘上的工程」而不是旧基线恢复。 */
    if (committed.body.status && committed.body.status.applied_values) {
      baselineValues = committed.body.status.applied_values;
      applyValuesToControls(baselineValues);
    }
    if (committed.body.baseline_id) {
      tuner.baselineInfo.textContent =
        "基线 " + committed.body.baseline_id + " · " + formatTime(committed.body.saved_at) +
        " · 已随保存刷新";
    }
  }

  // -- 预设 ---------------------------------------------------------------
  function renderPresets(items) {
    saveView.presetBody.textContent = "";
    var list = items || [];
    if (!list.length) {
      saveView.presetTable.classList.add("hidden");
      saveView.presetEmpty.classList.remove("hidden");
      return;
    }
    saveView.presetTable.classList.remove("hidden");
    saveView.presetEmpty.classList.add("hidden");
    list.forEach(function (item) {
      var row = document.createElement("tr");
      [
        item.name,
        formatTime(item.updated_at),
        item.parameter_count === null || item.parameter_count === undefined ? "—" : item.parameter_count,
        item.blender || "—"
      ].forEach(function (value, index) {
        var cell = document.createElement("td");
        cell.textContent = String(value);
        if (index === 2) {
          cell.className = "num";
        }
        row.appendChild(cell);
      });
      saveView.presetBody.appendChild(row);
    });
  }

  async function loadPresets() {
    var result = await request("/api/presets");
    if (!result.body || result.body.ok !== true) {
      setSaveStatus(saveView.presetStatus, "预设列表读取失败。", "err");
      return;
    }
    renderPresets(result.body.presets);
  }

  async function onSavePreset() {
    var name = saveView.presetName.value ? saveView.presetName.value.trim() : "";
    if (!name) {
      setSaveStatus(saveView.presetStatus, "请先填写预设名称（支持中文）。", "err");
      return;
    }
    if (!baselineValues) {
      setSaveStatus(saveView.presetStatus, "尚未建立基线：无法校验草稿，请先建立基线。", "err");
      return;
    }
    saveView.presetSave.disabled = true;
    setSaveStatus(saveView.presetStatus, "正在保存预设…", "busy");
    var result = await request("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        draft: collectDraft(),
        framing: currentFramingOptions()
      })
    });
    saveView.presetSave.disabled = false;
    if (!result.body) {
      setSaveStatus(saveView.presetStatus, "无法访问本地服务。", "err");
      return;
    }
    if (result.body.ok !== true) {
      setSaveStatus(saveView.presetStatus, describeSaveError(result.body.error), "err");
      return;
    }
    setSaveStatus(
      saveView.presetStatus,
      (result.body.updated ? "已更新预设「" : "已保存预设「") + result.body.name + "」（" +
        result.body.parameter_count + " 项参数）",
      "ok"
    );
    loadPresets();
  }

  async function initTuner() {
    tuner.card.classList.remove("hidden");
    saveView.presetCard.classList.remove("hidden");
    saveView.card.classList.remove("hidden");
    if (!schemaLoaded) {
      var loaded = await loadSchema();
      if (!loaded) {
        return;
      }
    }
    await ensureBaseline(false);
    loadPresets();
  }

  function startPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
    }
    pollTimer = window.setInterval(refreshStatus, POLL_INTERVAL_MS);
  }

  nodes.reconnect.addEventListener("click", onReconnect);
  nodes.refreshScene.addEventListener("click", onRefreshScene);
  tuner.baselineBtn.addEventListener("click", function () {
    ensureBaseline(true);
  });
  tuner.restoreBtn.addEventListener("click", onRestoreBaseline);
  tuner.resetBtn.addEventListener("click", onResetUi);

  /* 取景控件 */
  framingView.mode.addEventListener("change", onFramingModeChange);
  framingView.margin.addEventListener("input", onFramingMarginInput);
  framingView.reframe.addEventListener("click", onReframe);
  framingView.refreshBaseline.addEventListener("click", function () {
    ensureBaseline(true);
  });
  updateMarginReadout();
  setFramingSource(framingView.mode.value);
  setFramingStale(false, "");

  /* 保存预设 / 应用到工程 */
  saveView.presetSave.addEventListener("click", onSavePreset);
  saveView.presetRefresh.addEventListener("click", loadPresets);
  saveView.modeSaveAs.addEventListener("change", onSaveModeChange);
  saveView.modeOverwrite.addEventListener("change", onSaveModeChange);
  saveView.apply.addEventListener("click", onCommitApply);
  resetCommitStatuses();
  onSaveModeChange();

  refreshStatus();
  startPolling();
})();
