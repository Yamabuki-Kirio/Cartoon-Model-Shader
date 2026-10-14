/**
 * 材质图参数面（PR #7）：解析、分区、索引与请求体拆分。
 *
 * 三条纪律，与 `parse.ts` / `semantics.py` 完全一致：
 *
 * 1. **只降级，不升级。** 前端读不懂的 kind / 等级一律变成只读；任何情况下都不会把
 *    服务端标成「推测」「实验性」「不支持」的参数渲染成可写控件。
 * 2. **字段缺失按「不可用」处理。** `supported` / `editable` 缺省为 false，
 *    绝不因为字段没来就默认成 true。
 * 3. **不猜影响范围。** 受影响对象 / 材质只用服务端给的 `affected_materials`，
 *    前端不按名字反推。
 *
 * 参数 `id` 是服务端生成的 opaque `mg-<hash24>`：真实节点名只出现在 `label` 与
 * `source_label`（展示用），因此**请求体里不可能出现节点路径**。
 */

import type {
  GraphCategory,
  GraphConfidence,
  GraphParamPublic,
  GraphTraceSummary,
  MaterialGraphSchemaResponse,
  SharedConfirmRequest,
} from "../api/types";
import type {
  ColorNode,
  CostLayer,
  EnumNode,
  NodeBase,
  NodeKind,
  RampNode,
  ReadonlyReason,
  ScalarNode,
  SurfaceDraft,
  SurfaceNode,
  UnknownNode,
  VectorNode,
} from "./types";

/** 材质图参数在 schema 里的族名（后端 `material_graph.schema.GROUP`）。 */
export const MATERIAL_GRAPH_GROUP = "material_graph";

/** 材质图节点树的根 id 前缀（后端 `f"{GROUP}.{material.name}"`）。 */
export const MATERIAL_GRAPH_GROUP_PREFIX = `${MATERIAL_GRAPH_GROUP}.`;

// -- 识别等级 ------------------------

export const GRAPH_CONFIDENCES: readonly GraphConfidence[] = [
  "confirmed",
  "inferred",
  "user_confirmed",
  "experimental",
  "unsupported",
];

/**
 * 等级文案（后端 `semantics.LEVELS`）。
 *
 * 「推测」两个字是刻意的：它必须能在一眼扫过时与「已确认」区分开，
 * 否则界面就是在把推测当结论。
 */
export const CONFIDENCE_LABELS: Record<GraphConfidence, string> = {
  confirmed: "已确认",
  inferred: "推测",
  user_confirmed: "用户确认",
  experimental: "实验性",
  unsupported: "不支持",
};

export const CONFIDENCE_TONES: Record<GraphConfidence, "ok" | "warn" | "bad" | "info"> = {
  confirmed: "ok",
  user_confirmed: "ok",
  inferred: "warn",
  experimental: "warn",
  unsupported: "bad",
};

/** 每个等级「为什么是这样」，在参数行里作为说明展示。 */
export const CONFIDENCE_HINTS: Record<GraphConfidence, string> = {
  confirmed: "类型、目标插座与影响路径全部明确。",
  inferred: "影响路径明确，但语义依赖名称或规则推断 —— 默认不写。",
  user_confirmed: "由用户显式确认过。",
  experimental: "需要微扰验证后才能写入。",
  unsupported: "无法安全执行或回滚，仅作展示。",
};

/** 等级未知时（前端比后端旧 / 响应被改坏）的兜底原因码。 */
const REASON_UNCONFIRMED = "unconfirmed_capability";

// -- 展示分区（规格 §「提交 4」的分组顺序）------------------------

export type GraphSectionId =
  | "common"
  | "color_ramp"
  | "numeric"
  | "node_group"
  | "texture"
  | "inferred"
  | "advanced";

export interface GraphSectionSpec {
  id: GraphSectionId;
  label: string;
  /** 该展示分区覆盖的后端 schema 分区。 */
  categories: readonly GraphCategory[];
  hint: string;
}

/**
 * 七个展示分区，**顺序固定**且与规格一致。
 *
 * 「颜色与色带」有意合并 `color` 与 `ramp`：它们是同一件事的两种粒度
 * （单色 vs 渐变），拆成两个折叠面板只会让用户来回找。
 */
export const GRAPH_SECTIONS: readonly GraphSectionSpec[] = [
  {
    id: "common",
    label: "常用参数",
    categories: ["common"],
    hint: "着色器上的常量插座（基础颜色、粗糙度、金属度…），对画面的影响最直接。",
  },
  {
    id: "color_ramp",
    label: "颜色与色带",
    categories: ["color", "ramp"],
    hint: "颜色节点与 ColorRamp。色标取值可编辑，色标数量属结构信息（只读，不提供增删）。",
  },
  {
    id: "numeric",
    label: "数值参数",
    categories: ["numeric"],
    hint: "Value / Math / Map Range 等纯数值节点。",
  },
  {
    id: "node_group",
    label: "节点组参数",
    categories: ["node_group"],
    hint: "节点组实例：组内修改需要先确认影响范围，可能同时改到别的材质。",
  },
  {
    id: "texture",
    label: "纹理控制",
    categories: ["texture"],
    hint: "纹理与坐标节点。本工具只调参数，不替换贴图文件。",
  },
  {
    id: "inferred",
    label: "推测参数",
    categories: ["inferred"],
    hint: "影响路径明确、语义靠规则推断 —— 默认不写，仅供试调参考。",
  },
  {
    id: "advanced",
    label: "高级影响路径",
    categories: ["advanced"],
    hint: "着色器混合 / 转接 / 未归类节点，以及它们到材质输出的完整影响链路。",
  },
];

const CATEGORY_VALUES: readonly GraphCategory[] = [
  "common",
  "color",
  "numeric",
  "ramp",
  "node_group",
  "texture",
  "inferred",
  "advanced",
];

// -- 解析 ------------------------

/** 已知 kind（与 `parse.ts` 的 `KNOWN_KINDS` 同集，去掉 `group`：材质图不产生分组参数）。 */
const KNOWN_KINDS: readonly NodeKind[] = ["float", "int", "bool", "enum", "color", "vector", "ramp"];

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function asStringMatrix(value: unknown): string[][] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((row) => (Array.isArray(row) ? row.map((item) => String(item)) : []));
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asNumberArray(value: unknown): number[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const out: number[] = [];
  for (const item of value) {
    if (typeof item !== "number" || !Number.isFinite(item)) {
      return null;
    }
    out.push(item);
  }
  return out;
}

function asBool(value: unknown): boolean {
  return value === true;
}

/** 等级：不认识的等级串一律降级成 `unsupported`（并保留原文以便排查）。 */
export function parseConfidence(raw: unknown): {
  confidence: GraphConfidence;
  raw: string | null;
} {
  const text = typeof raw === "string" ? raw : null;
  if (text && (GRAPH_CONFIDENCES as readonly string[]).includes(text)) {
    return { confidence: text as GraphConfidence, raw: text };
  }
  return { confidence: "unsupported", raw: text };
}

/** 分区：不认识的分区归入 `advanced`，绝不因为读不懂就丢掉这个参数。 */
export function parseCategory(raw: unknown): GraphCategory {
  const text = typeof raw === "string" ? raw : "";
  return (CATEGORY_VALUES as readonly string[]).includes(text)
    ? (text as GraphCategory)
    : "advanced";
}

/** 最终展示分区：**推测参数永远进「推测参数」区**，不随语义散落到别处。 */
export function sectionOf(category: GraphCategory, confidence: GraphConfidence): GraphSectionId {
  if (confidence === "inferred" || category === "inferred") {
    return "inferred";
  }
  const spec = GRAPH_SECTIONS.find((item) => item.categories.includes(category));
  return spec ? spec.id : "advanced";
}

export function sectionSpec(id: GraphSectionId): GraphSectionSpec {
  const spec = GRAPH_SECTIONS.find((item) => item.id === id);
  // `GRAPH_SECTIONS` 覆盖全部 `GraphSectionId`，这里只是让类型收窄成立。
  return spec ?? GRAPH_SECTIONS[GRAPH_SECTIONS.length - 1];
}

/** 只读原因：后端没给就按等级推一个**准确**的原因码，绝不空着。 */
function readonlyReasonOf(
  declared: unknown,
  confidence: GraphConfidence,
  supported: boolean
): ReadonlyReason | null {
  const text = typeof declared === "string" && declared ? declared : null;
  if (text) {
    return text;
  }
  if (confidence === "inferred") {
    return "inferred_not_confirmed";
  }
  if (confidence === "experimental") {
    return "experimental_semantics";
  }
  if (confidence === "unsupported" || !supported) {
    return REASON_UNCONFIRMED;
  }
  return null;
}

/**
 * 回滚能力说明。
 *
 * 由 `supported` / `editable` 派生：只有可写参数才会进写入计划，而执行器对计划里的
 * 每一步都做快照 + 失败回滚 + 回读确认（`surface.executor`）。因此这句话不是承诺，
 * 是对执行链路的描述。
 */
export function rollbackNote(supported: boolean, editable: boolean): string {
  if (!supported) {
    return "不可回滚：该项没有可信取值，不会产生任何写入。";
  }
  if (!editable) {
    return "无需回滚：只读项不产生写入。";
  }
  return "可回滚：写入失败或回读不一致时，服务端恢复基线并回读确认。";
}

export interface GraphParamMeta {
  /** 该参数所属材质名（由 `parseGraphMaterial` 回填）。 */
  material: string;
  confidence: GraphConfidence;
  /** 后端原始等级串（未知等级时保留原文，便于排查契约漂移）。 */
  rawConfidence: string | null;
  semantic: string;
  semanticLabel: string;
  evidence: string[];
  influencePaths: string[][];
  /**
   * 受影响范围。
   *
   * 语义由服务端决定：**共享节点组**参数给的是会一起被改到的**材质名**；
   * 其余参数给的是使用该材质的**对象名**。界面据此换标签，不自行解释。
   */
  affectedMaterials: string[];
  shared: boolean;
  inGroup: string | null;
  usedOutputs: string[];
  sourceNode: string | null;
  sourceLabel: string | null;
  category: GraphCategory;
  section: GraphSectionId;
  /** 写下去的字段名（`default_value` / `elements` / …）。 */
  bindingField: string | null;
  bindingObjectType: string | null;
  rollback: string;
  /** 强制只读的**前端**理由（等级未知 / kind 未知）；`null` 表示没有额外理由。 */
  frontendDowngrade: string | null;
}

export interface GraphParam {
  node: SurfaceNode;
  meta: GraphParamMeta;
}

function baseOf(
  spec: GraphParamPublic,
  kind: NodeKind,
  options: {
    supported: boolean;
    editable: boolean;
    readonlyReason: ReadonlyReason | null;
    cost: CostLayer;
    meta: Omit<GraphParamMeta, "rollback" | "frontendDowngrade" | "section"> & {
      section?: GraphSectionId;
    };
  }
): NodeBase {
  const { meta } = options;
  return {
    id: spec.id,
    kind,
    group: MATERIAL_GRAPH_GROUP,
    cost: options.cost,
    label: asString(spec.label) || spec.id,
    supported: options.supported,
    editable: options.editable,
    active: options.supported,
    readonly_reason: options.readonlyReason,
    value_source: options.supported ? "scene" : "unsupported",
    structural: false,
    // `impact` 原样带上服务端的事实（等级 / 语义 / 证据 / 影响路径 / 来源节点），
    // 供检查器与测试直接读取，不需要二次推断。
    impact: {
      confidence: meta.confidence,
      level: meta.confidence,
      semantic: meta.semantic,
      semantic_label: meta.semanticLabel,
      evidence: meta.evidence,
      influence_paths: meta.influencePaths,
      affected_materials: meta.affectedMaterials,
      shared: meta.shared,
      in_group: meta.inGroup,
      source_node: meta.sourceNode,
      source_label: meta.sourceLabel,
      used_outputs: meta.usedOutputs,
    },
  };
}

function rampNodeOf(
  spec: GraphParamPublic,
  base: NodeBase,
  elements: GraphParamPublic["elements"]
): RampNode {
  const parsed = (elements ?? []).map((element, index) => {
    const position = asRecord(element?.position);
    const color = asRecord(element?.color);
    return {
      index: asFiniteNumber(element?.index) ?? index,
      position: {
        value: asFiniteNumber(position["value"]) ?? 0,
        baseline: asFiniteNumber(position["baseline"]),
        minimum: asFiniteNumber(position["minimum"]) ?? 0,
        maximum: asFiniteNumber(position["maximum"]) ?? 1,
        step: asFiniteNumber(position["step"]) ?? 0.001,
      },
      color: {
        value: asNumberArray(color["value"]) ?? [0, 0, 0, 1],
        baseline: asNumberArray(color["baseline"]),
      },
    };
  });
  const count = asFiniteNumber(spec.element_count) ?? parsed.length;
  return {
    ...base,
    kind: "ramp",
    elements: parsed,
    interpolation: {
      value: asString(spec.interpolation) || "LINEAR",
      baseline: asString(spec.interpolation) || null,
      // 后端**不**下发可选插值清单（与 Cel 的受管组不同），因此这里如实给空数组；
      // 界面回落成「当前值」单选项，而不是编一份清单出来。
      options: [],
    },
    element_count: {
      value: count,
      baseline: count,
      structural: true,
      cost: "L3",
      supported: true,
      // 色标增删属结构改动：即使后端误标可编辑，前端也按只读处理。
      editable: false,
      readonly_reason: "structural",
      minimum: 2,
      maximum: 32,
    },
    checks: [],
  };
}

/** 解析单个参数。任何读不懂的形态都落到只读，绝不产生可写控件。 */
export function parseGraphParam(raw: unknown): GraphParam | null {
  const source = asRecord(raw);
  const id = asString(source["id"]);
  if (!id) {
    return null;
  }
  const spec = source as unknown as GraphParamPublic;
  const { confidence, raw: rawConfidence } = parseConfidence(source["confidence"]);
  const category = parseCategory(source["category"]);
  const declaredKind = asString(source["kind"]);
  const kindKnown = (KNOWN_KINDS as readonly string[]).includes(declaredKind);
  const frontendDowngrade = kindKnown
    ? confidence === "unsupported"
      ? REASON_UNCONFIRMED
      : null
    : "unsupported_kind";

  // **只降级**：服务端说不可用/不可写，或者前端读不懂，就一定是只读。
  const supported = asBool(source["supported"]) && kindKnown && confidence !== "unsupported";
  const editable = asBool(source["editable"]) && supported && frontendDowngrade === null;
  const readonlyReason = readonlyReasonOf(
    source["readonly_reason"],
    confidence,
    supported
  );
  const binding = asRecord(source["binding"]);
  const value = asRecord(source["value"]);
  const metaCore = {
    confidence,
    rawConfidence,
    semantic: asString(source["semantic"]),
    semanticLabel: asString(source["semantic_label"]) || asString(source["semantic"]),
    evidence: asStringArray(source["evidence"]),
    influencePaths: asStringMatrix(source["influence_paths"]),
    affectedMaterials: asStringArray(source["affected_materials"]),
    shared: asBool(source["shared"]),
    inGroup: asString(source["in_group"]) || null,
    usedOutputs: asStringArray(source["used_outputs"]),
    sourceNode: asString(source["source_node"]) || null,
    sourceLabel: asString(source["source_label"]) || null,
    category,
    bindingField: asString(binding["field"]) || null,
    bindingObjectType: asString(binding["object_type"]) || null,
  };
  const meta: GraphParamMeta = {
    material: "",
    ...metaCore,
    section: sectionOf(category, confidence),
    rollback: rollbackNote(supported, editable),
    frontendDowngrade,
  };

  if (!kindKnown) {
    const degraded: UnknownNode = {
      ...baseOf(spec, "unknown", {
        supported: false,
        editable: false,
        readonlyReason: "unsupported_kind",
        cost: "L1",
        meta,
      }),
      kind: "unknown",
      supported: false,
      editable: false,
      active: false,
      readonly_reason: "unsupported_kind",
      value_source: "unsupported",
      raw: source,
      note: `未知参数类型 ${declaredKind || "（空）"}（前端版本较旧），已降级为只读。`,
    };
    return { node: degraded, meta };
  }

  const note = `语义：${meta.semanticLabel || "未识别"}；识别等级：${
    CONFIDENCE_LABELS[confidence]
  }。${CONFIDENCE_HINTS[confidence]}`;
  const base = { ...baseOf(spec, declaredKind as NodeKind, {
    supported,
    editable,
    readonlyReason,
    cost: "L1",
    meta,
  }), note };

  switch (declaredKind) {
    case "ramp":
      return {
        node: rampNodeOf(spec, base, spec.elements ?? []),
        meta,
      };
    case "color":
    case "vector": {
      const common = {
        ...base,
        value: asNumberArray(value["current"]),
        baseline: asNumberArray(value["baseline"]),
        effective: asNumberArray(value["effective"]),
      };
      if (declaredKind === "color") {
        const node: ColorNode = { ...common, kind: "color" };
        return { node, meta };
      }
      const node: VectorNode = {
        ...common,
        kind: "vector",
        axes: ["x", "y", "z"],
        space: "world",
        locked_ratio: false,
      };
      return { node, meta };
    }
    case "enum": {
      const asText = (raw: unknown): string | null =>
        typeof raw === "string" ? raw : raw === null || raw === undefined ? null : String(raw);
      const current = asText(value["current"]);
      const node: EnumNode = {
        ...base,
        kind: "enum",
        value: current,
        baseline: asText(value["baseline"]) ?? current,
        effective: asText(value["effective"]) ?? current,
        // 材质图里的 enum 只有「节点组实例」这一种，它的值就是组名本身，
        // 因此选项只有一项 —— 不编一份组名单出来。
        options: current ? [{ value: current, label: current }] : [],
        depends_on: null,
        options_dynamic: false,
      };
      return { node, meta };
    }
    default: {
      const node: ScalarNode = {
        ...base,
        kind: declaredKind as "float" | "int" | "bool",
        value:
          typeof value["current"] === "number" || typeof value["current"] === "boolean"
            ? (value["current"] as number | boolean)
            : null,
        baseline:
          typeof value["baseline"] === "number" || typeof value["baseline"] === "boolean"
            ? (value["baseline"] as number | boolean)
            : null,
        effective:
          typeof value["effective"] === "number" || typeof value["effective"] === "boolean"
            ? (value["effective"] as number | boolean)
            : null,
        minimum: asFiniteNumber(source["minimum"]),
        maximum: asFiniteNumber(source["maximum"]),
        step: asFiniteNumber(source["step"]),
      };
      return { node, meta };
    }
  }
}

// -- 材质与整份 schema ------------------------

export interface GraphSection {
  spec: GraphSectionSpec;
  params: GraphParam[];
}

export interface GraphCounts {
  total: number;
  writable: number;
  shared: number;
  byConfidence: Record<GraphConfidence, number>;
}

export interface GraphMaterial {
  materialId: string;
  name: string;
  available: boolean;
  error: string | null;
  structureHash: string | null;
  degradedReason: string | null;
  trace: GraphTraceSummary | null;
  sharedGroups: Record<string, string[]>;
  params: GraphParam[];
  index: Map<string, GraphParam>;
  sections: GraphSection[];
  counts: GraphCounts;
}

export interface MaterialGraphSchema {
  schemaVersion: string;
  baselineId: string | null;
  structureHash: string | null;
  materials: GraphMaterial[];
  byName: Map<string, GraphMaterial>;
  /** 全部材质的参数索引（`paramId -> GraphParam`）。 */
  index: Map<string, GraphParam>;
}

function countsOf(params: GraphParam[]): GraphCounts {
  const byConfidence: Record<GraphConfidence, number> = {
    confirmed: 0,
    inferred: 0,
    user_confirmed: 0,
    experimental: 0,
    unsupported: 0,
  };
  let writable = 0;
  let shared = 0;
  for (const param of params) {
    byConfidence[param.meta.confidence] += 1;
    if (param.node.supported && param.node.editable) {
      writable += 1;
    }
    if (param.meta.shared) {
      shared += 1;
    }
  }
  return { total: params.length, writable, shared, byConfidence };
}

export function parseGraphMaterial(raw: unknown): GraphMaterial | null {
  const source = asRecord(raw);
  const name = asString(source["material"]);
  if (!name) {
    return null;
  }
  const params: GraphParam[] = [];
  for (const item of Array.isArray(source["parameters"]) ? (source["parameters"] as unknown[]) : []) {
    const parsed = parseGraphParam(item);
    if (parsed) {
      params.push(parsed);
    }
  }
  const index = new Map<string, GraphParam>();
  for (const param of params) {
    // 回填材质名：编辑器与动作层都需要由 `paramId` 反查「这是哪个材质的参数」，
    // 让每个调用点自己去遍历 schema 只会多出若干份互相漂移的实现。
    param.meta.material = name;
    index.set(param.node.id, param);
  }
  const sharedGroupsRaw = asRecord(source["shared_groups"]);
  const sharedGroups: Record<string, string[]> = {};
  for (const [key, list] of Object.entries(sharedGroupsRaw)) {
    sharedGroups[key] = asStringArray(list);
  }
  const available = asBool(source["available"]);
  const sections = GRAPH_SECTIONS.map((spec) => ({
    spec,
    params: params.filter((param) => param.meta.section === spec.id),
  }));
  return {
    materialId: asString(source["material_id"]) || name,
    name,
    available,
    error: asString(source["error"]) || null,
    structureHash: asString(source["structure_hash"]) || null,
    degradedReason: asString(source["degraded_reason"]) || null,
    trace: (source["trace"] ?? null) as GraphMaterial["trace"],
    sharedGroups,
    params,
    index,
    sections,
    counts: countsOf(params),
  };
}

/** 解析整份材质图 schema。形状对不上时返回空壳（界面如实说「不可用」）。 */
export function parseMaterialGraphSchema(raw: unknown): MaterialGraphSchema {
  const source = asRecord(raw) as Partial<MaterialGraphSchemaResponse>;
  const materials: GraphMaterial[] = [];
  const list = Array.isArray(source.materials) ? source.materials : [];
  for (const item of list) {
    const parsed = parseGraphMaterial(item);
    if (parsed) {
      materials.push(parsed);
    }
  }
  const byName = new Map<string, GraphMaterial>();
  const index = new Map<string, GraphParam>();
  for (const material of materials) {
    byName.set(material.name, material);
    for (const param of material.params) {
      index.set(param.node.id, param);
    }
  }
  return {
    schemaVersion: asString(source.schema_version),
    baselineId: asString(source.material_graph_baseline_id) || null,
    structureHash: asString(source.structure_hash) || null,
    materials,
    byName,
    index,
  };
}

/** `paramId -> SurfaceNode`：交给 `draft.ts` 的草稿工具使用。 */
export function graphNodeIndex(schema: MaterialGraphSchema | null): Map<string, SurfaceNode> {
  const out = new Map<string, SurfaceNode>();
  if (!schema) {
    return out;
  }
  for (const [id, param] of schema.index) {
    out.set(id, param.node);
  }
  return out;
}

/**
 * 把一份草稿按「属于材质图 / 属于参数面」拆开。
 *
 * `/api/v4/preview` 的 `draft` 会被逐 id 当参数面校验，材质图 id 混进去只会得到
 * `PARAM_INVALID`；`material_draft` 同理。因此这个拆分点是**唯一**的分界，
 * 别处不需要知道哪些 id 属于哪一边。
 */
export function splitGraphDraft(
  graphSchema: MaterialGraphSchema | null,
  draft: SurfaceDraft
): { surface: SurfaceDraft; graph: SurfaceDraft } {
  const graphIds = graphSchema?.index ?? new Map<string, GraphParam>();
  const surface: SurfaceDraft = {};
  const graph: SurfaceDraft = {};
  for (const [paramId, value] of Object.entries(draft)) {
    if (graphIds.has(paramId)) {
      graph[paramId] = value;
    } else {
      surface[paramId] = value;
    }
  }
  return { surface, graph };
}

// -- 展示助手 ------------------------

/** 后端 `label` 的格式是「节点显示名 · 插座名」；解析不到就回落到字段名。 */
export function socketLabelOf(param: GraphParam): string | null {
  const label = typeof param.node.label === "string" ? param.node.label : "";
  const parts = label.split(" · ");
  if (parts.length >= 2) {
    return parts.slice(1).join(" · ");
  }
  return param.meta.bindingField;
}

/** 受影响范围的标签：共享组说「材质」，普通参数说「对象」。 */
export function affectedScopeLabel(param: GraphParam): string {
  return param.meta.shared ? "会一起改到的材质" : "受影响对象（使用该材质）";
}

export function materialGraphIndexFor(
  schema: MaterialGraphSchema | null,
  materialName: string
): GraphMaterial | null {
  return schema?.byName.get(materialName) ?? null;
}

/**
 * 某个参数所属的共享节点组（不在共享组里则 `null`）。
 *
 * 返回的就是服务端用来做确认键的那个字符串：`execution.build_param_index` 把
 * `impact["in_group"]` 存成 `shared_group`，`validate_graph_draft` 拿它比对
 * `confirmed_shared_groups`。**必须逐字一致**，否则用户确认了也仍然会被拒绝。
 */
export function sharedGroupOf(param: GraphParam, material: GraphMaterial): string | null {
  if (!param.meta.shared) {
    return null;
  }
  if (param.meta.inGroup) {
    return param.meta.inGroup;
  }
  // 服务端说是共享却没给组名：只有当「多材质引用的组」唯一时才敢替它选一个，
  // 否则宁可返回 null（界面据此如实说「组名未知」，不编一个出来）。
  const candidates = Object.entries(material.sharedGroups).filter(([, users]) => users.length > 1);
  return candidates.length === 1 ? candidates[0][0] : null;
}

function affectedMaterialsOf(param: GraphParam, material: GraphMaterial, group: string): string[] {
  // 服务端的 `affected_materials` 对共享参数给的就是共享组的使用材质清单
  // （`execution.validate_graph_draft` 报错时用的也是它），以它为准。
  if (param.meta.affectedMaterials.length > 0) {
    return [...param.meta.affectedMaterials];
  }
  const fromSchema = material.sharedGroups[group];
  if (fromSchema && fromSchema.length > 0) {
    return [...fromSchema];
  }
  return [material.name];
}

/**
 * 草稿里**已经写过**但尚未确认影响范围的共享节点组。
 *
 * 界面据此在提交前就给出确认入口，而不是等提交被服务端拒了才知道。
 */
export function unconfirmedSharedGroups(
  material: GraphMaterial,
  draft: SurfaceDraft,
  confirmed: readonly string[]
): SharedConfirmRequest[] {
  const done = new Set(confirmed);
  const out: SharedConfirmRequest[] = [];
  const seen = new Set<string>();
  for (const param of material.params) {
    if (!(param.node.id in draft)) {
      continue;
    }
    const group = sharedGroupOf(param, material);
    if (!group || done.has(group) || seen.has(group)) {
      continue;
    }
    seen.add(group);
    out.push({
      parameter: param.node.id,
      sharedGroup: group,
      affectedMaterials: affectedMaterialsOf(param, material, group),
    });
  }
  return out;
}

/**
 * 整份 schema 范围内、草稿里已经写过但**尚未确认**的共享节点组。
 *
 * 提交前用它把影响范围摊开（而不是等服务端拒一次再说）。判据与服务端
 * `execution.validate_graph_draft` 逐字一致：`shared` 为真且组名不在确认名单里。
 */
export function unconfirmedSharedGroupsInSchema(
  schema: MaterialGraphSchema | null,
  draft: SurfaceDraft,
  confirmed: readonly string[]
): SharedConfirmRequest[] {
  if (!schema) {
    return [];
  }
  const done = new Set(confirmed);
  const out: SharedConfirmRequest[] = [];
  const seen = new Set<string>();
  for (const material of schema.materials) {
    for (const item of unconfirmedSharedGroups(material, draft, confirmed)) {
      if (done.has(item.sharedGroup) || seen.has(item.sharedGroup)) {
        continue;
      }
      seen.add(item.sharedGroup);
      out.push(item);
    }
  }
  return out;
}

/** 从服务端 `SHARED_PARAMETER_CONFIRM_REQUIRED` 的 details 里取出确认信息。 */export function confirmRequestFromDetails(details: unknown): SharedConfirmRequest | null {
  const raw = asRecord(details);
  const group = asString(raw["shared_group"]);
  if (!group) {
    return null;
  }
  return {
    parameter: asString(raw["parameter"]),
    sharedGroup: group,
    affectedMaterials: asStringArray(raw["affected_materials"]),
  };
}

export interface GraphFilter {
  query: string;
  /** `null` = 全部等级。 */
  confidence: GraphConfidence | null;
  onlyModified: boolean;
  onlyShared: boolean;
}

export function emptyGraphFilter(): GraphFilter {
  return { query: "", confidence: null, onlyModified: false, onlyShared: false };
}

/**
 * 一个材质的界面视图状态（展开的分区、搜索词、筛选）。
 *
 * 按材质分别保存：切换材质回来时展开与筛选**保持原样**。这不是锦上添花 ——
 * 材质图参数可能上百项，一次切换就把用户折叠好的结构摊平，等于让他重做一遍。
 * 滚动位置属于「瞬时」状态，放在组件的 ref 里（见 `MaterialGraphEditor`）。
 */
export interface GraphViewState extends GraphFilter {
  openSections: GraphSectionId[];
}

export function emptyGraphView(): GraphViewState {
  return { ...emptyGraphFilter(), openSections: [] };
}

/**
 * 首次打开某个材质时的默认视图：**只展开「常用参数」**。
 *
 * 一上来就把七个分区全部摊开，等于把「页面过长」这个问题原样搬回界面里。
 * 用户自己的展开状态一旦存在（`graph.view[material]`），就以它为准。
 */
export function defaultGraphView(): GraphViewState {
  return { ...emptyGraphFilter(), openSections: ["common"] };
}

/**
 * 参数筛选。
 *
 * 搜索命中范围刻意包含**来源节点名 / 语义 / 证据**：用户找参数时想得到的是
 * 「那个 Base Color」或「哪个节点」，而不是 id。
 */
export function matchesGraphFilter(
  param: GraphParam,
  filter: GraphFilter,
  dirtyIds: ReadonlySet<string>
): boolean {
  if (filter.confidence && param.meta.confidence !== filter.confidence) {
    return false;
  }
  if (filter.onlyModified && !dirtyIds.has(param.node.id)) {
    return false;
  }
  if (filter.onlyShared && !param.meta.shared) {
    return false;
  }
  const needle = filter.query.trim().toLowerCase();
  if (!needle) {
    return true;
  }
  const haystack = [
    param.node.label,
    param.node.id,
    param.meta.semanticLabel,
    param.meta.sourceLabel,
    param.node.note,
    ...param.meta.evidence,
    ...param.meta.affectedMaterials,
    ...param.meta.influencePaths.map((path) => path.join(" → ")),
  ]
    .filter((item): item is string => typeof item === "string" && item.length > 0)
    .map((item) => item.toLowerCase());
  return haystack.some((item) => item.includes(needle));
}

/** 该材质下真正可写的参数（供「N 项可调」与回归断言使用）。 */
export function writableGraphParams(material: GraphMaterial): GraphParam[] {
  return material.params.filter((param) => param.node.supported && param.node.editable);
}
