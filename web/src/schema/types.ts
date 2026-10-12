/**
 * v4 递归 schema 的类型定义。
 *
 * 与后端 `src/server/surface/schema.py` 的 `to_public()` 一一对应。
 * `kind` 是判别字段；**未知 kind 会被降级成只读的 `unknown` 节点**（见 `parse.ts`）——
 * 前端版本比后端旧时，宁可把新参数显示成只读，也不能让它看起来可写。
 */

export type NodeKind =
  | "float"
  | "int"
  | "bool"
  | "enum"
  | "color"
  | "vector"
  | "ramp"
  | "group"
  | "unknown";

export type CostLayer = "L0" | "L1" | "L2" | "L3";

/** 只读原因（稳定枚举）。`unconfirmed_capability` = 探到了但真机插座名未确认。 */
export type ReadonlyReason =
  | "not_found"
  | "rollback_unavailable"
  | "keyframed_or_constrained"
  | "reference_only"
  | "structural"
  | "unconfirmed_capability"
  | "unsupported_kind"
  | string;

export type ValueSource = "scene" | "default" | "draft" | "unsupported" | string;

export interface BindingRef {
  object_type: string;
  object_id: string;
  field: string;
}

export interface EnumOption {
  value: string;
  label: string;
}

/** 所有节点公共部分。 */
export interface NodeBase {
  id: string;
  kind: NodeKind;
  group: string;
  cost: CostLayer;
  label: string;
  supported: boolean;
  editable: boolean;
  active: boolean;
  readonly_reason: ReadonlyReason | null;
  value_source: ValueSource;
  /** 结构性参数：变化会使草稿与保存确认令牌失效。 */
  structural: boolean;
  binding?: BindingRef;
  note?: string;
  unit?: string;
  impact?: Record<string, unknown>;
  reason?: string;
}

export interface ScalarNode extends NodeBase {
  kind: "float" | "int" | "bool";
  value: number | boolean | null;
  baseline: number | boolean | null;
  effective: number | boolean | null;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
}

export interface EnumNode extends NodeBase {
  kind: "enum";
  value: string | null;
  baseline: string | null;
  effective: string | null;
  options: EnumOption[];
  depends_on: string | null;
  options_dynamic?: boolean;
}

export interface ColorNode extends NodeBase {
  kind: "color";
  value: number[] | null;
  baseline: number[] | null;
  effective: number[] | null;
}

export interface VectorNode extends NodeBase {
  kind: "vector";
  value: number[] | null;
  baseline: number[] | null;
  effective: number[] | null;
  axes: string[];
  space: string;
  locked_ratio: boolean;
}

export interface RampPosition {
  value: number;
  baseline: number | null;
  minimum: number;
  maximum: number;
  step?: number;
}

export interface RampColor {
  value: number[];
  baseline: number[] | null;
}

export interface RampElement {
  index: number;
  position: RampPosition;
  color: RampColor;
}

export interface RampInterpolation {
  value: string;
  baseline: string | null;
  options: EnumOption[];
}

/** 色标数量是**结构信息**：只读，且被明确标为 L3。 */
export interface RampElementCount {
  value: number;
  baseline: number | null;
  structural: boolean;
  cost: CostLayer;
  supported: boolean;
  editable: boolean;
  readonly_reason: ReadonlyReason | null;
  minimum: number;
  maximum: number;
}

export interface RampNode extends NodeBase {
  kind: "ramp";
  elements: RampElement[];
  interpolation: RampInterpolation;
  element_count: RampElementCount;
  checks: Array<Record<string, unknown>>;
}

export interface GroupNode extends NodeBase {
  kind: "group";
  children: SurfaceNode[];
}

/** 未知 kind：只用来显示，**永远不可编辑**。 */
export interface UnknownNode extends NodeBase {
  kind: "unknown";
  raw: Record<string, unknown>;
}

export type SurfaceNode =
  | ScalarNode
  | EnumNode
  | ColorNode
  | VectorNode
  | RampNode
  | GroupNode
  | UnknownNode;

export interface SurfaceSchema {
  schema_version: string;
  groups: SurfaceNode[];
  highest_cost: CostLayer;
  surface_baseline_id?: string;
  structure_hash?: string;
  compositor_group?: string;
  degraded?: string[];
}

/** 一份色带草稿值：与后端 `POST /api/v4/preview` 的草稿形状一致。 */
export interface RampDraftValue {
  elements: Array<{ position: number; color: number[] }>;
  interpolation: string;
}

export type SurfaceDraft = Record<string, unknown>;

export interface SurfaceValues {
  [paramId: string]: unknown;
}

export function isGroup(node: SurfaceNode): node is GroupNode {
  return node.kind === "group";
}

export function isRamp(node: SurfaceNode): node is RampNode {
  return node.kind === "ramp";
}

export function isScalar(node: SurfaceNode): node is ScalarNode {
  return node.kind === "float" || node.kind === "int" || node.kind === "bool";
}

export function isEnum(node: SurfaceNode): node is EnumNode {
  return node.kind === "enum";
}
