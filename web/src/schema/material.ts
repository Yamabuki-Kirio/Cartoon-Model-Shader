/**
 * 逐材质分组（`material.<材质名>`）的读取助手。
 *
 * 这里**只读 schema 已经给出的东西**，不重新分类、不按名字猜：
 * 分类、目标 Cel 组、使用对象都由服务端（唯一规则源 `material_rules.json`）判定，
 * 前端把 `impact` 原样展示出来即可。前端再判一遍，就会出现两套规则、两套结论。
 *
 * 一个材质分组的孩子节点是**固定集合**（探到与探不到都生成，只是 `supported`
 * 不同），因此这里可以按 id 后缀取值，不需要为「这次有没有这个参数」写分支。
 */

import type { ColorNode, GroupNode, ScalarNode, SurfaceNode } from "./types";

export const MATERIAL_GROUP_PREFIX = "material.";

/** 材质分组里的只读展示节点（分类 / 目标 Cel 组 / 使用对象数）。 */
export const MATERIAL_META_SUFFIXES = {
  classification: ".classification",
  targetGroup: ".target_group",
  userCount: ".users.count",
} as const;

/** 材质分组里 6 个安全参数的后缀（顺序与后端 `FIELD_META` 一致）。 */
export const MATERIAL_PARAM_SUFFIXES = [
  ".base_color",
  ".metallic",
  ".roughness",
  ".emission_color",
  ".emission_strength",
  ".specular",
] as const;

export interface MaterialInfo {
  /** 材质名（分组 id 去掉前缀）。 */
  name: string;
  /** 分类键（如 `skin`）；服务端判不出时为 null。 */
  classification: string | null;
  /** 分类的中文标签（取自只读 enum 的选项），取不到就回落到分类键。 */
  classificationLabel: string | null;
  /** 该材质按分类规则落到的 Cel 节点组。 */
  targetGroup: string | null;
  /** 使用该材质的对象名（这就是「影响范围」）。 */
  users: string[];
  /** 材质槽数量（与 users 同源，单独报出便于展示）。 */
  slotCount: number | null;
  nodeCount: number | null;
  blendMethod: string | null;
}

/** 是否是逐材质分组。 */
export function isMaterialGroup(node: SurfaceNode | undefined | null): node is GroupNode {
  return Boolean(
    node && node.kind === "group" && node.id.startsWith(MATERIAL_GROUP_PREFIX)
  );
}

/** 从分组 id 取材质名（`material.皮肤_脸` → `皮肤_脸`）。 */
export function materialNameOf(group: GroupNode): string {
  return group.id.slice(MATERIAL_GROUP_PREFIX.length);
}

function childOf(group: GroupNode, suffix: string): SurfaceNode | undefined {
  return group.children.find((child) => child.id.endsWith(suffix));
}

/**
 * 汇总一个材质分组的展示信息。
 *
 * 优先读 `impact`（那是服务端给的**结构化**影响范围），读不到再回落到只读子节点。
 * 两条路都读不到就如实给 null / 空数组 —— 界面显示「—」，不猜。
 */
export function materialInfo(group: GroupNode): MaterialInfo {
  const impact = (group.impact ?? {}) as Record<string, unknown>;

  const classificationNode = childOf(group, MATERIAL_META_SUFFIXES.classification);
  const rawClass =
    typeof impact["classification"] === "string"
      ? (impact["classification"] as string)
      : classificationNode && classificationNode.kind === "enum"
        ? classificationNode.value
        : null;
  const optionLabel =
    classificationNode && classificationNode.kind === "enum"
      ? classificationNode.options.find((option) => option.value === rawClass)?.label
      : undefined;

  const targetNode = childOf(group, MATERIAL_META_SUFFIXES.targetGroup);
  const targetGroup =
    typeof impact["target_group"] === "string"
      ? (impact["target_group"] as string)
      : targetNode && targetNode.kind === "enum"
        ? targetNode.value
        : null;

  const users = Array.isArray(impact["users"])
    ? (impact["users"] as unknown[]).map((item) => String(item))
    : [];
  const slotCount =
    typeof impact["slot_count"] === "number" ? (impact["slot_count"] as number) : null;
  const nodeCount =
    typeof impact["node_count"] === "number" ? (impact["node_count"] as number) : null;
  const blendMethod =
    typeof impact["blend_method"] === "string" && impact["blend_method"]
      ? (impact["blend_method"] as string)
      : null;

  return {
    name: materialNameOf(group),
    classification: rawClass,
    classificationLabel: optionLabel ?? rawClass,
    targetGroup,
    users,
    slotCount,
    nodeCount,
    blendMethod,
  };
}

/** 可编辑的安全参数（按后端固定顺序，只保留真的可写的）。 */
export function materialParams(group: GroupNode): SurfaceNode[] {
  const out: SurfaceNode[] = [];
  for (const suffix of MATERIAL_PARAM_SUFFIXES) {
    const node = childOf(group, suffix);
    if (node) {
      out.push(node);
    }
  }
  return out;
}

/** 可编辑参数的数量与总数（用于导航徽标：「3 / 6 可调」）。 */
export function materialParamCounts(group: GroupNode): { writable: number; total: number } {
  const params = materialParams(group);
  return {
    writable: params.filter((node) => node.supported && node.editable).length,
    total: params.length,
  };
}

export function asColorNode(node: SurfaceNode | undefined): ColorNode | null {
  return node && node.kind === "color" ? node : null;
}

export function asScalarNode(node: SurfaceNode | undefined): ScalarNode | null {
  return node && (node.kind === "float" || node.kind === "int" || node.kind === "bool")
    ? node
    : null;
}

/** RGBA 数组 → `#rrggbb`（颜色输入的 value 只认 hex）。 */
export function rgbaToHex(rgba: unknown): string {
  if (!Array.isArray(rgba) || rgba.length < 3) {
    return "#000000";
  }
  const channel = (value: unknown) => {
    const number = typeof value === "number" && Number.isFinite(value) ? value : 0;
    return Math.max(0, Math.min(255, Math.round(number * 255)))
      .toString(16)
      .padStart(2, "0");
  };
  return `#${channel(rgba[0])}${channel(rgba[1])}${channel(rgba[2])}`;
}

/** `#rrggbb` → RGBA 数组（alpha 沿用原值，避免把不透明度悄悄改成 1）。 */
export function hexToRgba(hex: string, alpha = 1): number[] {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) {
    return [0, 0, 0, alpha];
  }
  const value = match[1];
  const parse = (offset: number) => parseInt(value.slice(offset, offset + 2), 16) / 255;
  return [parse(0), parse(2), parse(4), alpha];
}
