/** 与后端错误信封 / 任务模型对应的类型。 */

export interface ErrorDetail {
  code: string;
  message: string;
  retryable: boolean;
  hint?: string | null;
  details?: Record<string, unknown> | null;
}

export class AppError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly hint: string | null;
  readonly details: Record<string, unknown> | null;
  readonly status: number;

  constructor(detail: Partial<ErrorDetail> & { code?: string }, status = 0) {
    super(detail.message ?? "请求失败。");
    this.name = "AppError";
    this.code = detail.code ?? "INTERNAL_ERROR";
    this.retryable = detail.retryable ?? false;
    this.hint = detail.hint ?? null;
    this.details = detail.details ?? null;
    this.status = status;
  }

  /** 结构失效类错误：草稿与保存确认令牌都作废，必须刷新基线。 */
  get isStructureFatal(): boolean {
    return this.code === "STRUCTURE_CHANGED" || this.code === "IDENTITY_MISSING";
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    ((error as { name?: string }).name === "AbortError" ||
      (error as { code?: string }).code === "ABORT_ERR")
  );
}

export interface JobError extends ErrorDetail {}

export interface JobState {
  job_id: string;
  seq: number;
  status: "queued" | "running" | "done" | "failed" | "superseded" | string;
  created_at: string;
  updated_at: string;
  superseded: boolean;
  steps: string[];
  kind?: string | null;
  external_changes?: ExternalChange[];
  framing_request?: Record<string, unknown> | null;
  result?: JobResult | null;
  error?: JobError | null;
}

/** 工程脏标记快照（O3）。只含布尔值与文件名 —— 绝对路径不出现。 */
export interface ProjectStateSummary {
  dirty_at_baseline?: boolean | null;
  dirty_after_preview?: boolean | null;
  /** 仅当「基线干净 → 预览后变脏」时为真：这才是需要向用户解释的情形。 */
  dirty_flagged?: boolean;
  file_name?: string | null;
}

export interface JobResult {
  preview_url: string;
  render_resolution?: number[] | null;
  size_bytes?: number | null;
  applied?: Record<string, unknown>;
  applied_surface?: Record<string, unknown>;
  restored?: Record<string, unknown>;
  restored_surface?: Record<string, unknown>;
  baseline_id?: string;
  surface_baseline_id?: string;
  structure_hash?: string;
  restore_verified?: boolean;
  surface_restore_verified?: boolean;
  surface_restore_mismatches?: Array<Record<string, unknown>>;
  external_changes?: ExternalChange[];
  framing?: Record<string, unknown>;
  project?: ProjectStateSummary | null;
  [key: string]: unknown;
}

/** 值层外部改动：只提示，不作废草稿。 */
export interface ExternalChange {
  id: string;
  baseline?: unknown;
  current?: unknown;
}

export interface SurfaceBaselinePublic {
  available: boolean;
  schema_version?: string;
  baseline_id?: string;
  captured_at?: string;
  blender?: string;
  structure_hash?: string;
  compositor_group?: string;
  found_groups?: number;
  declared_groups?: number;
  degraded?: string[];
  identities?: Array<{ object_type: string; name: string; source: string }>;
  values?: Record<string, unknown>;
  /** 建立基线时的工程脏标记（只含布尔值与文件名）。 */
  project?: { dirty?: boolean | null; file_name?: string | null } | null;
  /** 只有「刚建立基线」的那次响应会带（那是首张预览的 job） */
  preview_url?: string | null;
  error?: ErrorDetail | null;
}

export interface BaselineResponse {
  baseline_id: string;
  captured_at: string;
  blender: string;
  preview_resolution?: number[];
  surface?: SurfaceBaselinePublic | null;
  job_id?: string | null;
  job_status?: string | null;
  preview_url?: string | null;
  [key: string]: unknown;
}

export interface FramingContext {
  frame_current?: number;
  camera?: string | null;
  baseline?: {
    established?: boolean;
    baseline_id?: string | null;
    captured_at?: string | null;
    stale?: boolean;
    reasons?: string[];
    warnings?: string[];
  };
  framing_modes?: {
    default?: string;
    default_margin?: number;
    margin_min?: number;
    margin_max?: number;
    modes?: Array<{ id: string; label: string; uses_temporary_camera: boolean }>;
  };
  [key: string]: unknown;
}

export interface BlenderStatus {
  ok: boolean;
  status: string;
  target: string;
  checked_at: string;
  latency_ms?: number | null;
  error?: ErrorDetail | null;
}

export interface ConnectionState {
  status: string;
  target: string;
  latencyMs: number | null;
  checkedAt: string | null;
  message: string | null;
}
