/**
 * 渲染工作流（HTTP 编排层）的前端类型。
 *
 * 与调参工作台（`./types.ts`）刻意分开：渲染任务的 id 是服务端 opaque 的
 * `model-*` / `rj-*`，前端从不持有真实路径。
 */

export type RenderState =
  | "queued"
  | "preflight"
  | "analyzing_materials"
  | "waiting_confirmation"
  | "preparing"
  | "rendering"
  | "verifying"
  | "success"
  | "failed"
  | "rejected"
  | "diagnostic"
  | "cancelled";

export interface RenderModel {
  model_id: string;
  name: string;
  fingerprint: string;
  material_count: number;
}

export interface RenderModelsResponse {
  ok: true;
  models: RenderModel[];
}

export interface RenderMaterial {
  material: string;
  proposed_class: string | null;
  requires_user: boolean;
  confirmed: boolean;
}

export interface RenderArtifact {
  artifact_id: string;
  name: string;
  size: number;
}

export interface RenderJob {
  ok: true;
  job_id: string;
  model_id: string;
  model_name: string;
  fingerprint: string;
  state: RenderState;
  step: number;
  stage: string;
  step_count: number;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  is_terminal: boolean;
  materials: RenderMaterial[];
  confirm_url: string | null;
  confirmed_count: number;
  required_count: number;
  /** 资源治理：是否被用户固定、占用字节数、保留状态。 */
  pinned: boolean;
  disk_bytes: number;
  retention_state: string;
  /** 运行终态（run_contract 词表）。取消不是正式终态，故为 null。 */
  outcome: RenderOutcome | null;
  /** 是否可交付。只有 success 为 true。 */
  shippable: boolean;
  /** 稳定错误码（非成功时）。 */
  error_code: string | null;
  /** 兼容降级判定：未收到 render.done，但退出码 0 且确有成品图。 */
  degraded: boolean;
  /** 取消原因（仅 cancelled 有值）。 */
  cancel_reason: string | null;
  /** 使用的渲染方案（PR #8）：id / 版本 / 实际生效参数。旧任务记录可能为 null。 */
  scheme: RenderJobScheme | null;
  logs?: string[] | null;
  artifacts?: RenderArtifact[] | null;
}

/**
 * 运行终态（`render_pipeline/run_contract.py` 词表）。
 *
 * 服务端已归一化：未知 token 一律归为 `failed`。前端**不重复做归一化** ——
 * 只消费 `outcome` 与 `shippable`，避免两处词表漂移。
 */
export type RenderOutcome = "success" | "rejected" | "diagnostic" | "failed";

export interface RenderStorageUsage {
  ok: true;
  job_count: number;
  pinned_count: number;
  total_bytes: number;
  max_total_bytes: number;
  max_jobs: number;
  max_age_days: number;
  evictable_jobs: number;
  reclaimable_bytes: number;
  roots: string[];
}

export interface RenderCleanupReport {
  ok: true;
  dry_run: boolean;
  removed_jobs: string[];
  released_bytes: number;
  failed: { job_id: string; error: string }[];
}

export interface RenderMaterialsResponse {
  ok: true;
  job_id: string;
  materials: RenderMaterial[];
  confirmed_count: number;
  required_count: number;
  confirm_url: string | null;
  waiting: boolean;
}

/** 环境诊断的单项检查（规格 §5.3）。 */
export interface RenderEnvironmentCheck {
  id: string;
  /** ok / warning / error。 */
  status: "ok" | "warning" | "error";
  message: string;
  /** 下一步该做什么（error/warning 时才有）。 */
  hint?: string;
}

/** 环境诊断响应。**绝不含本机绝对路径**（规格 §5.4）。 */
export interface RenderEnvironmentResponse {
  ok: true;
  ready: boolean;
  /** error 级检查项 id；非空即应禁用「开始渲染」。 */
  blockers: string[];
  checks: RenderEnvironmentCheck[];
}

/** 三步教程的固定文案（规格 §5.2）。 */
export const RENDER_ONBOARDING_STEPS = [
  "打开 Blender 测试工程",
  "启用 Blender MCP，确认端口 9876",
  "检查环境并刷新模型列表",
] as const;

export interface RenderArtifactsResponse {
  ok: true;
  job_id: string;
  artifacts: RenderArtifact[];
}

export interface CreateRenderJobBody {
  model_id: string;
  mode: "faithful" | "enhanced";
  auto_frame: boolean;
  /**
   * 固定方案 id（PR #8）。不传 = 用服务端注册表的默认方案。
   * ★ 前端**不硬编码**默认方案 —— 从 `GET /api/render/schemes` 的 `default` 取。
   */
  scheme?: string;
  /**
   * 方案参数。**只有服务端 schema 声明为可调的参数**才能放进来；
   * 未知参数会被服务端拒绝（不是忽略）。
   */
  scheme_parameters?: Record<string, unknown>;
  /** 【已弃用】灯光配置别名：legacy → classic。新代码请用 scheme。 */
  lighting?: "volume_fog" | "legacy";
  /** 【已弃用】体积雾雾密度别名。新代码请用 scheme_parameters.fog_density。 */
  fog_density?: number;
  /** 辉光微调（仅 enhanced 模式有意义；不传则用主脚本默认修复值 0.55/0.85/0.62/0.35/High）。 */
  glow_threshold?: number | null;
  glow_strength?: number | null;
  glow_size?: number | null;
  glow_smoothness?: number | null;
  glow_quality?: "High" | "Medium" | "Low" | null;
}

/**
 * 方案参数定义（`GET /api/render/schemes`，PR #8 规格 §一）。
 *
 * **所有字段由服务端生成**，客户端不得自定义。前端只按 `kind` 渲染控件：
 * `number` → 数字框 + 滑块；`boolean` → 开关；`enum` → 下拉框。
 * `readonly=true` 的参数只展示值与说明，不参与提交。
 */
export interface RenderSchemeParam {
  id: string;
  kind: "number" | "boolean" | "enum";
  label: string;
  default: unknown;
  description: string;
  /** 只读项：展示但不提交（未经真实 Blender 标定的参数属此类）。 */
  readonly: boolean;
  minimum?: number;
  maximum?: number;
  step?: number;
  choices?: string[];
}

/** 一个可选渲染方案（前端方案卡片的所有展示项都来自这里）。 */
export interface RenderScheme {
  id: string;
  version: number;
  label: string;
  description: string;
  /** 默认方案由服务端标记，前端不硬编码。 */
  recommended: boolean;
  /** 推荐用途。 */
  recommended_use: string;
  /** 是否包含体积雾。 */
  has_volume_fog: boolean;
  /** 性能影响提示。 */
  performance_note: string;
  /** 可调整参数数量（只读项不计）。 */
  adjustable_count: number;
  parameters: RenderSchemeParam[];
}

/** `GET /api/render/schemes` 响应（只读接口，不需要写令牌）。 */
export interface RenderSchemesResponse {
  ok: true;
  /** 默认方案 id —— 由服务端决定。 */
  default: string;
  schemes: RenderScheme[];
}

/** 任务实际使用的方案（任务详情展示：方案 / 版本 / 实际生效参数）。 */
export interface RenderJobScheme {
  id: string;
  version: number | null;
  parameters: Record<string, unknown>;
}

/** 成功路径的 6 步（规格 §5）。 */
export const RENDER_STEPS = [
  "选择模型",
  "材质分析",
  "材质确认",
  "渲染设置",
  "执行渲染",
  "查看结果",
] as const;

/**
 * 中间态标签（非终态）。
 */
export const RENDER_STATE_LABELS: Record<string, string> = {
  queued: "排队中",
  preflight: "预检中",
  analyzing_materials: "材质分析中",
  waiting_confirmation: "等待材质确认",
  preparing: "准备渲染",
  rendering: "渲染中",
  verifying: "校验产物",
};

/**
 * 终态标签。**关键**：`rejected` 显示「需要材质确认」而不是「渲染失败」；
 * `diagnostic` 明确「诊断图，不可交付」（规格 §3.3 / §3.4）。
 */
export const RENDER_TERMINAL_LABELS: Record<string, string> = {
  success: "渲染成功",
  rejected: "需要材质确认",
  diagnostic: "诊断图（不可交付）",
  failed: "渲染失败",
  cancelled: "已取消",
};

export function isTerminalState(state: RenderState): boolean {
  return (
    state === "success" ||
    state === "failed" ||
    state === "rejected" ||
    state === "diagnostic" ||
    state === "cancelled"
  );
}

/**
 * 可取消状态（与服务端 `render_state.CANCELABLE_STATES` 一致）。
 *
 * **正式渲染阶段（preparing / rendering / verifying）一律不可取消**（规格 §7.2~§7.3）：
 * 那是在写正式产物的窗口，强行中断可能损坏工程。前端据此禁用按钮并说明原因，
 * 而不是先发请求再看服务端报错。
 */
export const RENDER_CANCELABLE_STATES: readonly RenderState[] = [
  "queued",
  "analyzing_materials",
  "waiting_confirmation",
];

export function canCancelJob(job: RenderJob): boolean {
  return !isTerminalState(job.state) && RENDER_CANCELABLE_STATES.includes(job.state);
}

/** 正式渲染中（不可取消）时的说明文案（规格 §7.3 原文）。 */
export const RENDER_CANCEL_LOCKED_MESSAGE =
  "Blender 正在写入正式产物。为避免损坏工程，本阶段暂不支持取消。";

/**
 * 是否可交付。**只信服务端的 `shippable`**，前端不做二次推断：
 * 文件存在、退出码为 0、甚至有图，都不足以判可交付（规格 §3.3）。
 */
export function isShippableJob(job: RenderJob): boolean {
  return job.shippable === true && job.outcome === "success" && job.state === "success";
}
