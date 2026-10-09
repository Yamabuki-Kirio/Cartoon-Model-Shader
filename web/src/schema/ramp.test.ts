import { describe, expect, it } from "vitest";
import { parseSchema, topGroups } from "./parse";
import {
  MIN_POSITION_GAP,
  canCopyInto,
  clamp01,
  clampPositionForIndex,
  copyRampValue,
  draftValueFromNode,
  elementsFromNode,
  isValidColor,
  isValidPosition,
  neighborBounds,
  positionsStrictlyIncreasing,
  rampToCssGradient,
  validateDraftValue,
} from "./ramp";
import { groupNode, rampNode, schemaPayload } from "../testing/fixtures";
import type { RampNode } from "./types";

function rampOf(name: string, count = 3): RampNode {
  const payload = schemaPayload({
    groups: [
      groupNode(name, {
        children: [
          rampNode({
            group: name,
            elements: Array.from({ length: count }, (_, index) => [
              index / Math.max(1, count - 1),
              [0.1 * index, 0.2, 0.3, 1.0],
            ]) as Array<[number, number[]]>,
          }),
        ],
      }),
    ],
  });
  const node = topGroups(parseSchema(payload))[0].children[0];
  if (node.kind !== "ramp") {
    throw new Error("fixture 应当是 ramp");
  }
  return node;
}

describe("色带取值规则", () => {
  it("位置与颜色的范围判定", () => {
    expect(isValidPosition(0)).toBe(true);
    expect(isValidPosition(1)).toBe(true);
    expect(isValidPosition(1.0001)).toBe(false);
    expect(isValidPosition(-0.01)).toBe(false);
    expect(isValidPosition(Number.NaN)).toBe(false);
    expect(isValidPosition("0.5")).toBe(false);

    expect(isValidColor([0, 0, 0, 1])).toBe(true);
    expect(isValidColor([0, 0, 0])).toBe(false);
    expect(isValidColor([0, 0, 0, 1, 1])).toBe(false);
    expect(isValidColor([0, 0, 0, 1.2])).toBe(false);
  });

  it("clamp01 处理越界与非法数", () => {
    expect(clamp01(-3)).toBe(0);
    expect(clamp01(3)).toBe(1);
    expect(clamp01(0.42)).toBeCloseTo(0.42);
    expect(clamp01(Number.NaN)).toBe(0);
  });

  it("位置必须严格递增（相等也算非法）", () => {
    expect(positionsStrictlyIncreasing([0, 0.5, 1])).toBe(true);
    expect(positionsStrictlyIncreasing([0, 0.5, 0.5])).toBe(false);
    expect(positionsStrictlyIncreasing([0.5, 0, 1])).toBe(false);
  });

  it("草稿形态校验给出可读原因", () => {
    const ok = draftValueFromNode(rampOf("Cel_Skin"));
    expect(validateDraftValue(ok)).toBeNull();

    const outOfRange = { ...ok, elements: ok.elements.map((e, i) => (i === 0 ? { ...e, position: 9 } : e)) };
    expect(validateDraftValue(outOfRange)).toContain("越界");

    const badColor = {
      ...ok,
      elements: ok.elements.map((e, i) => (i === 0 ? { ...e, color: [0, 0, 0] } : e)),
    };
    expect(validateDraftValue(badColor)).toContain("4 个分量");

    const unordered = {
      ...ok,
      elements: ok.elements.map((e, i) => ({ ...e, position: i === 2 ? 0.1 : e.position })),
    };
    expect(validateDraftValue(unordered)).toContain("严格递增");

    expect(validateDraftValue({ elements: [{ position: 0, color: [0, 0, 0, 1] }], interpolation: "LINEAR" })).toContain(
      "至少需要 2 个色标"
    );
    expect(validateDraftValue(null)).toContain("必须是");
  });

  it("端点固定、中间色标夹在邻居之间", () => {
    const elements = [{ position: 0 }, { position: 0.5 }, { position: 1 }];
    expect(neighborBounds(elements, 0)).toEqual({ min: 0, max: 0 });
    expect(neighborBounds(elements, 2)).toEqual({ min: 1, max: 1 });
    // 中间色标的邻居是首尾两端
    expect(neighborBounds(elements, 1)).toEqual({ min: MIN_POSITION_GAP, max: 1 - MIN_POSITION_GAP });

    expect(clampPositionForIndex(elements, 1, 0.9)).toBeCloseTo(0.9);
    expect(clampPositionForIndex(elements, 1, -1)).toBeCloseTo(MIN_POSITION_GAP);
    expect(clampPositionForIndex(elements, 1, 0.7)).toBeCloseTo(0.7);
    // 端点拖不动
    expect(clampPositionForIndex(elements, 0, 0.5)).toBe(0);
    expect(clampPositionForIndex(elements, 2, 0.5)).toBe(1);
  });

  it("邻居间隔已被挤到小于最小间隔时保持原位（不制造交叉）", () => {
    // index 2 的左右邻居（0.2 与 0.2005）间距小于 2×gap ⇒ 可取值区间为空
    const elements = [{ position: 0 }, { position: 0.2 }, { position: 0.2002 }, { position: 0.2005 }, { position: 1 }];
    const bounds = neighborBounds(elements, 2);
    expect(bounds.min).toBeGreaterThan(bounds.max);
    expect(clampPositionForIndex(elements, 2, 0.9)).toBe(0.2002);
  });

  it("四色标也遵守同一套规则", () => {
    const ramp = rampOf("Cel_Skin", 4);
    expect(elementsFromNode(ramp)).toHaveLength(4);
    const draft = draftValueFromNode(ramp);
    expect(validateDraftValue(draft)).toBeNull();
    expect(positionsStrictlyIncreasing(draft.elements.map((e) => e.position))).toBe(true);
  });
});

describe("复制到兼容组", () => {
  it("色标数量相同才允许复制", () => {
    const source = rampOf("Cel_Skin", 3);
    const same = rampOf("Cel_Hair", 3);
    const different = rampOf("Cel_Cloth", 2);
    expect(canCopyInto(source, same)).toBeNull();
    expect(canCopyInto(source, different)).toContain("色标数量不同");
  });

  it("目标不可编辑 / 探测不到 / 不存在都被明确拒绝（不截断不补齐）", () => {
    const source = rampOf("Cel_Skin", 3);
    const readonlyTarget = { ...rampOf("Sakura", 3), editable: false, readonly_reason: "reference_only" };
    expect(canCopyInto(source, readonlyTarget)).toContain("不可编辑");
    const unsupported = { ...rampOf("Ghost", 3), supported: false };
    expect(canCopyInto(source, unsupported)).toContain("探测不到");
    expect(canCopyInto(source, null)).toContain("不存在");
    expect(canCopyInto(null, source)).toContain("源组");
  });

  it("复制结果带着插值一起走，且不改动色标数量", () => {
    const source = { elements: [{ position: 0, color: [0, 0, 0, 1] }, { position: 1, color: [1, 1, 1, 1] }], interpolation: "CONSTANT" };
    expect(copyRampValue(source, 3)).toBeNull();
    const copied = copyRampValue(source, 2);
    expect(copied?.interpolation).toBe("CONSTANT");
    expect(copied?.elements).toHaveLength(2);
    // 深拷贝：改复制结果不影响源
    copied!.elements[0].color[0] = 0.9;
    expect(source.elements[0].color[0]).toBe(0);
  });
});

describe("色带渲染", () => {
  it("LINEAR 生成逐档 stops", () => {
    const css = rampToCssGradient(
      [
        { position: 0, color: [0, 0, 0, 1] },
        { position: 1, color: [1, 1, 1, 1] },
      ],
      "LINEAR"
    );
    expect(css).toBe("linear-gradient(to right, rgba(0, 0, 0, 1.000) 0.00%, rgba(255, 255, 255, 1.000) 100.00%)");
  });

  it("CONSTANT 生成硬边（每段保持左端色）", () => {
    const css = rampToCssGradient(
      [
        { position: 0, color: [0, 0, 0, 1] },
        { position: 0.5, color: [1, 0, 0, 1] },
        { position: 1, color: [0, 0, 1, 1] },
      ],
      "CONSTANT"
    );
    // 第一段：0% 与 50% 都是黑色（硬边）；第二段：50% 与 100% 都是红色
    expect(css).toContain("rgba(0, 0, 0, 1.000) 0.00%, rgba(0, 0, 0, 1.000) 50.00%");
    expect(css).toContain("rgba(255, 0, 0, 1.000) 50.00%, rgba(255, 0, 0, 1.000) 100.00%");
  });

  it("空色带不崩", () => {
    expect(rampToCssGradient([])).toContain("linear-gradient");
  });
});
