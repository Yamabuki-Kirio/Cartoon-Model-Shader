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
import type { ErrorDetail } from "../api/types";
import type { RenderEndpoints } from "../api/render-endpoints";
import type {
  RenderCleanupReport,
  RenderEnvironmentResponse,
  RenderJob,
  RenderModel,
  RenderScheme,
  RenderSchemesResponse,
  RenderState,
  RenderStorageUsage,
} from "../api/render-types";
import { createStore, type Store } from "./store";

/** 单个方案的草稿（规格 §六：切换方案必须保留各自的草稿）。 */
export interface SchemeDraft {
  /** 只含**可调**参数的当前值；只读项不进来。 */
  values: Record<string, number | boolean | string>;
  /**
   * 提交前的本地范围校验错误：参数 id → 错误文案。
   * 非空时**阻止提交**（规格 §六：超范围显示错误并阻止提交）。
   */
  errors: Record<string, string>;
}

export interface RenderUiState {
  models: RenderModel[];
  modelsLoading: boolean;
  modelsError: ErrorDetail | null;
  /** 当前选中的模型 id。 */
  selectedModelId: string | null;
  /** 渲染设置。 */
  mode: "faithful" | "enhanced";
  autoFrame: boolean;
  /**
   * 渲染方案清单（PR #8）。null = 尚未读取。
   * 默认方案取自响应的 `default`，**不在前端硬编码**。
   */
  schemes: RenderScheme[];
  /** 当前选中的方案 id。 */
  schemeId: string | null;
  schemeLoading: boolean;
  schemeError: ErrorDetail | null;
  /**
   * 每个方案各自的草稿：`schemeId → SchemeDraft`。
   * 这样从「体积雾」切到「经典」再切回来，之前调好的雾密度还在，
   * 且经典方案里**不会**混入体积雾参数。
   */
  schemeDrafts: Record<string, SchemeDraft>;
  /** 辉光微调（仅 enhanced 有意义；null = 用主脚本默认修复值）。 */
  glowThreshold: number | null;
  glowStrength: number | null;
  glowSize: number | null;
  glowSmoothness: number | null;
  glowQuality: "High" | "Medium" | "Low" | null;
  /** 当前任务（活跃或最近一次）。 */
  job: RenderJob | null;
  jobLoading: boolean;
  jobError: ErrorDetail | null;
  /** 用户在向导里手选的步骤（0-based）；null = 跟随任务状态。 */
  stepOverride: number | null;
  /** 结果产物（终态后拉取）。 */
  artifacts: { artifact_id: string; name: string; size: number }[];
  busy: boolean;
  notice: string | null;
  /** 资源治理：存储用量（容量 / 任务数 / 可清理空间）。 */
  storage: RenderStorageUsage | null;
  /** 最近一次清理预演/执行的结果。 */
  lastCleanup: RenderCleanupReport | null;
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
}

/** 任务是否还在跑（需要继续轮询）。 */
function isActive(state: RenderState): boolean {
  return state !== "success" && state !== "failed" && state !== "rejected" && state !== "diagnostic" && state !== "cancelled";
}

/**
 * 一个方案的**可调**参数默认值。
 *
 * 只读参数（`readonly=true`）不进草稿：它们不参与提交，只作展示。
 */
function defaultDraft(scheme: RenderScheme): SchemeDraft {
  const values: Record<string, number | boolean | string> = {};
  for (const param of scheme.parameters) {
    if (param.readonly) {
      continue;
    }
    values[param.id] = param.default as number | boolean | string;
  }
  return { values, errors: {} };
}

/**
 * 本地范围校验（与提交前的第一道闸）。
 *
 * **只做服务端已声明的范围检查**，不发明新约束：服务端 schema 没给
 * `minimum`/`maximum` 就不判。真正的裁决权始终在服务端 —— 这里只是为了
 * 让「超范围」在按下按钮之前就可见，并按规格阻止提交。
 */
function validateDraft(scheme: RenderScheme, draft: SchemeDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const param of scheme.parameters) {
    if (param.readonly) {
      continue;
    }
    const value = draft.values[param.id];
    if (param.kind === "number") {
      if (typeof value !== "number" || !Number.isFinite(value)) {
        errors[param.id] = "请输入数字";
        continue;
      }
      if (param.minimum !== undefined && value < param.minimum) {
        errors[param.id] = `不能小于 ${param.minimum}`;
      } else if (param.maximum !== undefined && value > param.maximum) {
        errors[param.id] = `不能大于 ${param.maximum}`;
      }
    } else if (param.kind === "enum") {
      const choices = param.choices ?? [];
      if (choices.length > 0 && !choices.includes(String(value))) {
        errors[param.id] = `只能是 ${choices.join(" / ")}`;
      }
    }
  }
  return errors;
}

export function createRenderStore(options: RenderStoreOptions) {
  const { endpoints } = options;
  const pollIntervalMs = options.pollIntervalMs ?? 1200;

  const store: Store<RenderUiState> = createStore<RenderUiState>({
    models: [],
    modelsLoading: false,
    modelsError: null,
    selectedModelId: null,
    mode: "faithful",
    autoFrame: false,
    schemes: [],
    schemeId: null,
    schemeLoading: false,
    schemeError: null,
    schemeDrafts: {},
    glowThreshold: null,
    glowStrength: null,
    glowSize: null,
    glowSmoothness: null,
    glowQuality: null,
    job: null,
    jobLoading: false,
    jobError: null,
    stepOverride: null,
    artifacts: [],
    busy: false,
    notice: null,
    storage: null,
    lastCleanup: null,
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

  /**
   * 读取固定渲染方案清单（PR #8 规格 §六）。
   *
   * 默认方案取服务端返回的 `default` —— 前端**不硬编码**。为每个方案准备一份
   * 独立草稿，只包含服务端声明为可调的参数。
   */
  async function loadSchemes(): Promise<void> {
    store.setState({ schemeLoading: true, schemeError: null });
    try {
      const payload: RenderSchemesResponse = await endpoints.listSchemes();
      const schemes = payload.schemes ?? [];
      const prevSchemeId = store.getState().schemeId;
      const drafts = { ...store.getState().schemeDrafts };
      for (const scheme of schemes) {
        if (!drafts[scheme.id]) {
          drafts[scheme.id] = defaultDraft(scheme);
        }
      }
      // 选中项失效（方案被下线）时回落到服务端默认方案。
      const keep = schemes.some((s) => s.id === prevSchemeId);
      const chosen = keep ? (prevSchemeId as string) : (payload.default ?? schemes[0]?.id ?? null);
      store.setState({
        schemes,
        schemeId: chosen,
        schemeDrafts: drafts,
        schemeLoading: false,
        schemeError: null,
      });
    } catch (error) {
      store.setState({ schemeLoading: false, schemeError: toDetail(error) });
    }
  }

  /** 当前方案的草稿（不存在则按该方案默认值建一份）。 */
  function draftFor(schemeId: string | null): SchemeDraft {
    const state = store.getState();
    if (!schemeId) {
      return { values: {}, errors: {} };
    }
    const existing = state.schemeDrafts[schemeId];
    if (existing) {
      return existing;
    }
    const scheme = state.schemes.find((s) => s.id === schemeId);
    return scheme ? defaultDraft(scheme) : { values: {}, errors: {} };
  }

  /**
   * 组装要提交的 `scheme_parameters`。
   *
   * **只发可调参数**（规格 §一：只读项不提交）；只读参数不在这里出现，
   * 服务端也不需要它们。
   */
  function schemeParametersFor(schemeId: string | null): Record<string, unknown> {
    const scheme = store.getState().schemes.find((s) => s.id === schemeId);
    if (!scheme) {
      return {};
    }
    const draft = draftFor(schemeId);
    const out: Record<string, unknown> = {};
    for (const param of scheme.parameters) {
      if (param.readonly) {
        continue;
      }
      const value = draft.values[param.id];
      if (value !== undefined) {
        out[param.id] = value;
      }
    }
    return out;
  }

  /** 当前选中方案是否存在未解决的本地校验错误。 */
  function currentSchemeInvalid(): boolean {
    const state = store.getState();
    const scheme = state.schemes.find((s) => s.id === state.schemeId);
    if (!scheme) {
      return false;
    }
    // 无论是否已缓存在 state 里，都按当前值现算一遍，避免草稿过期。
    return Object.keys(validateDraft(scheme, draftFor(scheme.id))).length > 0;
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
    // 规格 §六：方案参数超范围时必须**阻止提交**，而不是发出去等服务端拒绝。
    const scheme = state.schemes.find((s) => s.id === state.schemeId);
    if (scheme) {
      const errors = validateDraft(scheme, draftFor(scheme.id));
      if (Object.keys(errors).length > 0) {
        const draft = { ...draftFor(scheme.id), errors };
        store.setState({
          schemeDrafts: { ...state.schemeDrafts, [scheme.id]: draft },
          jobError: {
            code: "RENDER_SCHEME_PARAM_INVALID",
            message: "渲染方案参数超出允许范围，已阻止提交。",
            retryable: false,
            hint: Object.entries(errors)
              .map(([id, msg]) => `${id}：${msg}`)
              .join("；"),
          },
        });
        return false;
      }
    }
    store.setState({ busy: true, jobError: null, notice: null, artifacts: [], stepOverride: null });
    try {
      const job = await endpoints.createJob({
        model_id: state.selectedModelId,
        mode: state.mode,
        auto_frame: state.autoFrame,
        // 方案 id 与参数：只读项不提交；不传 scheme 时服务端用注册表默认方案。
        ...(state.schemeId ? { scheme: state.schemeId } : {}),
        scheme_parameters: schemeParametersFor(state.schemeId),
        glow_threshold: state.glowThreshold,
        glow_strength: state.glowStrength,
        glow_size: state.glowSize,
        glow_smoothness: state.glowSmoothness,
        glow_quality: state.glowQuality,
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

  const actions = {
    bootstrap(): void {
      void loadModels();
      void loadStorage();
      void loadEnvironment();
      void loadSchemes();
    },
    loadStorage,
    loadEnvironment,
    loadSchemes,
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
    /**
     * 切换渲染方案。
     *
     * 每个方案的草稿各自独立保留（规格 §六）：切走再切回来，之前的取值还在，
     * 且**不会**把体积雾参数带进经典方案 —— 参数只存在于当前方案的草稿里。
     */
    selectScheme(schemeId: string): void {
      const state = store.getState();
      if (!state.schemes.some((s) => s.id === schemeId)) {
        return;
      }
      const drafts = { ...state.schemeDrafts };
      if (!drafts[schemeId]) {
        const scheme = state.schemes.find((s) => s.id === schemeId) as RenderScheme;
        drafts[schemeId] = defaultDraft(scheme);
      }
      store.setState({ schemeId, schemeDrafts: drafts });
    },
    /**
     * 修改当前方案的某个可调参数。立即做范围校验并把错误写进草稿，
     * 供 UI 就地提示（提交时还会再拦一次）。
     */
    setSchemeParam(paramId: string, value: number | boolean | string): void {
      const state = store.getState();
      const scheme = state.schemes.find((s) => s.id === state.schemeId);
      if (!scheme) {
        return;
      }
      const param = scheme.parameters.find((p) => p.id === paramId);
      // 只读参数不接受写入 —— 它们不参与提交。
      if (!param || param.readonly) {
        return;
      }
      const next: SchemeDraft = {
        values: { ...draftFor(scheme.id).values, [paramId]: value },
        errors: {},
      };
      const errors = validateDraft(scheme, next);
      next.errors = errors;
      store.setState({ schemeDrafts: { ...state.schemeDrafts, [scheme.id]: next } });
    },
    /** 当前方案的草稿（组件渲染动态控件时读取）。 */
    schemeDraft(): SchemeDraft {
      return draftFor(store.getState().schemeId);
    },
    /** 当前方案是否存在未解决的本地校验错误。 */
    schemeInvalid(): boolean {
      return currentSchemeInvalid();
    },
    /** 当前选中的方案对象（含参数定义）。 */
    currentScheme(): RenderScheme | null {
      const state = store.getState();
      return state.schemes.find((s) => s.id === state.schemeId) ?? null;
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
