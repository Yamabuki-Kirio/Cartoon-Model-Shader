import { describe, expect, it } from "vitest";
import {
  hexToRgba,
  isMaterialGroup,
  materialInfo,
  materialNameOf,
  materialParamCounts,
  materialParams,
  rgbaToHex,
} from "./material";
import { parseSchema, topGroups } from "./parse";
import { materialNode, schemaPayload } from "../testing/fixtures";

function parseGroup(raw: unknown) {
  const schema = parseSchema(schemaPayload({ groups: [raw] }));
  const group = topGroups(schema)[0];
  if (!group) {
    throw new Error("夹具没有解析出分组");
  }
  return group;
}

describe("逐材质 schema 助手", () => {
  it("按 id 前缀识别材质分组，并取回材质名", () => {
    const material = parseGroup(materialNode({ material: "皮肤_脸" }));
    expect(isMaterialGroup(material)).toBe(true);
    expect(materialNameOf(material)).toBe("皮肤_脸");

    const cel = parseGroup({
      id: "cel.Cel_Skin",
      kind: "group",
      group: "cel",
      cost: "L1",
      label: "Cel_Skin",
      supported: true,
      editable: true,
      active: true,
      readonly_reason: null,
      value_source: "scene",
      structural: false,
      children: [],
    });
    expect(isMaterialGroup(cel)).toBe(false);
    expect(isMaterialGroup(undefined)).toBe(false);
    expect(isMaterialGroup(null)).toBe(false);
  });

  it("分类 / 目标组 / 使用对象都从 impact 读出来，并带上分类标签", () => {
    const group = parseGroup(
      materialNode({
        material: "头发_前",
        classification: "hair",
        classificationLabel: "头发",
        targetGroup: "Cel_Hair",
        users: ["model_mesh", "model_hair"],
        nodeCount: 5,
        blendMethod: "HASHED",
      })
    );
    const info = materialInfo(group);
    expect(info.name).toBe("头发_前");
    expect(info.classification).toBe("hair");
    expect(info.classificationLabel).toBe("头发");
    expect(info.targetGroup).toBe("Cel_Hair");
    expect(info.users).toEqual(["model_mesh", "model_hair"]);
    expect(info.slotCount).toBe(2);
    expect(info.nodeCount).toBe(5);
    expect(info.blendMethod).toBe("HASHED");
  });

  it("impact 缺失时回落到只读子节点，读不到就如实给 null / 空数组", () => {
    const raw = materialNode({ classification: "eye", targetGroup: "RayToon_Eyes_Unlit" }) as Record<
      string,
      unknown
    >;
    delete raw["impact"];
    const group = parseGroup(raw);
    const info = materialInfo(group);
    expect(info.classification).toBe("eye");
    expect(info.targetGroup).toBe("RayToon_Eyes_Unlit");
    // 使用对象只存在于 impact 里，删掉之后必须如实为空，不能编一个出来
    expect(info.users).toEqual([]);
    expect(info.slotCount).toBeNull();
  });

  it("分类判不出时给 null，界面据此显示「未分类」", () => {
    const group = parseGroup(materialNode({ classification: null, classificationLabel: null }));
    const info = materialInfo(group);
    expect(info.classification).toBeNull();
    expect(info.classificationLabel).toBeNull();
  });

  it("参数按后端固定顺序返回，并统计可调项数", () => {
    const group = parseGroup(
      materialNode({
        params: {
          // 插座被上游连线 ⇒ 只读，但**仍然出现在列表里**
          roughness: { editable: false, readonlyReason: "structural", reason: "插座已被连线" },
          // 探不到 ⇒ 只读占位
          specular: { supported: false, editable: false, readonlyReason: "not_found" },
        },
      })
    );
    const params = materialParams(group);
    expect(params.map((node) => node.id.split(".").pop())).toEqual([
      "base_color",
      "metallic",
      "roughness",
      "emission_color",
      "emission_strength",
      "specular",
    ]);
    expect(materialParamCounts(group)).toEqual({ writable: 4, total: 6 });
  });

  it("RGBA ⇄ hex 往返；非法输入不抛异常", () => {
    expect(rgbaToHex([1, 0, 0, 1])).toBe("#ff0000");
    expect(rgbaToHex([0, 0.5, 1, 1])).toBe("#0080ff");
    // 越界分量被夹回 0–1，而不是溢出成非法 hex
    expect(rgbaToHex([2, -1, 0.5, 1])).toBe("#ff0080");
    expect(rgbaToHex(null)).toBe("#000000");
    expect(rgbaToHex([0.1, 0.2])).toBe("#000000");

    expect(hexToRgba("#ff8000", 0.5)).toEqual([1, 128 / 255, 0, 0.5]);
    // 非法 hex 回黑色，alpha 仍沿用调用方给的值
    expect(hexToRgba("nope", 0.25)).toEqual([0, 0, 0, 0.25]);
  });
});
