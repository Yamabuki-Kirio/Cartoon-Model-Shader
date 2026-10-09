import { describe, expect, it } from "vitest";
import { flatten, parseNode, parseSchema, topGroups, walk } from "./parse";
import { schemaPayload } from "../testing/fixtures";

describe("递归 schema 解析", () => {
  it("解析出分组与子节点，并保留状态四件", () => {
    const schema = parseSchema(schemaPayload());
    expect(schema.schema_version).toBe("toon-surface/2");
    const groups = topGroups(schema);
    expect(groups.map((group) => group.label)).toEqual([
      "Cel_Skin",
      "Cel_Hair",
      "Sakura_Hair_Reference",
    ]);

    const skin = groups[0];
    expect(skin.supported).toBe(true);
    expect(skin.editable).toBe(true);
    expect(skin.active).toBe(true);
    expect(skin.readonly_reason).toBeNull();
    expect(skin.cost).toBe("L1");
  });

  it("色带是单一复合节点，色标数量单独标为结构信息", () => {
    const schema = parseSchema(schemaPayload());
    const ramp = flatten(schema.groups).get("cel.Cel_Skin.ramp");
    expect(ramp?.kind).toBe("ramp");
    if (ramp?.kind !== "ramp") {
      throw new Error("应当解析成 ramp 节点");
    }
    expect(ramp.elements).toHaveLength(3);
    expect(ramp.elements[0].color.value).toEqual([0.05, 0.05, 0.08, 1.0]);
    expect(ramp.element_count.structural).toBe(true);
    // 数量即使后端误标成可编辑，前端也按只读处理
    expect(ramp.element_count.editable).toBe(false);
    expect(ramp.element_count.readonly_reason).toBe("rollback_unavailable");
    expect(ramp.interpolation.value).toBe("LINEAR");
  });

  it("未知 kind 降级为只读 unknown 节点（不当作可写）", () => {
    const node = parseNode({
      id: "cel.Cel_Skin.mystery",
      kind: "quaternion",
      group: "cel",
      cost: "L2",
      label: "神秘参数",
      supported: true,
      editable: true,
      active: true,
      value_source: "scene",
    });
    expect(node?.kind).toBe("unknown");
    expect(node?.supported).toBe(false);
    expect(node?.editable).toBe(false);
    expect(node?.active).toBe(false);
    expect(node?.readonly_reason).toBe("unsupported_kind");
    expect(node?.note).toContain("quaternion");
  });

  it("字段缺失按「不可用」处理，绝不默认成可写", () => {
    const node = parseNode({ id: "cel.Cel_Skin.x", kind: "float" });
    expect(node?.supported).toBe(false);
    expect(node?.editable).toBe(false);
    expect(node?.active).toBe(false);
    expect(node?.value_source).toBe("unsupported");
  });

  it("没有 id 的节点被丢弃（不生成无名控件）", () => {
    expect(parseNode({ kind: "float" })).toBeNull();
    const schema = parseSchema({ groups: [{ kind: "group" }, schemaPayload().groups[0]] });
    expect(schema.groups).toHaveLength(1);
  });

  it("遍历与索引覆盖整棵树", () => {
    const schema = parseSchema(schemaPayload());
    const all = walk(schema.groups);
    // 3 个分组 + Cel_Skin 的 4 个子项 + Cel_Hair 1 个 + Sakura 1 个
    expect(all).toHaveLength(9);
    const index = flatten(schema.groups);
    expect(index.has("cel.Cel_Skin.ramp")).toBe(true);
    expect(index.has("cel.Cel_Hair.ramp")).toBe(true);
    // 只读展示项也在索引里（可被检查器展示，但不可写）
    expect(index.get("cel.Cel_Skin.managed_mode")?.editable).toBe(false);
  });

  it("未知 kind 出现在分组内时同样降级，且不影响兄弟节点", () => {
    const payload = schemaPayload({
      groups: [
        {
          id: "cel.Cel_Skin",
          kind: "group",
          group: "cel",
          cost: "L1",
          label: "Cel_Skin",
          supported: true,
          editable: true,
          active: true,
          value_source: "scene",
          children: [{ id: "cel.Cel_Skin.future", kind: "table", supported: true, editable: true }],
        },
      ],
    });
    const schema = parseSchema(payload);
    const children = topGroups(schema)[0].children;
    expect(children).toHaveLength(1);
    expect(children[0].kind).toBe("unknown");
    expect(children[0].editable).toBe(false);
  });

  it("降级清单透传", () => {
    const schema = parseSchema(schemaPayload());
    expect(schema.degraded).toContain("Cel_Cloth");
    expect(schema.structure_hash).toBe("f".repeat(64));
  });
});
