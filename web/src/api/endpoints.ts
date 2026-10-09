/** v4 工作台用到的全部接口。路径集中在这里，组件不拼字符串。 */

import type { ApiClient, RequestOptions } from "./client";
import type {
  BaselineResponse,
  BlenderStatus,
  FramingContext,
  JobState,
  SurfaceBaselinePublic,
} from "./types";

export interface SurfaceSchemaResponse {
  ok: true;
  schema_version: string;
  groups: unknown[];
  highest_cost: string;
  surface_baseline_id?: string;
  structure_hash?: string;
  compositor_group?: string;
  degraded?: string[];
}

export interface SubmitPreviewResponse {
  ok: true;
  job_id: string;
  seq: number;
  status: string;
  framing?: Record<string, unknown>;
}

export interface PreviewSubmitPayload {
  draft: Record<string, unknown>;
  framing?: { mode: string; margin: number };
  expected_structure_hash?: string;
}

export interface SurfaceBaselineResponse extends SurfaceBaselinePublic {
  ok: true;
}

export class Endpoints {
  constructor(private readonly client: ApiClient) {}

  health(): Promise<{ ok: true; service: string; version: string }> {
    return this.client.get("/api/health");
  }

  blenderStatus(): Promise<BlenderStatus> {
    return this.client.get("/api/blender/status");
  }

  framingContext(): Promise<FramingContext & { ok: true }> {
    return this.client.get("/api/framing/context");
  }

  /** v4 递归 schema（只读，不带令牌）。 */
  surfaceSchema(options: Omit<RequestOptions, "method" | "body"> = {}): Promise<SurfaceSchemaResponse> {
    return this.client.get("/api/v4/surface/schema", options);
  }

  /** v4 基线（只读，不带令牌）。 */
  surfaceBaseline(
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<SurfaceBaselineResponse> {
    return this.client.get("/api/v4/session/baseline", options);
  }

  /** 建立 / 刷新内存基线（写接口；同时采集 v4 拓扑）。 */
  createBaseline(
    body?: { framing?: { mode: string; margin: number } },
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<BaselineResponse & { ok: true }> {
    return this.client.post("/api/session/baseline", body ?? {}, options);
  }

  /** 提交 v4 预览：L0 与 Cel 编进同一任务，只渲染一次。 */
  submitPreview(
    payload: PreviewSubmitPayload,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<SubmitPreviewResponse> {
    return this.client.post("/api/v4/preview", payload, options);
  }

  /** v4 任务状态（与 /api/jobs/{id} 共用同一份存储）。 */
  job(jobId: string, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<JobState> {
    return this.client.get(`/api/v4/jobs/${encodeURIComponent(jobId)}`, options);
  }
}

export const V4_SCHEMA_PATH = "/api/v4/surface/schema";
export const V4_BASELINE_PATH = "/api/v4/session/baseline";
export const V4_PREVIEW_PATH = "/api/v4/preview";
export const V4_JOB_PATH = "/api/v4/jobs";
export const FRAMING_PATH = "/api/framing/context";
export const BLENDER_STATUS_PATH = "/api/blender/status";
