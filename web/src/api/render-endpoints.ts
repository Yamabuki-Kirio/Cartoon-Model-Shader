/** 渲染工作流接口。路径集中在这里，组件不拼字符串。与调参 `Endpoints` 并列。 */

import type { ApiClient } from "./client";
import type {
  CreateRenderJobBody,
  RenderArtifactsResponse,
  RenderJob,
  RenderMaterialsResponse,
  RenderModelsResponse,
} from "./render-types";

const MODELS_PATH = "/api/render/models";
const JOBS_PATH = "/api/render/jobs";

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
}
