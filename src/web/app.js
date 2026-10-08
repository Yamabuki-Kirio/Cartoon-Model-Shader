/* Cartoon-Model-Shader · MVP-01 前端
 * 只读展示：连接状态 + 当前工程/场景摘要。
 * 所有动态文本一律用 textContent 写入，绝不拼接 HTML，避免对象名注入。
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
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

  var lastStatus = null;
  var sceneLoaded = false;
  var pollTimer = null;

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

  function startPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
    }
    pollTimer = window.setInterval(refreshStatus, POLL_INTERVAL_MS);
  }

  nodes.reconnect.addEventListener("click", onReconnect);
  nodes.refreshScene.addEventListener("click", onRefreshScene);

  refreshStatus();
  startPolling();
})();
