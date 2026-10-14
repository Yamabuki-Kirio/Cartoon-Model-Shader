/** 渲染工作流接口。路径集中在这里，组件不拼字符串。与调参 `Endpoints` 并列。 */

import type { ApiClient } from "./client";
import type {
  CommitBody,
  CommitPrepareBody,
  CommitPrepareResponse,
  CommitResponse,
  PresetDetailResponse,
  PresetSaveBody,
} from "./types";
import type {
  CreateRenderJobBody,
  RenderArtifactsResponse,
  RenderCleanupReport,
  RenderEnvironmentResponse,
  RenderJob,
  RenderMaterialsResponse,
  RenderModelsResponse,
  RenderStorageUsage,
} from "./render-types";

const MODELS_PATH = "/api/render/models";
const JOBS_PATH = "/api/render/jobs";
const STORAGE_PATH = "/api/render/storage";
const ENVIRONMENT_PATH = "/api/render/environment";
const PRESETS_PATH = "/api/presets";
const COMMIT_PREPARE_PATH = "/api/session/commit/prepare";
const COMMIT_PATH = "/api/session/commit";

export class RenderEndpoints {
  constructor(private readonly client: ApiClient) {}

  /** 模型列表（只读）。 */
  listModels(options: { signal?: AbortSignal } = {}): Promise<RenderModelsResponse> {
    return this.client.get(MODELS_PATH, options);
  }

  /** 重新扫描模型目录（写接口，带令牌）。 */
  refreshModels(options: { signal?: AbortSignal } = {}): Promise<RenderModelsResponse> {
    return this.client.post(`${MODELS_PATH}/refresh`, {}, options);
  }

  /** 创建渲染任务（写接口）。同模型已有活跃任务时服务端会复用。 */
  createJob(body: CreateRenderJobBody, options: { signal?: AbortSignal } = {}): Promise<RenderJob> {
    return this.client.post(JOBS_PATH, body, options);
  }

  listJobs(options: { signal?: AbortSignal } = {}): Promise<RenderJob[]> {
    return this.client.get(JOBS_PATH, options);
  }

  getJob(jobId: string, options: { signal?: AbortSignal } = {}): Promise<RenderJob> {
    return this.client.get(`${JOBS_PATH}/${encodeURIComponent(jobId)}`, options);
  }

  retryJob(jobId: string, options: { signal?: AbortSignal } = {}): Promise<RenderJob> {
    return this.client.post(`${JOBS_PATH}/${encodeURIComponent(jobId)}/retry`, {}, options);
  }

  cancelJob(jobId: string, options: { signal?: AbortSignal } = {}): Promise<RenderJob> {
    return this.client.post(`${JOBS_PATH}/${encodeURIComponent(jobId)}/cancel`, {}, options);
  }

  materials(jobId: string, options: { signal?: AbortSignal } = {}): Promise<RenderMaterialsResponse> {
    return this.client.get(`${JOBS_PATH}/${encodeURIComponent(jobId)}/materials`, options);
  }

  artifacts(jobId: string, options: { signal?: AbortSignal } = {}): Promise<RenderArtifactsResponse> {
    return this.client.get(`${JOBS_PATH}/${encodeURIComponent(jobId)}/artifacts`, options);
  }

  /** 产物文件 URL（按 opaque artifact_id）。供 <img>/<a> 直接引用。 */
  artifactUrl(jobId: string, artifactId: string): string {
    return `${JOBS_PATH}/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}`;
  }

  openOutput(jobId: string, options: { signal?: AbortSignal } = {}): Promise<{ ok: true; opened: boolean }> {
    return this.client.post(`${JOBS_PATH}/${encodeURIComponent(jobId)}/open-output`, {}, options);
  }

  /** 固定 / 取消固定任务（写接口）。固定后不会被自动清理。 */
  pinJob(
    jobId: string,
    pinned: boolean,
    options: { signal?: AbortSignal } = {}
  ): Promise<RenderJob> {
    return this.client.post(
      `${JOBS_PATH}/${encodeURIComponent(jobId)}/pin`,
      {},
      { ...options, query: { pinned } }
    );
  }

  /** 存储用量（只读）。 */
  storage(options: { signal?: AbortSignal } = {}): Promise<RenderStorageUsage> {
    return this.client.get(STORAGE_PATH, options);
  }

  /** 执行安全清理（写接口）。dryRun=true 只预演。 */
  cleanupStorage(
    dryRun: boolean,
    options: { signal?: AbortSignal } = {}
  ): Promise<RenderCleanupReport> {
    return this.client.post(`${STORAGE_PATH}/cleanup`, {}, { ...options, query: { dry_run: dryRun } });
  }

  /**
   * 渲染环境只读诊断（规格 §5）。
   *
   * 只读、不带令牌，也不接受任何路径参数 —— 路径全部由服务端自己解析，
   * 响应里只有脱敏标签。用于渲染前的红黄绿预检与「开始」按钮的就绪判断。
   */
  environment(options: { signal?: AbortSignal } = {}): Promise<RenderEnvironmentResponse> {
    return this.client.get(ENVIRONMENT_PATH, options);
  }

  // -- 与调参域联动：保存为预设 / 草稿另存为新 .blend --------------------
  //
  // 复用**既有**预设与 commit 端点，不另开一套保存协议：
  //   * 预设目录与文件名全部由服务端决定，接口不接受任何路径参数；
  //   * 保存默认「另存为」，覆盖必须走既有确认单号 + 备份机制。

  /** 把当前渲染参数存为预设（写接口）。服务端扫描并拒绝任何本机路径。 */
  savePreset(body: PresetSaveBody, options: { signal?: AbortSignal } = {}): Promise<PresetDetailResponse> {
    return this.client.post(PRESETS_PATH, body, options);
  }

  /**
   * 保存准备（只校验不写盘）：签发一次性短效令牌。
   *
   * 覆盖已有文件且未带 `confirm` 时不签发令牌，而是回 `SAVE_CONFIRM_REQUIRED`（409），
   * `details` 里带目标与备份的绝对路径 —— 调用方应把它当「确认提示」处理。
   */
  commitPrepare(
    body: CommitPrepareBody,
    options: { signal?: AbortSignal } = {}
  ): Promise<CommitPrepareResponse> {
    return this.client.post(COMMIT_PREPARE_PATH, body, options);
  }

  /** 执行保存：消费令牌 → 应用草稿 → 回读校验 → 备份 → 保存工程。 */
  commitApply(
    body: CommitBody,
    options: { signal?: AbortSignal } = {}
  ): Promise<CommitResponse> {
    return this.client.post(COMMIT_PATH, body, options);
  }
}
