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
import type { RenderJob, RenderModel, RenderState } from "../api/render-types";
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
    job: null,
    jobLoading: false,
    jobError: null,
    stepOverride: null,
    artifacts: [],
    busy: false,
    notice: null,
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

  async function startJob(): Promise<boolean> {
    const state = store.getState();
    if (!state.selectedModelId) {
      store.setState({
        jobError: { code: "MODEL_NOT_FOUND", message: "请先选择一个模型。", retryable: false },
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

  const actions = {
    bootstrap(): void {
      void loadModels();
    },
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
