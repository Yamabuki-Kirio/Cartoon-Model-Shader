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
  logs?: string[] | null;
  artifacts?: RenderArtifact[] | null;
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

export interface RenderArtifactsResponse {
  ok: true;
  job_id: string;
  artifacts: RenderArtifact[];
}

export interface CreateRenderJobBody {
  model_id: string;
  mode: "faithful" | "enhanced";
  auto_frame: boolean;
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

export function isTerminalState(state: RenderState): boolean {
  return (
    state === "success" ||
    state === "failed" ||
    state === "rejected" ||
    state === "diagnostic" ||
    state === "cancelled"
  );
}
