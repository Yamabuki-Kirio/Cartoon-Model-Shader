/** v4 工作台用到的全部接口。路径集中在这里，组件不拼字符串。 */

import type { ApiClient, RequestOptions } from "./client";
import type {
  BaselineResponse,
  BlenderStatus,
  CommitBody,
  CommitPrepareBody,
  CommitPrepareResponse,
  CommitResponse,
  FramingContext,
  FramingSubmitOptions,
  GraphMaterialPublic,
  JobState,
  MaterialGraphSchemaResponse,
  PresetDetailResponse,
  PresetListResponse,
  PresetSaveBody,
  PresetSummary,
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
  framing?: FramingSubmitOptions;
  expected_structure_hash?: string;
  /**
   * 材质图草稿（PR #7）：**opaque 参数 id -> 取值**。
   *
   * 与 `draft` 分开是服务端的契约，不是风格问题：`draft` 会被当成参数面草稿
   * 逐个 id 校验，把材质图 id 混进去只会得到 `PARAM_INVALID`。
   */
  material_draft?: Record<string, unknown>;
  /** 已确认影响范围的共享节点组。未列出的共享组参数会被服务端拒绝。 */
  confirmed_shared_groups?: string[];
  expected_material_structure_hash?: string;
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
    body?: { framing?: FramingSubmitOptions },
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

  // -- 材质图（PR #7）------------------------

  /**
   * 材质图递归 schema（只读，不带令牌）。
   *
   * 需要**材质图基线**：它由 `POST /api/session/baseline` 顺带采集，
   * 没建立之前服务端回 `NO_BASELINE`（而不是给一份空 schema 假装成功）。
   */
  materialGraphSchema(
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<MaterialGraphSchemaResponse> {
    return this.client.get(MATERIAL_GRAPH_SCHEMA_PATH, options);
  }

  /** 单个材质的材质图 schema（含反向追踪与影响路径）。只读，不校验令牌。 */
  materialGraphMaterial(
    materialId: string,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<{ ok: true } & GraphMaterialPublic> {
    return this.client.get(
      `${MATERIAL_GRAPH_PATH}/materials/${encodeURIComponent(materialId)}`,
      options
    );
  }

  // -- 预设 ------------------------

  /** 预设列表（只读，不带令牌）。附带运行设置白名单与可展示的存储位置。 */
  listPresets(
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<PresetListResponse> {
    return this.client.get(PRESETS_PATH, options);
  }

  readPreset(
    presetId: string,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<PresetDetailResponse> {
    return this.client.get(`${PRESETS_PATH}/${encodeURIComponent(presetId)}`, options);
  }

  /** 新建预设（201）。同名会被服务端以 `PRESET_NAME_CONFLICT` 拒绝。 */
  createPreset(
    body: PresetSaveBody,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<PresetDetailResponse> {
    return this.client.post(PRESETS_PATH, body, options);
  }

  /** 覆盖保存：`preset_id` 与 `created_at` 由服务端保留，客户端改不动。 */
  updatePreset(
    presetId: string,
    body: PresetSaveBody,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<PresetDetailResponse> {
    return this.client.put(`${PRESETS_PATH}/${encodeURIComponent(presetId)}`, body, options);
  }

  deletePreset(
    presetId: string,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<{ ok: true; deleted: PresetSummary }> {
    return this.client.del(`${PRESETS_PATH}/${encodeURIComponent(presetId)}`, options);
  }

  // -- 应用到工程 ------------------------

  /**
   * 准备保存：**只校验，不写盘**，成功则签发一次性短效令牌。
   *
   * 覆盖已有文件且没带 `confirm` 时不签发令牌，而是回 `SAVE_CONFIRM_REQUIRED`
   * （409），`details` 里带目标绝对路径与预计备份路径 —— 那正是「先看清再确认」
   * 那一步，前端把它当确认提示渲染，而不是当失败。
   */
  commitPrepare(
    body: CommitPrepareBody,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<CommitPrepareResponse> {
    return this.client.post(COMMIT_PREPARE_PATH, body, options);
  }

  /** 执行保存：消费令牌 → 应用 → 回读 → 备份 → 保存。失败时不写盘，草稿保留。 */
  commitApply(
    body: CommitBody,
    options: Omit<RequestOptions, "method" | "body"> = {}
  ): Promise<CommitResponse> {
    return this.client.post(COMMIT_PATH, body, options);
  }
}

export const V4_SCHEMA_PATH = "/api/v4/surface/schema";
export const V4_BASELINE_PATH = "/api/v4/session/baseline";
export const V4_PREVIEW_PATH = "/api/v4/preview";
export const V4_JOB_PATH = "/api/v4/jobs";
export const MATERIAL_GRAPH_PATH = "/api/material-graph";
export const MATERIAL_GRAPH_SCHEMA_PATH = "/api/material-graph/schema";
export const FRAMING_PATH = "/api/framing/context";
export const BLENDER_STATUS_PATH = "/api/blender/status";
export const PRESETS_PATH = "/api/presets";
export const COMMIT_PREPARE_PATH = "/api/session/commit/prepare";
export const COMMIT_PATH = "/api/session/commit";
