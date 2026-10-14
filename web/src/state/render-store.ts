/**
 * 渲染工作流的独立状态（Preact 订阅式 store）。
 *
 * 与调参工作台（`state/workspace.ts`）**互不干扰**：渲染任务有自己的
 * model_id / job_id / 材质清单，但**不**再维护第二份调参草稿 ——
 * 调参仍走 `Workspace`。两者只在「查看结果」处汇合（都只是展示产物）。
 *
 * 所有会改状态的路径都收在 actions 里，组件只调用 actions。
 */

import { AppError, isAbortError } from "../api/types";
import type { ErrorDetail, SaveMode } from "../api/types";
import type { RenderEndpoints } from "../api/render-endpoints";
import type {
  RenderArtifact,
  RenderCleanupReport,
  RenderEnvironmentResponse,
  RenderJob,
  RenderModel,
  RenderState,
  RenderStorageUsage,
} from "../api/render-types";
import { createStore, type Store } from "./store";

export interface RenderUiState {
  models: RenderModel[];
  modelsLoading: boolean;
  modelsError: ErrorDetail | null;
  /** 当前选中的模型 id。 */
  selectedModelId: string | null;
  /** 渲染设置。 */
  mode: "faithful" | "enhanced";
  autoFrame: boolean;
  /** 灯光配置（2026-10-11 体积雾灯光增量）。 */
  lighting: "volume_fog" | "legacy";
  /** 体积雾雾密度 0.0–0.1（仅 volume_fog 有意义）。 */
  fogDensity: number;
  /** 辉光微调（仅 enhanced 有意义；null = 用主脚本默认修复值）。 */
  glowThreshold: number | null;
  glowStrength: number | null;
  glowSize: number | null;
  glowSmoothness: number | null;
  glowQuality: "High" | "Medium" | "Low" | null;
  /** 材质准入口径：strict=未解决即拒绝（默认）；diagnostic=洋红棋盘诊断图（不可交付）。 */
  materialPolicy: "strict" | "diagnostic";
  /** 当前任务（活跃或最近一次）。 */
  job: RenderJob | null;
  jobLoading: boolean;
  jobError: ErrorDetail | null;
  /** 用户在向导里手选的步骤（0-based）；null = 跟随任务状态。 */
  stepOverride: number | null;
  /** 结果产物（终态后拉取）。复用 api 层类型，别在这里另写一份 —— 重复定义必然漂移。 */
  artifacts: RenderArtifact[];
  busy: boolean;
  notice: string | null;
  /** 资源治理：存储用量（容量 / 任务数 / 可清理空间）。 */
  storage: RenderStorageUsage | null;
  /** 最近一次清理预演/执行的结果。 */
  lastCleanup: RenderCleanupReport | null;
  /**
   * 保存结果（与渲染终态**分开**）。
   *
   * 保存失败绝不能改渲染任务的成功状态 —— 渲染成功就是成功。所以失败只体现在
   * 这里，页面单独显示「渲染成功，但工程保存失败」。
   */
  saveStatus: { ok: boolean; message: string } | null;
  /** 环境诊断（规格 §5）：就绪状态 + 12 项检查。null = 尚未读取。 */
  environment: RenderEnvironmentResponse | null;
  environmentLoading: boolean;
  /** 首次使用教程是否展开（规格 §5.2：首次进入默认展开，可折叠，可随时重开）。 */
  tourOpen: boolean;
}

function toDetail(error: unknown): ErrorDetail {
  if (error instanceof AppError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      hint: error.hint,
      details: error.details,
    };
  }
  return {
    code: "INTERNAL_ERROR",
    message: error instanceof Error ? error.message : String(error),
    retryable: true,
  };
}

export interface RenderStoreOptions {
  endpoints: RenderEndpoints;
  pollIntervalMs?: number;
  /**
   * 当前调参草稿（由调参域持有）。
   *
   * 「草稿另存为新 .blend」要把**这一份**草稿交给 commit 协议，而草稿本体住在
   * v4/调参 store 里 —— 所以由外部注入读取器，渲染 store 不自己复制一份。
   * 没注入时草稿为空对象（渲染产物下载不受影响）。
   */
  getDraft?: () => Record<string, unknown>;
}

/** 任务是否还在跑（需要继续轮询）。 */
function isActive(state: RenderState): boolean {
  return state !== "success" && state !== "failed" && state !== "rejected" && state !== "diagnostic" && state !== "cancelled";
}

export function createRenderStore(options: RenderStoreOptions) {
  const { endpoints } = options;
  const pollIntervalMs = options.pollIntervalMs ?? 1200;
  /** 草稿读取器另取别名：saveDraftAs 的入参也叫 options，会把它遮蔽掉。 */
  const getDraftOption = options.getDraft;

  const store: Store<RenderUiState> = createStore<RenderUiState>({
    models: [],
    modelsLoading: false,
    modelsError: null,
    selectedModelId: null,
    mode: "faithful",
    autoFrame: false,
    lighting: "volume_fog",
    fogDensity: 0.018,
    glowThreshold: null,
    glowStrength: null,
    glowSize: null,
    glowSmoothness: null,
    glowQuality: null,
    materialPolicy: "strict",
    job: null,
    jobLoading: false,
    jobError: null,
    stepOverride: null,
    artifacts: [],
    busy: false,
    notice: null,
    storage: null,
    lastCleanup: null,
    saveStatus: null,
    environment: null,
    environmentLoading: false,
    tourOpen: true,
  });

  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let pollAbort: AbortController | null = null;
  /** 追踪的是哪个任务的轮询；换任务时旧轮询必须作废。 */
  let trackedJobId: string | null = null;

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (pollAbort) {
      pollAbort.abort();
      pollAbort = null;
    }
  }

  function currentStep(job: RenderJob | null): number {
    if (store.getState().stepOverride !== null) {
      return store.getState().stepOverride as number;
    }
    return job ? job.step : 0;
  }

  async function loadModels(): Promise<void> {
    store.setState({ modelsLoading: true, modelsError: null });
    try {
      const payload = await endpoints.listModels();
      const models = payload.models ?? [];
      store.setState({
        models,
        modelsLoading: false,
        // 选中项失效（模型被移除）时清空，避免指向一个不存在的 id。
        selectedModelId: models.some((m) => m.model_id === store.getState().selectedModelId)
          ? store.getState().selectedModelId
          : (models[0]?.model_id ?? null),
      });
    } catch (error) {
      store.setState({ modelsLoading: false, modelsError: toDetail(error) });
    }
  }

  async function refreshModels(): Promise<void> {
    store.setState({ modelsLoading: true, modelsError: null });
    try {
      const payload = await endpoints.refreshModels();
      store.setState({ models: payload.models ?? [], modelsLoading: false });
    } catch (error) {
      store.setState({ modelsLoading: false, modelsError: toDetail(error) });
    }
  }

  /** 轮询一个任务直到终态。换任务时旧轮询作废。 */
  function trackJob(jobId: string): void {
    stopPolling();
    trackedJobId = jobId;
    const tick = async (): Promise<void> => {
      if (trackedJobId !== jobId) {
        return;
      }
      const controller = new AbortController();
      pollAbort = controller;
      try {
        const job = await endpoints.getJob(jobId, { signal: controller.signal });
        if (trackedJobId !== jobId) {
          return;
        }
        store.setState({ job, jobLoading: false, jobError: null });
        if (isActive(job.state)) {
          pollTimer = setTimeout(() => void tick(), pollIntervalMs);
        } else {
          // 终态：拉一次产物，供「查看结果」直接展示。
          void loadArtifacts(jobId);
        }
      } catch (error) {
        if (isAbortError(error)) {
          return;
        }
        if (trackedJobId !== jobId) {
          return;
        }
        store.setState({ jobError: toDetail(error), jobLoading: false });
        // 网络类错误继续重试；其余停下等用户动作。
        const detail = toDetail(error);
        if (detail.retryable) {
          pollTimer = setTimeout(() => void tick(), pollIntervalMs);
        }
      }
    };
    void tick();
  }

  async function loadArtifacts(jobId: string): Promise<void> {
    try {
      const payload = await endpoints.artifacts(jobId);
      if (trackedJobId !== jobId) {
        return;
      }
      store.setState({ artifacts: payload.artifacts ?? [] });
    } catch {
      // 产物拉取失败不影响任务状态展示。
    }
  }

  async function loadEnvironment(): Promise<void> {
    store.setState({ environmentLoading: true });
    try {
      const report = await endpoints.environment();
      store.setState({ environment: report, environmentLoading: false });
    } catch {
      // 诊断读取失败不阻断页面：保持上一份（或 null），由就绪判断按「未就绪」处理。
      store.setState({ environmentLoading: false });
    }
  }

  async function startJob(): Promise<boolean> {
    const state = store.getState();
    if (!state.selectedModelId) {
      store.setState({
        jobError: { code: "MODEL_NOT_FOUND", message: "请先选择一个模型。", retryable: false },
      });
      return false;
    }
    // 规格 §5.4：环境未就绪时**前端就先拒**，把具体缺项列出来，
    // 而不是发一个注定被服务端 503 拒绝的请求。
    const env = state.environment;
    if (env && !env.ready) {
      store.setState({
        jobError: {
          code: "RENDER_ENVIRONMENT_NOT_READY",
          message: "环境检查未通过，暂时不能开始渲染。",
          retryable: false,
          hint: env.checks
            .filter((c) => c.status === "error")
            .map((c) => c.hint ?? c.message)
            .join("；"),
        },
      });
      return false;
    }
    if (state.busy) {
      return false;
    }
    store.setState({ busy: true, jobError: null, notice: null, artifacts: [], stepOverride: null });
    try {
      const job = await endpoints.createJob({
        model_id: state.selectedModelId,
        mode: state.mode,
        auto_frame: state.autoFrame,
        lighting: state.lighting,
        fog_density: state.fogDensity,
        glow_threshold: state.glowThreshold,
        glow_strength: state.glowStrength,
        glow_size: state.glowSize,
        glow_smoothness: state.glowSmoothness,
        glow_quality: state.glowQuality,
        material_policy: state.materialPolicy,
      });
      store.setState({ job, busy: false, stepOverride: null });
      trackJob(job.job_id);
      return true;
    } catch (error) {
      store.setState({ busy: false, jobError: toDetail(error) });
      return false;
    }
  }

  async function retryJob(): Promise<boolean> {
    const job = store.getState().job;
    if (!job) {
      return false;
    }
    store.setState({ busy: true, jobError: null });
    try {
      const next = await endpoints.retryJob(job.job_id);
      store.setState({ job: next, busy: false, artifacts: [], stepOverride: null });
      trackJob(next.job_id);
      return true;
    } catch (error) {
      store.setState({ busy: false, jobError: toDetail(error) });
      return false;
    }
  }

  async function cancelJob(): Promise<boolean> {
    const job = store.getState().job;
    if (!job) {
      return false;
    }
    store.setState({ busy: true, jobError: null });
    try {
      const next = await endpoints.cancelJob(job.job_id);
      stopPolling();
      store.setState({ job: next, busy: false });
      return true;
    } catch (error) {
      store.setState({ busy: false, jobError: toDetail(error) });
      return false;
    }
  }

  async function openOutput(): Promise<void> {
    const job = store.getState().job;
    if (!job) {
      return;
    }
    try {
      const result = await endpoints.openOutput(job.job_id);
      if (!result.opened) {
        store.setState({ notice: "无法自动打开输出目录，请手动在输出根目录中查找。" });
      }
    } catch (error) {
      store.setState({ notice: toDetail(error).message });
    }
  }

  /** 选择某个历史任务继续查看（如从任务列表进入）。 */
  function selectJob(job: RenderJob): void {
    store.setState({ job, artifacts: [], stepOverride: null });
    if (isActive(job.state)) {
      trackJob(job.job_id);
    } else {
      void loadArtifacts(job.job_id);
    }
  }

  async function loadStorage(): Promise<void> {
    try {
      const usage = await endpoints.storage();
      store.setState({ storage: usage });
    } catch {
      // 存储用量读取失败不影响主流程。
    }
  }

  async function pinJob(pinned: boolean): Promise<void> {
    const job = store.getState().job;
    if (!job) {
      return;
    }
    try {
      const next = await endpoints.pinJob(job.job_id, pinned);
      store.setState({
        job: next,
        notice: pinned ? "已固定该任务，自动清理不会删除它。" : "已取消固定。",
      });
      void loadStorage();
    } catch (error) {
      store.setState({ jobError: toDetail(error) });
    }
  }

  async function cleanupStorage(dryRun: boolean): Promise<RenderCleanupReport | null> {
    store.setState({ busy: true });
    try {
      const report = await endpoints.cleanupStorage(dryRun);
      store.setState({
        busy: false,
        lastCleanup: report,
        notice: dryRun
          ? `预演：可清理 ${report.removed_jobs.length} 个任务，释放约 ${formatBytes(report.released_bytes)}。`
          : `已清理 ${report.removed_jobs.length} 个任务，释放 ${formatBytes(report.released_bytes)}。`,
      });
      await loadStorage();
      return report;
    } catch (error) {
      store.setState({ busy: false, jobError: toDetail(error) });
      return null;
    }
  }

  /** 把当前渲染参数存为预设。目录与文件名全部由服务端决定。 */
  async function saveRenderPreset(name: string): Promise<boolean> {
    const job = store.getState().job;
    const trimmed = name.trim();
    if (!trimmed) {
      store.setState({ notice: "请先给预设起个名字。" });
      return false;
    }
    // 参数取自任务 settings（服务端持久化的那次提交），不是当前 UI 的临时值 ——
    // 否则「渲染用的参数」与「存下来的参数」可能不是同一份。
    const parameters = { ...(job?.settings ?? {}) };
    if (job) {
      parameters.model_id = job.model_id;
    }
    store.setState({ busy: true });
    try {
      const saved = await endpoints.savePreset({ name: trimmed, parameters });
      store.setState({ busy: false, notice: `已保存预设「${saved.preset.name}」。` });
      return true;
    } catch (error) {
      store.setState({ busy: false, notice: toDetail(error).message });
      return false;
    }
  }

  /**
   * 把当前调参草稿**另存为**新 `.blend`（默认口径，绝不自动覆盖用户工程）。
   *
   * 走既有 commit 两步协议：prepare 签发一次性令牌 → apply 消费令牌写盘。
   * 覆盖必须由调用方显式传 `mode: "overwrite"` 并带 `confirm`，服务端会先回
   * `SAVE_CONFIRM_REQUIRED` 让用户看清目标路径与备份位置。
   *
   * ★ 保存失败**不改渲染任务的成功状态** —— 渲染成功就是成功，保存是另一件事。
   *   失败只写 `saveStatus`，页面单独显示「渲染成功，但工程保存失败」。
   */
  async function saveDraftAs(
    targetPath: string,
    options: { mode?: SaveMode; confirm?: boolean; confirmationId?: string | null } = {}
  ): Promise<boolean> {
    const mode = options.mode ?? "save_as";
store.setState({ busy: true, saveStatus: null });
    // 注意别写成 `options.getDraft` —— options 已被本函数的入参遮蔽，
    // 真正的读取器在工厂参数上（renderStoreOptions）。
    const draft = getDraftOption ? getDraftOption() : {};
    try {
      const prepared = await endpoints.commitPrepare({
        mode,
        draft,
        target_path: mode === "save_as" ? targetPath : null,
        confirm: options.confirm ?? false,
        confirmation_id: options.confirmationId ?? null,
      });
      const applied = await endpoints.commitApply({
        token: prepared.token,
        mode: prepared.mode as SaveMode,
        draft,
        target_path: prepared.target_path,
      });
      store.setState({
        busy: false,
        saveStatus: { ok: true, message: "工程已保存。" },
      });
      void applied;
      return true;
    } catch (error) {
      const detail = toDetail(error);
      store.setState({
        busy: false,
        saveStatus: {
          ok: false,
          message: `渲染成功，但工程保存失败：${detail.message}`,
        },
      });
      return false;
    }
  }

  function clearSaveStatus(): void {
    store.setState({ saveStatus: null });
  }

  const actions = {
    bootstrap(): void {
      void loadModels();
      void loadStorage();
      void loadEnvironment();
    },
    loadStorage,
    loadEnvironment,
    setTourOpen(open: boolean): void {
      store.setState({ tourOpen: open });
    },
    pinJob,
    cleanupStorage,
    loadModels,
    refreshModels,
    startJob,
    retryJob,
    cancelJob,
    openOutput,
    saveRenderPreset,
    saveDraftAs,
    clearSaveStatus,
    selectJob,
    selectModel(modelId: string): void {
      store.setState({ selectedModelId: modelId });
    },
    setMode(mode: "faithful" | "enhanced"): void {
      store.setState({ mode });
    },
    setAutoFrame(enabled: boolean): void {
      store.setState({ autoFrame: enabled });
    },
    setLighting(lighting: "volume_fog" | "legacy"): void {
      store.setState({ lighting });
    },
    setFogDensity(density: number): void {
      store.setState({ fogDensity: density });
    },
    setGlow(key: "threshold" | "strength" | "size" | "smoothness", value: number | null): void {
      if (key === "threshold") store.setState({ glowThreshold: value });
      else if (key === "strength") store.setState({ glowStrength: value });
      else if (key === "size") store.setState({ glowSize: value });
      else store.setState({ glowSmoothness: value });
    },
    setGlowQuality(quality: "High" | "Medium" | "Low" | null): void {
      store.setState({ glowQuality: quality });
    },
    setMaterialPolicy(policy: "strict" | "diagnostic"): void {
      store.setState({ materialPolicy: policy });
    },
    gotoStep(step: number): void {
      store.setState({ stepOverride: step });
    },
    clearStepOverride(): void {
      store.setState({ stepOverride: null });
    },
    clearError(): void {
      store.setState({ jobError: null });
    },
    clearNotice(): void {
      store.setState({ notice: null });
    },
    dispose(): void {
      trackedJobId = null;
      stopPolling();
    },
    stepOf(job: RenderJob | null): number {
      return currentStep(job);
    },
    /** 产物文件 URL（按 opaque artifact_id）。 */
    artifactUrl(jobId: string, artifactId: string): string {
      return endpoints.artifactUrl(jobId, artifactId);
    },
  };

  return { store, actions };
}

export type RenderStore = ReturnType<typeof createRenderStore>;

/** 人类可读的字节数。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}
