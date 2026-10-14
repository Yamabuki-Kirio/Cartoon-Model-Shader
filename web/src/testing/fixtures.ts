/**
 * 测试夹具：与后端 `GET /api/v4/surface/schema` 的真实输出形状保持一致。
 *
 * 刻意保留后端那些「看起来多余」的字段（`value_source`、`structural`、
 * `element_count.cost`…）：解析层如果少读一个字段，测试应该跟着红，
 * 而不是因为夹具恰好没写而悄悄通过。
 */

export function rampElement(index: number, position: number, color: number[]) {
  return {
    index,
    position: { value: position, baseline: position, minimum: 0, maximum: 1, step: 0.001 },
    color: { value: color, baseline: color },
  };
}

export interface RampFixtureOptions {
  group?: string;
  elements?: Array<[number, number[]]>;
  interpolation?: string;
  editable?: boolean;
  supported?: boolean;
  label?: string;
  readonlyReason?: string | null;
}

export function rampNode(options: RampFixtureOptions = {}) {
  const group = options.group ?? "Cel_Skin";
  const pairs = options.elements ?? [
    [0.0, [0.05, 0.05, 0.08, 1.0]],
    [0.5, [0.5, 0.48, 0.46, 1.0]],
    [1.0, [0.95, 0.94, 0.92, 1.0]],
  ];
  const interpolation = options.interpolation ?? "LINEAR";
  const editable = options.editable ?? true;
  const supported = options.supported ?? true;
  return {
    id: `cel.${group}.ramp`,
    kind: "ramp",
    group: "cel",
    cost: "L1",
    label: options.label ?? `${group} 色带`,
    supported,
    editable,
    active: supported,
    readonly_reason: options.readonlyReason ?? (editable ? null : "reference_only"),
    value_source: supported ? "scene" : "unsupported",
    structural: false,
    binding: { object_type: "COLOR_RAMP", object_id: `${group}/ColorRamp`, field: "elements" },
    elements: pairs.map(([position, color], index) => rampElement(index, position, color)),
    interpolation: {
      value: interpolation,
      baseline: interpolation,
      options: [
        { value: "LINEAR", label: "线性" },
        { value: "CONSTANT", label: "常量" },
        { value: "EASE", label: "缓动" },
      ],
    },
    element_count: {
      value: pairs.length,
      baseline: pairs.length,
      structural: true,
      cost: "L3",
      supported: true,
      editable: false,
      readonly_reason: "rollback_unavailable",
      minimum: 2,
      maximum: 32,
    },
    checks: [],
  };
}

export function groupNode(
  name: string,
  options: { children?: unknown[]; editable?: boolean; supported?: boolean; label?: string } = {}
) {
  const editable = options.editable ?? true;
  const supported = options.supported ?? true;
  return {
    id: `cel.${name}`,
    kind: "group",
    group: "cel",
    cost: "L1",
    label: options.label ?? name,
    supported,
    editable,
    active: supported,
    readonly_reason: supported ? (editable ? null : "reference_only") : "not_found",
    value_source: supported ? "scene" : "unsupported",
    structural: false,
    children: options.children ?? [rampNode({ group: name, editable, supported })],
  };
}

// -- 逐材质分组 ------------------------------

/** 材质分组里的只读展示项：分类 / 目标 Cel 组 / 使用对象数。 */
function materialReadonlyChild(
  id: string,
  kind: "enum" | "int",
  value: string | number | null,
  options: { label?: string; optionsList?: Array<{ value: string; label: string }> } = {}
) {
  return {
    id,
    kind,
    group: "material",
    cost: "L1",
    label: options.label ?? id,
    supported: true,
    editable: false,
    active: true,
    readonly_reason: "structural",
    value_source: "scene",
    structural: false,
    value,
    baseline: value,
    effective: value,
    ...(kind === "enum"
      ? { options: options.optionsList ?? [], depends_on: null }
      : { minimum: 0 }),
  };
}

/** 材质分组里的一个可写参数（颜色或标量）。 */
export function materialParamNode(
  material: string,
  field: string,
  kind: "color" | "float",
  value: unknown,
  options: {
    editable?: boolean;
    supported?: boolean;
    readonlyReason?: string | null;
    reason?: string;
    note?: string;
    socket?: string;
    shader?: string;
    minimum?: number;
    maximum?: number;
    step?: number;
  } = {}
) {
  const editable = options.editable ?? true;
  const supported = options.supported ?? true;
  const shader = options.shader ?? "Principled BSDF";
  const socket = options.socket ?? field;
  return {
    id: `material.${material}.${field}`,
    kind,
    group: "material",
    cost: "L1",
    label: field,
    supported,
    editable,
    active: supported,
    readonly_reason: options.readonlyReason ?? (editable ? null : "structural"),
    value_source: supported ? "scene" : "unsupported",
    structural: false,
    ...(editable && supported
      ? {
          binding: {
            object_type: "MATERIAL",
            object_id: `${material}/${shader}/${socket}`,
            field,
          },
        }
      : {}),
    value,
    baseline: value,
    effective: value,
    ...(kind === "float"
      ? {
          minimum: options.minimum ?? 0,
          maximum: options.maximum ?? 1,
          step: options.step ?? 0.01,
        }
      : {}),
    ...(options.reason ? { reason: options.reason } : {}),
    ...(options.note ? { note: options.note } : {}),
  };
}

export interface MaterialFixtureOptions {
  material?: string;
  classification?: string | null;
  classificationLabel?: string | null;
  targetGroup?: string | null;
  users?: string[];
  nodeCount?: number;
  blendMethod?: string;
  editable?: boolean;
  supported?: boolean;
  label?: string;
  /** 覆盖某几个参数（`field -> options`）；未覆盖的用默认可写值。 */
  params?: Record<string, Record<string, unknown>>;
  /** 只生成这些字段（默认 6 个全生成，与后端「固定集合」一致）。 */
  only?: string[];
}

/**
 * 逐材质分组夹具。形状与后端 `material.build_material_group` 的 `to_public()` 对齐：
 * 三个只读展示节点 + 固定 6 个安全参数（探不到的以只读占位出现）。
 */
export function materialNode(options: MaterialFixtureOptions = {}) {
  const material = options.material ?? "皮肤_脸";
  const editable = options.editable ?? true;
  const supported = options.supported ?? true;
  const classification = options.classification === undefined ? "skin" : options.classification;
  const targetGroup = options.targetGroup === undefined ? "Cel_Skin" : options.targetGroup;
  const users = options.users ?? ["model_mesh"];

  const defaults: Record<string, { kind: "color" | "float"; value: unknown }> = {
    base_color: { kind: "color", value: [0.8, 0.8, 0.8, 1.0] },
    metallic: { kind: "float", value: 0.0 },
    roughness: { kind: "float", value: 0.5 },
    emission_color: { kind: "color", value: [0.0, 0.0, 0.0, 1.0] },
    emission_strength: { kind: "float", value: 0.0 },
    specular: { kind: "float", value: 0.5 },
  };
  const fields = options.only ?? Object.keys(defaults);

  const children: unknown[] = [
    materialReadonlyChild(
      `material.${material}.classification`,
      "enum",
      // 判不出分类时后端给的是 `null`（不是空串）—— 夹具必须同形，
      // 否则「空串 vs null」这类差异会被夹具悄悄抹平。
      classification,
      {
        label: "分类",
        optionsList: classification
          ? [{ value: classification, label: options.classificationLabel ?? classification }]
          : [],
      }
    ),
    materialReadonlyChild(
      `material.${material}.target_group`,
      "enum",
      targetGroup,
      {
        label: "目标 Cel 组",
        optionsList: targetGroup ? [{ value: targetGroup, label: targetGroup }] : [],
      }
    ),
    materialReadonlyChild(`material.${material}.users.count`, "int", users.length, {
      label: "使用对象数",
    }),
    ...fields.map((field) => {
      const spec = defaults[field];
      if (!spec) {
        throw new Error(`未知的材质参数夹具字段：${field}`);
      }
      const override = options.params?.[field] ?? {};
      // `value` 在覆盖里单独取：它替换的是夹具的默认值，而不是一个「选项」。
      const value = "value" in override ? override["value"] : spec.value;
      return materialParamNode(material, field, spec.kind, value, override);
    }),
  ];

  return {
    id: `material.${material}`,
    kind: "group",
    group: "material",
    cost: "L1",
    label: options.label ?? material,
    supported,
    editable,
    active: supported,
    readonly_reason: supported ? (editable ? null : "structural") : "not_found",
    value_source: supported ? "scene" : "unsupported",
    structural: false,
    impact: {
      users,
      slot_count: users.length,
      classification,
      target_group: targetGroup,
      node_count: options.nodeCount ?? 2,
      blend_method: options.blendMethod ?? "OPAQUE",
    },
    children,
  };
}

/** 只读展示项（不带 binding）。 */
export function readonlyEnum(id: string, value: string, reason = "structural") {
  return {
    id,
    kind: "enum",
    group: "cel",
    cost: "L1",
    label: id,
    supported: true,
    editable: false,
    active: true,
    readonly_reason: reason,
    value_source: "scene",
    structural: false,
    value,
    baseline: value,
    effective: value,
    options: [],
    depends_on: null,
  };
}

/** 未确认能力的标量（通用只读夹具）：探到了，但真机插座名尚未确认。 */
export function unconfirmedScalar(id: string, value: number) {
  return {
    id,
    kind: "float",
    group: "cel",
    cost: "L1",
    label: id,
    supported: true,
    editable: false,
    active: true,
    readonly_reason: "unconfirmed_capability",
    value_source: "scene",
    structural: false,
    value,
    baseline: value,
    effective: value,
    minimum: 0,
    maximum: 100,
    step: 0.05,
  };
}

/**
 * Cel 组的 Emission 强度（真机已确认，可写）。
 *
 * 与 `unconfirmedScalar` 的差别只有 `editable` 与 `binding`：真机拓扑确认后
 * 后端给的是 `NODE_SOCKET.default_value`（object_id = 「组名/节点名/插座名」，
 * 三段都来自探测结果），前端据此渲染数字输入框。
 *
 * 需要「降级只读」的夹具时，直接在这个对象上覆盖 `editable: false` +
 * `readonly_reason` + `binding: undefined`（模拟插座被上游连线或参考组）。
 */
export function emissionScalar(id: string, value: number, nodeName = "Emission") {
  const groupName = id.split(".")[1] ?? "Cel_Skin";
  return {
    id,
    kind: "float",
    group: "cel",
    cost: "L1",
    label: id,
    supported: true,
    editable: true,
    active: true,
    readonly_reason: null,
    value_source: "scene",
    structural: false,
    binding: {
      object_type: "NODE_SOCKET",
      object_id: `${groupName}/${nodeName}/Strength`,
      field: "default_value",
    },
    value,
    baseline: value,
    effective: value,
    minimum: 0,
    maximum: 100,
    step: 0.05,
  };
}

export function impactCount(id: string, count: number) {
  return {
    ...readonlyEnum(id, String(count)),
    kind: "int",
    structural: true,
    value: count,
    baseline: count,
    effective: count,
  };
}

export interface SchemaFixtureOptions {
  groups?: unknown[];
  structureHash?: string;
}

/** 默认 schema：Cel_Skin（3 档）+ Cel_Hair（2 档）+ Sakura_Hair_Reference（只读）。 */
export function schemaPayload(options: SchemaFixtureOptions = {}) {
  const groups =
    options.groups ??
    [
      groupNode("Cel_Skin", {
        children: [
          rampNode({ group: "Cel_Skin" }),
          emissionScalar("cel.Cel_Skin.emission_strength", 1.5),
          impactCount("cel.Cel_Skin.impact.material_count", 7),
          readonlyEnum("cel.Cel_Skin.managed_mode", "editable"),
        ],
      }),
      groupNode("Cel_Hair", {
        children: [
          // 刻意与 Cel_Skin 同为 3 档：这样「复制到兼容组」的成功路径有真实夹具可用；
          // 数量不同的拒绝路径由 ramp.test.ts 用专门的夹具覆盖。
          rampNode({ group: "Cel_Hair", interpolation: "CONSTANT" }),
        ],
      }),
      groupNode("Sakura_Hair_Reference", {
        editable: false,
        children: [
          {
            ...rampNode({
              group: "Sakura_Hair_Reference",
              editable: false,
              readonlyReason: "reference_only",
            }),
          },
        ],
      }),
    ];
  return {
    ok: true,
    schema_version: "toon-surface/2",
    groups,
    highest_cost: "L1",
    surface_baseline_id: "abc123abc123",
    structure_hash: options.structureHash ?? "f".repeat(64),
    compositor_group: "AI_Compositor",
    degraded: ["Cel_Cloth", "Cel_Dark", "Cel_Eyes", "RayToon_Face_Soft", "RayToon_Eyes_Unlit"],
  };
}

export function baselinePayload(options: { structureHash?: string; baselineId?: string } = {}) {
  return {
    ok: true,
    available: true,
    schema_version: "toon-surface/2",
    baseline_id: options.baselineId ?? "bl0000000001",
    captured_at: "2026-10-09T18:00:00+08:00",
    blender: "5.2.1 LTS",
    structure_hash: options.structureHash ?? "f".repeat(64),
    compositor_group: "AI_Compositor",
    found_groups: 3,
    declared_groups: 8,
    degraded: ["Cel_Cloth"],
    identities: [{ object_type: "NODE_GROUP", name: "Cel_Skin", source: "AI_Compositor" }],
    values: {},
  };
}

export function jobPayload(overrides: Record<string, unknown> = {}) {
  // `job_id` 与 `preview_url` 必须自洽：真实服务端的预览 URL 就是
  // `/api/preview/<job_id>`。夹具里各自写死会让「按 job_id 取图」的实现测不出来。
  const jobId = typeof overrides.job_id === "string" ? overrides.job_id : "job0000000000001";
  return {
    ok: true,
    job_id: jobId,
    seq: 7,
    status: "done",
    created_at: "2026-10-09T18:00:01+08:00",
    updated_at: "2026-10-09T18:00:09+08:00",
    superseded: false,
    steps: ["检查身份与结构", "恢复基线", "应用完整草稿", "回读校验", "渲染预览", "恢复基线并校验"],
    kind: "surface",
    external_changes: [],
    result: {
      preview_url: `/api/preview/${jobId}`,
      render_resolution: [540, 990, 100],
      applied_surface: {
        "cel.Cel_Skin.ramp": [
          { position: 0.0, color: [0, 0, 0, 1] },
          { position: 0.5, color: [0.5, 0.5, 0.5, 1] },
          { position: 1.0, color: [1, 1, 1, 1] },
        ],
        "cel.Cel_Skin.ramp.interpolation": "CONSTANT",
      },
      surface_restore_verified: true,
      restore_verified: true,
      structure_hash: "f".repeat(64),
      framing: { mode: "current_camera", mode_label: "当前相机预览" },
    },
    error: null,
    ...overrides,
  };
}

// -- 预设与保存 ------------------------

/** 预设摘要。`surface_count` 为 0 表示这是一份只含 L0 的旧预设。 */
export function presetSummary(overrides: Record<string, unknown> = {}) {
  return {
    preset_id: "p0000000001",
    name: "柔和肤色 v2",
    schema: "toon-tuner-preset/1",
    created_at: "2026-10-01T10:00:00+08:00",
    updated_at: "2026-10-09T18:00:00+08:00",
    pipeline_mode: "faithful",
    framing_mode: "current_camera",
    framing_margin: 0.15,
    preview_quality: "standard",
    parameter_count: 0,
    active_count: 0,
    surface_count: 2,
    ...overrides,
  };
}

export function presetDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...presetSummary(),
    parameters: {},
    surface: {
      "cel.Cel_Skin.ramp.interpolation": "CONSTANT",
      "cel.Cel_Skin.managed_mode": "simple",
    },
    ...overrides,
  };
}

export function presetListPayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true as const,
    protocol: "toon-tuner-presets/1",
    storage: "%LOCALAPPDATA%\\CartoonModelShader\\presets",
    presets: [presetSummary()],
    skipped: [],
    quality_tiers: { standard: { label: "标准", long_side: 990, samples: 8 } },
    pipeline_modes: ["faithful", "enhanced"],
    framing_modes: ["current_camera", "auto_headshot"],
    defaults: {
      pipeline_mode: "faithful",
      framing_mode: "current_camera",
      framing_margin: 0.15,
      preview_quality: "standard",
    },
    ...overrides,
  };
}

/** prepare 成功的响应：`save_as`、目标不存在 → 不需要备份。 */
export function commitPreparedPayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true as const,
    token: "tok-0000000001",
    mode: "save_as",
    target_path: "D:\\项目\\角色-调参.blend",
    backup_path: null as string | null,
    warnings: [],
    baseline_id: "bl0000000001",
    draft_hash: "a".repeat(64),
    parameter_count: 24,
    surface_parameter_count: 2,
    structure_hash: "f".repeat(64),
    normalized: [],
    expires_at: "2026-10-09T18:05:00+08:00",
    expires_in_seconds: 300,
    confirmation_required: false,
    confirmation_id: "cf0000000001",
    ...overrides,
  };
}

/** 完整的四态状态块（成功保存）。 */
export function commitStatusPayload(overrides: Record<string, unknown> = {}) {
  return {
    applied: true,
    readback_verified: true,
    mismatches: [],
    backup: { required: false, created: false, path: null, path_confirmed: null },
    rollback: {
      attempted: false,
      verified: false,
      mismatches: [],
      code: null,
      error: null,
      reason: "保存成功，未触发回滚。",
    },
    saved: true,
    target_path: "D:\\项目\\角色-调参.blend",
    mode: "save_as",
    draft_preserved: true,
    applied_surface: { "cel.Cel_Skin.managed_mode": "simple" },
    surface_readback_verified: true,
    surface_mismatches: [],
    save: { path_after: "D:\\项目\\角色-调参.blend", path_matches: true, file_exists: true, is_dirty_after: false },
    ...overrides,
  };
}

export function commitResponsePayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true as const,
    mode: "save_as",
    target_path: "D:\\项目\\角色-调参.blend",
    backup_path: null as string | null,
    saved: true,
    status: commitStatusPayload(),
    steps: ["复核确认时的前提", "应用完整草稿", "回读校验", "复核 v4 结构", "应用 Cel 草稿", "无需备份", "保存工程", "刷新基线"],
    normalized: [],
    baseline_id: "bl0000000002",
    baseline_refreshed: true,
    saved_at: "2026-10-09T18:01:00+08:00",
    warnings: [],
    ...overrides,
  };
}

// -- 材质图（PR #7）-----------------------------

/**
 * 单个材质图参数夹具。
 *
 * 形状与后端 `material_graph.schema.to_spec` 对齐，**包括那些看起来多余的字段**
 * （`evidence`、`used_outputs`、`influence_paths`…）：解析层少读一个字段，
 * 测试应该跟着红，而不是因为夹具恰好没写而悄悄通过。
 */
export function graphParamFixture(
  id: string,
  overrides: Record<string, unknown> = {}
) {
  return {
    id,
    label: id,
    kind: "float",
    confidence: "confirmed",
    semantic: "value",
    semantic_label: "数值",
    evidence: ["节点类型 VALUE 语义确定"],
    influence_paths: [["Value", "Principled BSDF", "Material Output"]],
    affected_materials: ["model_mesh"],
    shared: false,
    editable: true,
    supported: true,
    category: "numeric",
    readonly_reason: null,
    source_node: "Value",
    source_label: "Value",
    in_group: null,
    used_outputs: ["Value"],
    value: { current: 1, baseline: 1, effective: 1 },
    minimum: null,
    maximum: null,
    step: null,
    binding: { object_type: "MATERIAL_GRAPH_SOCKET", field: "default_value" },
    ...overrides,
  };
}

/** 着色器常量（`SEM_SHADER` → 分区 `common`）。 */
export function graphShaderColorFixture(id: string, value: number[]) {
  return graphParamFixture(id, {
    label: "Principled BSDF · Base Color",
    kind: "color",
    semantic: "shader",
    semantic_label: "着色器",
    category: "common",
    source_node: "Principled BSDF",
    source_label: "Principled BSDF",
    used_outputs: ["BSDF"],
    value: { current: value, baseline: value, effective: value },
    binding: { object_type: "MATERIAL_GRAPH_SOCKET", field: "default_value" },
  });
}

/** ColorRamp：单一复合节点，色标取值可编辑、数量只读。 */
export function graphRampFixture(
  id: string,
  options: { elements?: Array<[number, number[]]>; interpolation?: string } = {}
) {
  const pairs = options.elements ?? [
    [0.0, [0.1, 0.1, 0.12, 1.0]],
    [0.5, [0.55, 0.5, 0.48, 1.0]],
    [1.0, [0.95, 0.94, 0.92, 1.0]],
  ];
  const interpolation = options.interpolation ?? "LINEAR";
  return graphParamFixture(id, {
    label: "ColorRamp 色带",
    kind: "ramp",
    semantic: "ramp",
    semantic_label: "色带",
    evidence: ["节点挂载 color_ramp 结构"],
    category: "ramp",
    source_node: "ColorRamp",
    source_label: "ColorRamp",
    used_outputs: ["Color"],
    value: { current: null, baseline: null, effective: null },
    elements: pairs.map(([position, color], index) => ({
      index,
      position: { value: position, baseline: position, minimum: 0, maximum: 1, step: 0.001 },
      color: { value: color, baseline: color },
    })),
    // `interpolation` 与 `element_count` 是 `surface.schema` 的**复合形状**，
    // 不是裸标量 —— 夹具必须同形，否则「解析层少读一层」这类缺陷会被夹具抹平。
    interpolation: { value: interpolation, baseline: interpolation, options: [] },
    element_count: {
      value: pairs.length,
      baseline: pairs.length,
      structural: true,
      cost: "L3",
      supported: true,
      editable: false,
      readonly_reason: "rollback_unavailable",
      minimum: 2,
      maximum: 32,
    },
    binding: { object_type: "MATERIAL_GRAPH_RAMP", field: "elements" },
  });
}

/** 推测参数（`inferred` → 独立分区，且服务端一律标成不可写）。 */
export function graphInferredFixture(id: string) {
  return graphParamFixture(id, {
    label: "Mix · Fac",
    confidence: "inferred",
    semantic: "mix",
    semantic_label: "颜色混合",
    evidence: ["节点类型 MIX 的语义随模式/规则变化，需真机确认"],
    category: "inferred",
    editable: false,
    readonly_reason: "inferred_not_confirmed",
    source_node: "Mix",
    source_label: "Mix",
    used_outputs: ["Result"],
    influence_paths: [["Mix", "Principled BSDF", "Material Output"]],
  });
}

/** 实验性语义（着色器混合）。 */
export function graphExperimentalFixture(id: string) {
  return graphParamFixture(id, {
    label: "Mix Shader 节点组",
    confidence: "experimental",
    semantic: "mix_shader",
    semantic_label: "着色器混合",
    evidence: ["着色器混合，改动需微扰验证"],
    category: "advanced",
    editable: false,
    readonly_reason: "experimental_semantics",
    source_node: "Mix Shader",
    source_label: "Mix Shader",
  });
}

/** 不支持的节点（探到了类型但无法安全执行 / 回滚）。 */
export function graphUnsupportedFixture(id: string) {
  return graphParamFixture(id, {
    label: "MysteryNode（无可编辑参数）",
    confidence: "unsupported",
    semantic: "other",
    semantic_label: "其它",
    evidence: ["未识别的节点类型 MYSTERY"],
    category: "advanced",
    editable: false,
    supported: false,
    readonly_reason: "unconfirmed_capability",
    source_node: "MysteryNode",
    source_label: "MysteryNode",
    binding: undefined,
  });
}

/**
 * **共享**节点组内的可写参数。
 *
 * 这是唯一能触发服务端 `SHARED_PARAMETER_CONFIRM_REQUIRED` 的形态：
 * 参数本身可写（组内 `confirmed` 节点），但它所在的组被多个材质引用。
 */
export function graphSharedFixture(id: string, options: { group?: string; users?: string[] } = {}) {
  const group = options.group ?? "Cel_Shared";
  const users = options.users ?? ["皮肤_脸", "头发_前"];
  return graphParamFixture(id, {
    label: "SharedValue · Value",
    confidence: "confirmed",
    semantic: "value",
    semantic_label: "数值",
    category: "numeric",
    shared: true,
    in_group: group,
    source_node: "SharedValue",
    source_label: "SharedValue",
    affected_materials: users,
    value: { current: 0.5, baseline: 0.5, effective: 0.5 },
  });
}

export interface GraphMaterialFixtureOptions {
  material?: string;
  materialId?: string;
  available?: boolean;
  structureHash?: string;
  degradedReason?: string | null;
  error?: string | null;
  parameters?: unknown[];
  sharedGroups?: Record<string, string[]>;
}

/** 单个材质的材质图 schema 夹具。 */
export function graphMaterialFixture(options: GraphMaterialFixtureOptions = {}) {
  const material = options.material ?? "皮肤_脸";
  const available = options.available ?? true;
  const parameters = options.parameters ?? defaultGraphParameters();
  const categories: Record<string, string[]> = {};
  for (const raw of parameters) {
    const param = raw as { id?: string; category?: string };
    const category = param.category ?? "advanced";
    categories[category] = [...(categories[category] ?? []), param.id ?? ""];
  }
  return {
    schema_version: "toon-material-graph/1",
    material_id: options.materialId ?? `mat-${material}`,
    material,
    available,
    ...(available
      ? {
          structure_hash: options.structureHash ?? "a".repeat(64),
          degraded_reason: options.degradedReason ?? null,
          trace: {
            entry: "Material Output",
            entry_output: "Surface",
            max_depth: 8,
            cycle: [],
            nodes: [
              {
                name: "Principled BSDF",
                type: "BSDF_PRINCIPLED",
                label: "Principled BSDF",
                depth: 1,
                level: "confirmed",
                confidence: "confirmed",
                semantic: "shader",
                influence_paths: [["Principled BSDF", "Material Output"]],
              },
            ],
          },
        }
      : { error: options.error ?? "该材质没有可用的节点图（未启用节点，或探测未返回结构）。" }),
    shared_groups: options.sharedGroups ?? {},
    groups: [],
    parameters: available ? parameters : [],
    categories: available ? categories : {},
    highest_cost: "L1",
  };
}

/** 覆盖全部七个分区的默认参数集合。 */
export function defaultGraphParameters(): unknown[] {
  return [
    graphShaderColorFixture("mg-common01", [0.86, 0.72, 0.66, 1]),
    graphParamFixture("mg-common02", {
      label: "Principled BSDF · Roughness",
      semantic: "shader",
      semantic_label: "着色器",
      category: "common",
      source_node: "Principled BSDF",
      source_label: "Principled BSDF",
      value: { current: 0.5, baseline: 0.5, effective: 0.5 },
      minimum: 0,
      maximum: 1,
      step: 0.01,
    }),
    graphParamFixture("mg-color01", {
      label: "RGB · Color",
      kind: "color",
      semantic: "color",
      semantic_label: "颜色",
      category: "color",
      source_node: "RGB",
      source_label: "RGB",
      value: { current: [0.2, 0.4, 0.9, 1], baseline: [0.2, 0.4, 0.9, 1], effective: [0.2, 0.4, 0.9, 1] },
    }),
    graphRampFixture("mg-ramp01"),
    graphParamFixture("mg-numeric01", {
      label: "Math · Value",
      semantic: "math",
      semantic_label: "数值运算",
      category: "numeric",
      source_node: "Math",
      source_label: "Math",
      value: { current: 2, baseline: 2, effective: 2 },
      minimum: -10,
      maximum: 10,
      step: 0.1,
      binding: { object_type: "MATERIAL_GRAPH_SOCKET", field: "default_value" },
    }),
    graphParamFixture("mg-group01", {
      label: "Cel_Shared 节点组",
      kind: "enum",
      confidence: "experimental",
      semantic: "group",
      semantic_label: "节点组",
      category: "node_group",
      editable: false,
      readonly_reason: "shared_group_requires_confirmation",
      source_node: "Cel_Shared",
      source_label: "Cel_Shared",
      value: { current: "Cel_Shared", baseline: "Cel_Shared", effective: "Cel_Shared" },
      binding: undefined,
    }),
    graphParamFixture("mg-texture01", {
      label: "Image Texture（无可编辑参数）",
      confidence: "confirmed",
      semantic: "texture",
      semantic_label: "纹理控制",
      category: "texture",
      editable: false,
      supported: false,
      readonly_reason: "no_editable_socket",
      source_node: "Image Texture",
      source_label: "Image Texture",
      binding: undefined,
    }),
    graphInferredFixture("mg-inferred01"),
    graphExperimentalFixture("mg-advanced01"),
    graphUnsupportedFixture("mg-advanced02"),
  ];
}

/** 整份材质图 schema（`GET /api/material-graph/schema`）。 */
export function graphSchemaPayload(
  options: { materials?: unknown[]; structureHash?: string; baselineId?: string } = {}
) {
  return {
    ok: true as const,
    available: true,
    schema_version: "toon-material-graph/1",
    material_graph_baseline_id: options.baselineId ?? "mg0000000001",
    structure_hash: options.structureHash ?? "b".repeat(64),
    materials: options.materials ?? [graphMaterialFixture()],
  };
}
