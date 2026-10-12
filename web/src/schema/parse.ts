/**
 * 递归 schema 的解析与降级。
 *
 * 两条硬规则：
 *
 * 1. **未知 kind 一律降级为只读**（`kind: "unknown"` + `readonly_reason: "unsupported_kind"`）。
 *    前端比后端旧时，新参数宁可显示成只读，也不能看起来可写。
 * 2. **字段缺失按「不可用」处理**：`supported` / `editable` / `active` 缺省一律 false，
 *    绝不因为字段没来就默认成 true。
 */

import type {
  CostLayer,
  EnumNode,
  GroupNode,
  NodeBase,
  NodeKind,
  RampElementCount,
  RampNode,
  ReadonlyReason,
  ScalarNode,
  SurfaceNode,
  SurfaceSchema,
  UnknownNode,
  ValueSource,
} from "./types";

export const KNOWN_KINDS: readonly NodeKind[] = [
  "float",
  "int",
  "bool",
  "enum",
  "color",
  "vector",
  "ramp",
  "group",
];

const COSTS: readonly CostLayer[] = ["L0", "L1", "L2", "L3"];

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBool(value: unknown): boolean {
  return value === true;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asNullableNumber(value: unknown): number | null {
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

function asCost(value: unknown): CostLayer {
  return COSTS.includes(value as CostLayer) ? (value as CostLayer) : "L0";
}

function baseOf(raw: Record<string, unknown>, kind: NodeKind): NodeBase {
  const bindingRaw = raw["binding"];
  const binding =
    bindingRaw !== null && typeof bindingRaw === "object"
      ? {
          object_type: asString(asRecord(bindingRaw)["object_type"]),
          object_id: asString(asRecord(bindingRaw)["object_id"]),
          field: asString(asRecord(bindingRaw)["field"]),
        }
      : undefined;

  return {
    id: asString(raw["id"]),
    kind,
    group: asString(raw["group"]),
    cost: asCost(raw["cost"]),
    label: asString(raw["label"]) || asString(raw["id"]),
    supported: asBool(raw["supported"]),
    editable: asBool(raw["editable"]),
    active: asBool(raw["active"]),
    readonly_reason: (raw["readonly_reason"] as ReadonlyReason | null) ?? null,
    value_source: (raw["value_source"] as ValueSource) ?? "unsupported",
    structural: asBool(raw["structural"]),
    ...(binding ? { binding } : {}),
    ...(raw["note"] ? { note: asString(raw["note"]) } : {}),
    ...(raw["unit"] ? { unit: asString(raw["unit"]) } : {}),
    ...(raw["impact"] ? { impact: asRecord(raw["impact"]) } : {}),
    ...(raw["reason"] ? { reason: asString(raw["reason"]) } : {}),
  };
}

function parseElementCount(raw: unknown) {
  const source = asRecord(raw);
  const count: RampElementCount = {
    value: asNumber(source["value"]),
    baseline: asNullableNumber(source["baseline"]),
    structural: asBool(source["structural"]),
    cost: asCost(source["cost"]),
    supported: asBool(source["supported"]),
    // 结构字段：即使后端误标成可编辑，前端也按只读处理（数量增删属结构改动）
    editable: false,
    readonly_reason: (source["readonly_reason"] as ReadonlyReason | null) ?? "structural",
    minimum: asNumber(source["minimum"], 2),
    maximum: asNumber(source["maximum"], 32),
  };
  return count;
}

function parseRamp(raw: Record<string, unknown>, base: NodeBase): RampNode {
  const elements = asArray(raw["elements"]).map((item, index) => {
    const element = asRecord(item);
    const position = asRecord(element["position"]);
    const color = asRecord(element["color"]);
    return {
      index: asNumber(element["index"], index),
      position: {
        value: asNumber(position["value"]),
        baseline: asNullableNumber(position["baseline"]),
        minimum: asNumber(position["minimum"], 0),
        maximum: asNumber(position["maximum"], 1),
        step: asNullableNumber(position["step"]) ?? undefined,
      },
      color: {
        value: asNumberArray(color["value"]) ?? [0, 0, 0, 1],
        baseline: asNumberArray(color["baseline"]),
      },
    };
  });

  const interpolationRaw = asRecord(raw["interpolation"]);
  const options = asArray(interpolationRaw["options"]).map((item) => {
    const option = asRecord(item);
    return { value: asString(option["value"]), label: asString(option["label"]) };
  });

  return {
    ...base,
    kind: "ramp",
    elements,
    interpolation: {
      value: asString(interpolationRaw["value"]) || "LINEAR",
      baseline: (interpolationRaw["baseline"] as string | null) ?? null,
      options,
    },
    element_count: parseElementCount(raw["element_count"]),
    checks: asArray(raw["checks"]).map((item) => asRecord(item)),
  };
}

/** 单节点解析。未知 kind 与未知结构都落到 `unknown`（只读）。 */
export function parseNode(raw: unknown): SurfaceNode | null {
  const source = asRecord(raw);
  const id = asString(source["id"]);
  if (!id) {
    return null;
  }
  const kind = asString(source["kind"]);
  if (!KNOWN_KINDS.includes(kind as NodeKind)) {
    return degradedNode(source, id, kind);
  }

  const base = baseOf(source, kind as NodeKind);
  switch (kind) {
    case "group": {
      const children: SurfaceNode[] = [];
      for (const child of asArray(source["children"])) {
        const parsed = parseNode(child);
        if (parsed) {
          children.push(parsed);
        }
      }
      const group: GroupNode = { ...base, kind: "group", children };
      return group;
    }
    case "ramp":
      return parseRamp(source, base);
    case "enum": {
      const options = asArray(source["options"]).map((item) => {
        const option = asRecord(item);
        return { value: asString(option["value"]), label: asString(option["label"]) };
      });
      const node: EnumNode = {
        ...base,
        kind: "enum",
        value: (source["value"] as string | null) ?? null,
        baseline: (source["baseline"] as string | null) ?? null,
        effective: (source["effective"] as string | null) ?? null,
        options,
        depends_on: (source["depends_on"] as string | null) ?? null,
        options_dynamic: asBool(source["options_dynamic"]),
      };
      return node;
    }
    case "color":
    case "vector": {
      const common = {
        ...base,
        value: asNumberArray(source["value"]),
        baseline: asNumberArray(source["baseline"]),
        effective: asNumberArray(source["effective"]),
      };
      if (kind === "color") {
        return { ...common, kind: "color" };
      }
      return {
        ...common,
        kind: "vector",
        axes: asArray(source["axes"]).map((axis) => asString(axis)),
        space: asString(source["space"]) || "world",
        locked_ratio: asBool(source["locked_ratio"]),
      };
    }
    default: {
      const node: ScalarNode = {
        ...base,
        kind: kind as "float" | "int" | "bool",
        value: (source["value"] as number | boolean | null) ?? null,
        baseline: (source["baseline"] as number | boolean | null) ?? null,
        effective: (source["effective"] as number | boolean | null) ?? null,
        minimum: asNullableNumber(source["minimum"]),
        maximum: asNullableNumber(source["maximum"]),
        step: asNullableNumber(source["step"]),
      };
      return node;
    }
  }
}

function degradedNode(
  source: Record<string, unknown>,
  id: string,
  kind: string
): UnknownNode {
  const base = baseOf(source, "unknown");
  return {
    ...base,
    id,
    kind: "unknown",
    // 未知 kind **永远不可编辑**：前端看不懂的结构，写下去只会写错。
    supported: false,
    editable: false,
    active: false,
    readonly_reason: "unsupported_kind",
    value_source: "unsupported",
    raw: source,
    label: base.label || id,
    ...(kind ? { note: `未知参数类型 ${kind}（前端版本较旧），已降级为只读。` } : {}),
  };
}

export function parseSchema(raw: unknown): SurfaceSchema {
  const source = asRecord(raw);
  const groups: SurfaceNode[] = [];
  for (const item of asArray(source["groups"])) {
    const parsed = parseNode(item);
    if (parsed) {
      groups.push(parsed);
    }
  }
  return {
    schema_version: asString(source["schema_version"]),
    groups,
    highest_cost: asCost(source["highest_cost"]),
    surface_baseline_id: source["surface_baseline_id"] as string | undefined,
    structure_hash: source["structure_hash"] as string | undefined,
    compositor_group: source["compositor_group"] as string | undefined,
    degraded: asArray(source["degraded"]).map((item) => asString(item)),
  };
}

/** 深度优先遍历（含分组自身）。 */
export function walk(nodes: SurfaceNode[]): SurfaceNode[] {
  const out: SurfaceNode[] = [];
  for (const node of nodes) {
    out.push(node);
    if (node.kind === "group") {
      out.push(...walk(node.children));
    }
  }
  return out;
}

export function flatten(nodes: SurfaceNode[]): Map<string, SurfaceNode> {
  const index = new Map<string, SurfaceNode>();
  for (const node of walk(nodes)) {
    index.set(node.id, node);
  }
  return index;
}

/** 分组列表（左侧导航用）。 */
export function topGroups(schema: SurfaceSchema | null): GroupNode[] {
  if (!schema) {
    return [];
  }
  return schema.groups.filter((node): node is GroupNode => node.kind === "group");
}
