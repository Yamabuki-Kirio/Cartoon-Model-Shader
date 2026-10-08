/* Cartoon-Model-Shader · 前端
 * 连接状态 + 当前工程/场景摘要 + L0 曝光调参与预览。
 * 所有动态文本一律用 textContent 写入，绝不拼接 HTML，避免对象名注入。
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var JOB_POLL_INTERVAL_MS = 200;
  /* L0 参数停止调整后触发预览的去抖窗口（需求：250–400ms） */
  var DEBOUNCE_MS = 300;
  var DEFAULT_TARGET = "127.0.0.1:9876";

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
    jobRaw: el("job-raw")
  };

  var lastStatus = null;
  var sceneLoaded = false;
  var pollTimer = null;

  // -- 调参状态 ---------------------------------------------------------
  var schemaLoaded = false;
  var controlsById = {};
  var baselineValues = null;
  var debounceTimer = null;
  var activeJobId = null;
  var jobPollTimer = null;
  var previewInFlight = false;

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
    try {
      var response = await fetch(path, options || {});
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
    setBusy(false);
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
    setPreviewStatus("(" + (detail.code || "ERROR") + ") " + (detail.message || "预览失败"), "err");
    tuner.jobRaw.textContent = JSON.stringify(detail, null, 2);
  }

  function clearJobPolling() {
    if (jobPollTimer) {
      window.clearInterval(jobPollTimer);
      jobPollTimer = null;
    }
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
      (spec.options || []).forEach(function (option) {
        var opt = document.createElement("option");
        opt.value = option;
        opt.textContent = option;
        input.appendChild(opt);
      });
    }
    input.id = "ctl-" + spec.id;
    input.dataset.paramId = spec.id;
    wrap.appendChild(input);

    var meta = document.createElement("p");
    meta.className = "control-meta";
    if (spec.type === "float") {
      meta.textContent = "范围 [" + spec.minimum + ", " + spec.maximum + "] · " + spec.target;
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
    Object.keys(controlsById).forEach(function (paramId) {
      var entry = controlsById[paramId];
      if (!(paramId in values)) {
        return;
      }
      var value = values[paramId];
      if (entry.spec.type === "float") {
        entry.input.value = String(value);
      } else if (entry.spec.options && entry.spec.options.indexOf(value) === -1) {
        var opt = document.createElement("option");
        opt.value = value;
        opt.textContent = value;
        entry.input.appendChild(opt);
        entry.input.value = value;
      } else {
        entry.input.value = value;
      }
      updateReadout(entry);
    });
  }

  async function ensureBaseline(force) {
    if (baselineValues && !force) {
      return true;
    }
    setPreviewStatus("正在建立内存基线…", "busy");
    var result = await request("/api/session/baseline", { method: "POST" });
    if (!result.body || !result.body.ok) {
      showTunerError(result.body && result.body.error);
      return false;
    }
    baselineValues = result.body.values || {};
    tuner.baselineInfo.textContent =
      "基线 " + result.body.baseline_id + " · " + formatTime(result.body.captured_at) +
      " · Blender " + (result.body.blender || "?");
    applyValuesToControls(baselineValues);
    setPreviewStatus("基线已建立，可开始调参", "ok");
    return true;
  }

  async function onRestoreBaseline() {
    clearJobPolling();
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
    if (result.body.verified) {
      setPreviewStatus("已恢复到基线，逐项校验通过", "ok");
    } else {
      setPreviewStatus("已恢复，但存在不一致项（见任务详情）", "err");
    }
  }

  function schedulePreview() {
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
    if (!baselineValues) {
      var ok = await ensureBaseline(false);
      if (!ok) {
        return;
      }
    }
    clearJobPolling();
    previewInFlight = true;
    setPreviewStatus("已提交预览，等待 Blender…", "busy");
    var result = await request("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: collectDraft() })
    });
    if (!result.body || !result.body.ok) {
      previewInFlight = false;
      showTunerError(result.body && result.body.error);
      return;
    }
    activeJobId = result.body.job_id;
    pollJob(result.body.job_id);
  }

  function pollJob(jobId) {
    clearJobPolling();
    jobPollTimer = window.setInterval(async function () {
      var result = await request("/api/jobs/" + jobId);
      if (!result.body) {
        return;
      }
      var job = result.body;
      tuner.jobRaw.textContent = JSON.stringify(job, null, 2);
      if (job.status === "queued" || job.status === "running") {
        setPreviewStatus("Blender 处理中…（" + (job.steps.join(" → ") || job.status) + "）", "busy");
        return;
      }
      clearJobPolling();
      previewInFlight = false;
      if (job.status === "superseded") {
        return;
      }
      if (job.status === "failed") {
        showTunerError(job.error);
        return;
      }
      if (job.status === "done" && job.result) {
        var url = job.result.preview_url + "?t=" + Date.now();
        tuner.img.onload = function () {
          tuner.img.classList.remove("hidden");
          tuner.placeholder.classList.add("hidden");
        };
        tuner.img.src = url;
        var resolution = job.result.render_resolution || [];
        var verified = job.result.restore_verified ? "已回滚基线（校验通过）" : "回滚校验未通过";
        setPreviewStatus(
          "预览完成 · " + resolution[0] + "×" + resolution[1] + " · " + verified,
          job.result.restore_verified ? "ok" : "err"
        );
      }
    }, JOB_POLL_INTERVAL_MS);
  }

  function onResetUi() {
    if (!baselineValues) {
      setPreviewStatus("尚未建立基线，无法复位", "err");
      return;
    }
    applyValuesToControls(baselineValues);
    schedulePreview();
  }

  async function initTuner() {
    tuner.card.classList.remove("hidden");
    if (!schemaLoaded) {
      var loaded = await loadSchema();
      if (!loaded) {
        return;
      }
    }
    await ensureBaseline(false);
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

  refreshStatus();
  startPolling();
})();
