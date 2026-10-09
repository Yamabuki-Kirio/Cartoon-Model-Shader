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

/** 未确认能力的标量（e.g. Emission 强度）：supported 但不可写。 */
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
          unconfirmedScalar("cel.Cel_Skin.emission_strength", 1.5),
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
