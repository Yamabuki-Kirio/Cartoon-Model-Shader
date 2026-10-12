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

  /**
   * 「覆盖需要二次确认」—— 这**不是**失败。
   *
   * 服务端在覆盖已有文件且没收到 `confirm` 时回 409 + 该错误码，
   * `details` 里带目标绝对路径与预计备份路径。前端要把它渲染成确认提示，
   * 而不是一条红色错误：用户还没做错任何事，只是还没看清路径。
   */
  get isSaveConfirmRequired(): boolean {
    return this.code === "SAVE_CONFIRM_REQUIRED";
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
  /** 隔离预览结果：隐藏了哪些对象、有没有完整写回。 */
  isolation?: IsolationSummary | null;
  project?: ProjectStateSummary | null;
  [key: string]: unknown;
}

/**
 * 提交给服务端的取景选项。
 *
 * `isolate_material` 给出材质名时做**隔离预览**：渲染时临时隐藏不使用该材质的网格对象，
 * 渲染后逐个写回原值。`null` / 缺省 / 空串都表示不隔离 —— 服务端把空串归一成「未指定」。
 */
export interface FramingSubmitOptions {
  mode: string;
  margin: number;
  isolate_material?: string | null;
}

/**
 * 隔离预览的结果。
 *
 * `restored_ok` 只在服务端确实回读了每个被隐藏对象、且逐项一致时才是 `true`；
 * 拿不到证据一律 `null`（未知）—— 与输出设置同一条准则，**不报假绿**。
 */
export interface IsolationSummary {
  requested: boolean;
  material: string | null;
  applied: boolean | null;
  reason: string | null;
  objects_kept: string[];
  objects_hidden: string[];
  restored_ok: boolean | null;
  mismatches: Record<string, unknown>;
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

// -- 预设（MVP-03）------------------------

export interface PresetSummary {
  preset_id: string;
  name: string;
  schema: string;
  created_at: string;
  updated_at: string;
  pipeline_mode: string;
  framing_mode: string;
  framing_margin: number;
  preview_quality: string;
  /** L0 参数条数。 */
  parameter_count: number;
  active_count: number;
  /**
   * v4 参数面草稿条数。
   *
   * `0` 表示这是一份**只含 L0 的旧预设**（v4 参数面加进来之前存的）；
   * 界面据此区分「L0 预设」与「完整预设」，不需要先把明细拉下来。
   */
  surface_count: number;
}

export interface PresetDetail extends PresetSummary {
  /** L0 参数：`{configured_value, effective_value, active}` 三元组。 */
  parameters: Record<string, unknown>;
  /**
   * v4 参数面草稿：`参数 id -> 裸取值`。
   *
   * 与 `parameters` 的区别不是风格问题：`surface` 的取值在写入时就是最终值，
   * 没有需要迁移的显示标签，所以没有三元组包装。
   */
  surface: Record<string, unknown>;
}

export interface PresetSkip {
  file: string;
  code: string;
  message: string;
}

export interface PresetListResponse {
  ok: true;
  protocol: string;
  /** **可展示**的存储位置（如 `%LOCALAPPDATA%\...`），绝不含本机绝对路径。 */
  storage: string;
  presets: PresetSummary[];
  /** 读不动的文件：不会被静默忽略，带着错误码与原因列出来。 */
  skipped: PresetSkip[];
  quality_tiers: Record<string, Record<string, unknown>>;
  pipeline_modes: string[];
  framing_modes: string[];
  defaults: Record<string, unknown>;
}

export interface PresetDetailResponse {
  ok: true;
  preset: PresetDetail;
}

/** 新建 / 覆盖保存预设的请求体。 */
export interface PresetSaveBody {
  name: string;
  pipeline_mode?: string;
  framing_mode?: string;
  framing_margin?: number;
  preview_quality?: string;
  parameters?: Record<string, unknown>;
  surface?: Record<string, unknown>;
}

// -- 应用到工程（commit）------------------------

export type SaveMode = "save_as" | "overwrite";

/** `POST /api/session/commit/prepare` 的请求体。 */
export interface CommitPrepareBody {
  mode: SaveMode;
  draft: Record<string, unknown>;
  /** 仅 `save_as` 使用：绝对路径且以 `.blend` 结尾。 */
  target_path?: string | null;
  /** 覆盖已有文件必须为 `true`（先看清绝对路径与备份位置再确认）。 */
  confirm?: boolean;
  /**
   * 第一次 prepare 返回的确认单号。第二次确认（`confirm: true`）**必须**原样携带：
   * 服务端据此沿用第一次已展示的备份路径（两步之间跨过整秒边界也不会漂移），
   * 并复核基线 / 草稿 / 模式 / 目标都没变。第一次调用不要带。
   */
  confirmation_id?: string | null;
  surface_draft?: Record<string, unknown> | null;
  structure_hash?: string | null;
}

/**
 * 准备阶段成功时的响应：**一次性短效令牌** + 目标 / 备份的绝对路径。
 *
 * `confirmation_required` 为 `true` 表示这次会覆盖已有文件、且已生成过备份路径。
 * 覆盖**未经确认**时服务端不会走到这里，而是回 `SAVE_CONFIRM_REQUIRED`（409），
 * 其 `details` 里带着同样的两条路径 —— 前端把那个错误当成「确认提示」来渲染。
 */
export interface CommitPrepareResponse {
  ok: true;
  token: string;
  mode: string;
  target_path: string;
  backup_path: string | null;
  warnings: string[];
  baseline_id: string;
  draft_hash: string;
  parameter_count: number;
  surface_parameter_count: number;
  structure_hash: string | null;
  normalized: Array<Record<string, unknown>>;
  expires_at: string;
  expires_in_seconds: number;
  confirmation_required: boolean;
  /**
   * 本次准备的确认单号：确认覆盖时必须随第二次 prepare 回传，服务端据此
   * 沿用已展示的备份路径并复核各项未变。不需要确认的流程同样会给出。
   */
  confirmation_id: string;
}

/** `POST /api/session/commit` 的请求体（令牌 + 与 prepare 逐项一致的内容）。 */
export interface CommitBody {
  token: string;
  mode: SaveMode;
  draft: Record<string, unknown>;
  target_path?: string | null;
  surface_draft?: Record<string, unknown> | null;
  structure_hash?: string | null;
}

/** 保存失败时 `details.status` 的形状。四个状态各自独立，不合并成一句。 */
export interface CommitStatus {
  applied: boolean;
  readback_verified: boolean;
  mismatches: Array<Record<string, unknown>>;
  backup: {
    required: boolean;
    created: boolean;
    path: string | null;
    path_confirmed: string | null;
  };
  rollback: {
    attempted: boolean;
    verified: boolean;
    mismatches: Array<Record<string, unknown>>;
    code: string | null;
    error: string | null;
    reason: string | null;
    l0?: Record<string, unknown> | null;
    cel?: Record<string, unknown> | null;
  };
  saved: boolean;
  target_path: string;
  mode: string;
  /** 失败时草稿一定还在界面上（服务端也这么承诺）。 */
  draft_preserved: boolean;
  applied_surface: Record<string, unknown>;
  surface_readback_verified: boolean;
  surface_mismatches: Array<Record<string, unknown>>;
  save?: {
    path_after?: string | null;
    path_matches?: boolean;
    file_exists?: boolean;
    is_dirty_after?: boolean;
  } | null;
  baseline_refresh_error?: ErrorDetail | null;
  [key: string]: unknown;
}

export interface CommitResponse {
  ok: true;
  mode: string;
  target_path: string;
  backup_path: string | null;
  saved: boolean;
  status: CommitStatus;
  steps: string[];
  normalized: Array<Record<string, unknown>>;
  baseline_id: string | null;
  baseline_refreshed: boolean;
  saved_at: string;
  warnings: string[];
}
