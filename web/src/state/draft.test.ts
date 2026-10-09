import { describe, expect, it } from "vitest";
import { parseSchema, topGroups } from "../schema/parse";
import { schemaPayload } from "../testing/fixtures";
import {
  baselineValue,
  computeDirtyIds,
  nodeValue,
  resetAll,
  resetGroup,
  sanitizeDraft,
  setDraftValue,
  valuesEqual,
  writableIdsOf,
} from "./draft";

const schema = parseSchema(schemaPayload());

describe("dirtyIds 计算", () => {
  it("空草稿没有脏项", () => {
    expect(computeDirtyIds(schema, {}).size).toBe(0);
  });

  it("与基线一致的值不算脏（浮点容差内也算一致）", () => {
    const same = {
      "cel.Cel_Skin.ramp": {
        elements: [
          { position: 0.0, color: [0.05, 0.05, 0.08, 1.0] },
          { position: 0.5, color: [0.5, 0.48, 0.46, 1.0] },
          { position: 1.0, color: [0.95, 0.94, 0.92, 1.0] },
        ],
        interpolation: "LINEAR",
      },
    };
    expect(computeDirtyIds(schema, same).size).toBe(0);

    const nudged = {
      "cel.Cel_Skin.ramp": {
        elements: [
          { position: 1e-9, color: [0.05, 0.05, 0.08, 1.0] },
          { position: 0.5, color: [0.5, 0.48, 0.46, 1.0] },
          { position: 1.0, color: [0.95, 0.94, 0.92, 1.0] },
        ],
        interpolation: "LINEAR",
      },
    };
    expect(computeDirtyIds(schema, nudged).size).toBe(0);
  });

  it("改了颜色 / 插值即算脏", () => {
    const changed = {
      "cel.Cel_Skin.ramp": {
        elements: [
          { position: 0.0, color: [0.9, 0.05, 0.08, 1.0] },
          { position: 0.5, color: [0.5, 0.48, 0.46, 1.0] },
          { position: 1.0, color: [0.95, 0.94, 0.92, 1.0] },
        ],
        interpolation: "LINEAR",
      },
    };
    expect(computeDirtyIds(schema, changed).has("cel.Cel_Skin.ramp")).toBe(true);

    const interpolated = {
      "cel.Cel_Skin.ramp": {
        elements: [
          { position: 0.0, color: [0.05, 0.05, 0.08, 1.0] },
          { position: 0.5, color: [0.5, 0.48, 0.46, 1.0] },
          { position: 1.0, color: [0.95, 0.94, 0.92, 1.0] },
        ],
        interpolation: "CONSTANT",
      },
    };
    expect(computeDirtyIds(schema, interpolated).has("cel.Cel_Skin.ramp")).toBe(true);
  });

  it("草稿里出现 schema 没有的 id 时也计入（不静默忽略）", () => {
    const stale = { "cel.Cel_Gone.ramp": { elements: [], interpolation: "LINEAR" } };
    expect(computeDirtyIds(schema, stale).has("cel.Cel_Gone.ramp")).toBe(true);
  });
});

describe("草稿读写与取值回落", () => {
  it("草稿优先，否则回落基线", () => {
    const ramp = topGroups(schema)[0].children[0];
    const empty = {};
    expect(nodeValue(empty, ramp)).toBeTruthy();
    const withDraft = setDraftValue(empty, ramp.id, { elements: [], interpolation: "EASE" });
    expect(nodeValue(withDraft, ramp)).toEqual({ elements: [], interpolation: "EASE" });
    expect(baselineValue(ramp)).toBeTruthy();
  });

  it("valuesEqual 递归比较", () => {
    expect(valuesEqual({ a: [1, 2] }, { a: [1, 2] })).toBe(true);
    expect(valuesEqual({ a: [1, 2] }, { a: [1, 2.0000001] })).toBe(true);
    expect(valuesEqual({ a: 1 }, { a: 2 })).toBe(false);
    expect(valuesEqual([1, 2], [1])).toBe(false);
    expect(valuesEqual(null, null)).toBe(true);
  });
});

describe("复位与提交前过滤", () => {
  it("复位当前组只清掉该子树", () => {
    const draft = {
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" },
      "cel.Cel_Hair.ramp": { elements: [], interpolation: "LINEAR" },
    };
    const next = resetGroup(draft, schema, "cel.Cel_Skin");
    expect(next["cel.Cel_Skin.ramp"]).toBeUndefined();
    expect(next["cel.Cel_Hair.ramp"]).toBeDefined();
  });

  it("复位全部清掉 schema 覆盖的全部 id，保留陌生 id", () => {
    const draft = {
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" },
      "something.else": 1,
    };
    const next = resetAll(draft, schema);
    expect(next["cel.Cel_Skin.ramp"]).toBeUndefined();
    expect(next["something.else"]).toBe(1);
  });

  it("提交前过滤掉不可写与 schema 之外的 id", () => {
    const draft = {
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" },
      // 只读展示项：不允许提交
      "cel.Cel_Skin.managed_mode": "editable",
      // 未确认能力：不允许提交
      "cel.Cel_Skin.emission_strength": 3,
      // 参考组：不可编辑
      "cel.Sakura_Hair_Reference.ramp": { elements: [], interpolation: "LINEAR" },
      // schema 之外
      "nope.nope": 1,
    };
    const clean = sanitizeDraft(schema, draft);
    expect(Object.keys(clean)).toEqual(["cel.Cel_Skin.ramp"]);
    expect(clean["cel.Cel_Skin.ramp"]).toEqual({ elements: [], interpolation: "LINEAR" });
  });

  it("writableIdsOf 只给出可写 id", () => {
    const ids = writableIdsOf(schema.groups[0]);
    expect(ids).toContain("cel.Cel_Skin.ramp");
    expect(ids).not.toContain("cel.Cel_Skin.emission_strength");
    expect(ids).not.toContain("cel.Cel_Skin.managed_mode");
  });
});
