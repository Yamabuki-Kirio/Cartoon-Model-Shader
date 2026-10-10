/**
 * 色带（ColorRamp）的取值规则与校验。
 *
 * 与后端 `src/server/surface/executor.py` 的 `normalise_ramp_elements` 对齐，
 * 但**前端校验只是「早点给出可读反馈」**：真正不许写坏工程的那道闸门在服务端
 * 与 Blender 侧。因此这里只做形态与范围判定，不承担安全职责。
 *
 * 三条规则（来自任务书）：
 * * 位置必须落在 `0 ≤ position ≤ 1`；
 * * 位置**严格递增**；
 * * RGBA 每个分量 `0–1`，且恰好 4 个分量。
 *
 * 刻意**不提供**新增 / 删除色标的能力：色标数量是结构信息，增删属 L3 结构性改动，
 * 当前一律只读（后端 `element_count.editable === false`）。
 */

import type { RampDraftValue, RampElement, RampNode } from "./types";

/** 拖动时相邻色标之间保留的最小间隔，保证位置「严格递增」而不是「非递减」。 */
export const MIN_POSITION_GAP = 0.001;

export function clamp01(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

export function isValidPosition(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

export function isValidColor(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === 4 &&
    value.every((item) => typeof item === "number" && Number.isFinite(item) && item >= 0 && item <= 1)
  );
}

/** 位置严格递增（不允许相等）。 */
export function positionsStrictlyIncreasing(positions: number[]): boolean {
  for (let index = 1; index < positions.length; index += 1) {
    if (!(positions[index] > positions[index - 1])) {
      return false;
    }
  }
  return true;
}

export function elementsFromNode(node: RampNode): RampElement[] {
  return node.elements.map((element) => ({
    index: element.index,
    position: { ...element.position },
    color: { value: [...element.color.value], baseline: element.color.baseline },
  }));
}

/**
 * 把 schema 节点转成可直接提交的色带草稿。
 *
 * `interpolation` 取节点的当前值 —— 提交的是**完整**色带（位置 + 颜色 + 插值），
 * 与服务端「整体写入」的语义一致。
 */
export function draftValueFromNode(node: RampNode): RampDraftValue {
  return {
    elements: node.elements.map((element) => ({
      position: element.position.value,
      color: [...element.color.value],
    })),
    interpolation: node.interpolation.value,
  };
}

/** 色带草稿的形态校验；返回错误说明（`null` = 合法）。 */
export function validateDraftValue(value: unknown): string | null {
  const source = value as RampDraftValue | null;
  if (!source || typeof source !== "object" || !Array.isArray(source.elements)) {
    return "色带取值必须是 {elements, interpolation}。";
  }
  if (source.elements.length < 2) {
    return "色带至少需要 2 个色标。";
  }
  for (const element of source.elements) {
    if (!element || typeof element !== "object") {
      return "色标必须是对象。";
    }
    if (!isValidPosition(element.position)) {
      return `色标位置越界：${String(element.position)}（须在 0–1）。`;
    }
    if (!isValidColor(element.color)) {
      return "色标颜色必须是 0–1 的 4 个分量。";
    }
  }
  const positions = source.elements.map((element) => element.position);
  if (!positionsStrictlyIncreasing(positions)) {
    return "色标位置必须严格递增。";
  }
  return null;
}

/**
 * 第 `index` 个色标允许的取值区间。
 *
 * 首尾色标固定在 `0` 与 `1`（ColorRamp 的端点若被拖到中间，色带外沿行为会变得难以预期，
 * 而当前不提供增删色标的手段，用户无法自行补回端点），中间色标夹在左右邻居之间。
 */
export function neighborBounds(
  elements: Array<{ position: number }>,
  index: number,
  gap: number = MIN_POSITION_GAP
): { min: number; max: number } {
  const isFirst = index <= 0;
  const isLast = index >= elements.length - 1;
  if (isFirst) {
    return { min: 0, max: 0 };
  }
  if (isLast) {
    return { min: 1, max: 1 };
  }
  const left = elements[index - 1]?.position ?? 0;
  const right = elements[index + 1]?.position ?? 1;
  return { min: left + gap, max: right - gap };
}

/** 把拖动 / 输入的位置夹进允许区间（含严格递增的间隔要求）。 */
export function clampPositionForIndex(
  elements: Array<{ position: number }>,
  index: number,
  desired: number
): number {
  const bounds = neighborBounds(elements, index);
  const value = clamp01(desired);
  if (bounds.min > bounds.max) {
    // 邻居间距已经小于最小间隔：保持原位，不要制造交叉。
    return elements[index]?.position ?? 0;
  }
  return Math.min(bounds.max, Math.max(bounds.min, value));
}

/** 两个色带是否「兼容复制」：色标数量相同。source 为 null 表示源组没有可复制的色带。 */
export function canCopyInto(source: RampNode | null, target: RampNode | null): string | null {
  if (!source) {
    return "源组没有可复制的色带。";
  }
  if (!target) {
    return "目标组不存在。";
  }
  if (!target.supported) {
    return "目标组在当前工程中探测不到。";
  }
  if (!target.editable) {
    return `目标组不可编辑（${target.readonly_reason ?? "未知原因"}）。`;
  }
  if (source.elements.length !== target.elements.length) {
    return `色标数量不同（源 ${source.elements.length} 个、目标 ${target.elements.length} 个），不截断也不补齐。`;
  }
  if (source.elements.length < 2) {
    return "色带至少需要 2 个色标。";
  }
  return null;
}

/** 把源色带的位置与颜色复制到目标色标数量相同的组（插值也一并带走）。 */
export function copyRampValue(
  source: RampDraftValue,
  targetElementCount: number
): RampDraftValue | null {
  if (source.elements.length !== targetElementCount) {
    return null;
  }
  return {
    elements: source.elements.map((element) => ({
      position: element.position,
      color: [...element.color],
    })),
    interpolation: source.interpolation,
  };
}

export function rgbaToCss(color: number[]): string {
  const [r = 0, g = 0, b = 0, a = 1] = color;
  const to255 = (value: number) => Math.round(clamp01(value) * 255);
  return `rgba(${to255(r)}, ${to255(g)}, ${to255(b)}, ${clamp01(a).toFixed(3)})`;
}

/** 把色带渲染成 CSS `linear-gradient`，用于只读/可编辑两种色带预览。
 *
 * `CONSTANT` 插值要画成**硬边**（每段保持左端色直到下一档），否则界面会给出
 * 与 Blender 不同的观感 —— 用户按图调参就会调错。
 */
export function rampToCssGradient(
  elements: Array<{ position: number; color: number[] }>,
  interpolation = "LINEAR"
): string {
  if (elements.length === 0) {
    return "linear-gradient(to right, #000, #000)";
  }
  const percent = (value: number) => `${(clamp01(value) * 100).toFixed(2)}%`;
  if (interpolation === "CONSTANT") {
    const stops: string[] = [];
    elements.forEach((element, index) => {
      const next = elements[index + 1];
      const end = next ? clamp01(next.position) : 1;
      stops.push(`${rgbaToCss(element.color)} ${percent(element.position)}`);
      stops.push(`${rgbaToCss(element.color)} ${percent(end)}`);
    });
    return `linear-gradient(to right, ${stops.join(", ")})`;
  }
  const stops = elements.map(
    (element) => `${rgbaToCss(element.color)} ${percent(element.position)}`
  );
  return `linear-gradient(to right, ${stops.join(", ")})`;
}
