import { describe, expect, it } from "vitest";
import {
  CONFIDENCE_LABELS,
  GRAPH_SECTIONS,
  confirmRequestFromDetails,
  emptyGraphFilter,
  matchesGraphFilter,
  parseConfidence,
  parseGraphParam,
  parseMaterialGraphSchema,
  rollbackNote,
  sectionOf,
  sharedGroupOf,
  socketLabelOf,
  splitGraphDraft,
  unconfirmedSharedGroupsInSchema,
  writableGraphParams,
  type MaterialGraphSchema,
} from "./graph";
import {
  defaultGraphParameters,
  graphInferredFixture,
  graphMaterialFixture,
  graphParamFixture,
  graphRampFixture,
  graphSchemaPayload,
  graphSharedFixture,
  graphUnsupportedFixture,
} from "../testing/fixtures";

function schemaOf(materials: unknown[]): MaterialGraphSchema {
  return parseMaterialGraphSchema(graphSchemaPayload({ materials }));
}

describe("材质图 schema 解析 · 识别等级", () => {
  it("五个等级各有固定文案，且不会互相冒充", () => {
    expect(CONFIDENCE_LABELS.confirmed).toBe("已确认");
    expect(CONFIDENCE_LABELS.inferred).toBe("推测");
    expect(CONFIDENCE_LABELS.user_confirmed).toBe("用户确认");
    expect(CONFIDENCE_LABELS.experimental).toBe("实验性");
    expect(CONFIDENCE_LABELS.unsupported).toBe("不支持");
  });

  it("未知等级降级为 unsupported，并保留原始串以便排查", () => {
    const parsed = parseConfidence("probably_fine");
    expect(parsed.confidence).toBe("unsupported");
    expect(parsed.raw).toBe("probably_fine");
  });

  it("未知等级的参数一定不可写（前端比后端旧时绝不假装能写）", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-x", { confidence: "probably_fine", editable: true, supported: true })
    );
    expect(param).not.toBeNull();
    expect(param?.node.editable).toBe(false);
    expect(param?.node.supported).toBe(false);
    expect(param?.node.readonly_reason).toBe("unconfirmed_capability");
    expect(param?.meta.frontendDowngrade).toBe("unconfirmed_capability");
  });

  it("未知 kind 降级为 unknown 只读节点，并说明原因", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-y", { kind: "shader_socket_v9", editable: true, supported: true })
    );
    expect(param?.node.kind).toBe("unknown");
    expect(param?.node.editable).toBe(false);
    expect(param?.node.supported).toBe(false);
    expect(param?.node.readonly_reason).toBe("unsupported_kind");
    expect(param?.node.note).toContain("shader_socket_v9");
  });

  it("服务端标成不可写的参数，前端绝不改成可写", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-z", {
        confidence: "inferred",
        editable: false,
        readonly_reason: "inferred_not_confirmed",
      })
    );
    expect(param?.meta.confidence).toBe("inferred");
    expect(param?.node.editable).toBe(false);
    // 兜底原因码：后端没给也要有一个**准确**的理由，不能空着
    const noReason = parseGraphParam(
      graphParamFixture("mg-z2", { confidence: "experimental", editable: false, readonly_reason: null })
    );
    expect(noReason?.node.readonly_reason).toBe("experimental_semantics");
  });

  it("缺少 supported / editable 字段时按不可用处理", () => {
    const raw = graphParamFixture("mg-missing") as Record<string, unknown>;
    delete raw["supported"];
    delete raw["editable"];
    const param = parseGraphParam(raw);
    expect(param?.node.supported).toBe(false);
    expect(param?.node.editable).toBe(false);
  });
});

describe("材质图 schema 解析 · 分区", () => {
  it("七个分区顺序固定，与规格一致", () => {
    expect(GRAPH_SECTIONS.map((item) => item.id)).toEqual([
      "common",
      "color_ramp",
      "numeric",
      "node_group",
      "texture",
      "inferred",
      "advanced",
    ]);
    expect(GRAPH_SECTIONS.map((item) => item.label)).toEqual([
      "常用参数",
      "颜色与色带",
      "数值参数",
      "节点组参数",
      "纹理控制",
      "推测参数",
      "高级影响路径",
    ]);
  });

  it("推测参数永远进「推测参数」区，不随语义散落到别处", () => {
    expect(sectionOf("color", "inferred")).toBe("inferred");
    expect(sectionOf("inferred", "confirmed")).toBe("inferred");
    expect(sectionOf("color", "confirmed")).toBe("color_ramp");
    expect(sectionOf("ramp", "confirmed")).toBe("color_ramp");
  });

  it("不认识的分区归入 advanced，绝不丢掉这个参数", () => {
    const param = parseGraphParam(graphParamFixture("mg-cat", { category: "brand_new" }));
    expect(param?.meta.category).toBe("advanced");
    expect(param?.meta.section).toBe("advanced");
  });

  it("默认夹具覆盖全部七个分区", () => {
    const schema = schemaOf([graphMaterialFixture()]);
    const material = schema.materials[0];
    for (const section of material.sections) {
      expect(section.params.length, `分区 ${section.spec.id} 应有参数`).toBeGreaterThan(0);
    }
    expect(material.sections).toHaveLength(7);
  });
});

describe("材质图 schema 解析 · 值", () => {
  it("标量带当前值 / 基线值 / 生效值与值域", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-num", {
        value: { current: 0.25, baseline: 0.5, effective: 0.25 },
        minimum: 0,
        maximum: 1,
        step: 0.01,
      })
    );
    expect(param?.node.kind).toBe("float");
    const node = param?.node as { value: number; baseline: number; effective: number; minimum: number; maximum: number; step: number };
    expect(node.value).toBe(0.25);
    expect(node.baseline).toBe(0.5);
    expect(node.effective).toBe(0.25);
    expect(node.minimum).toBe(0);
    expect(node.maximum).toBe(1);
    expect(node.step).toBe(0.01);
  });

  it("色带是单一复合节点：色标可写、数量只读", () => {
    const schema = schemaOf([graphMaterialFixture()]);
    const ramp = schema.materials[0].params.find((param) => param.node.kind === "ramp");
    expect(ramp).toBeTruthy();
    const node = ramp?.node as {
      elements: Array<{ position: { value: number }; color: { value: number[] } }>;
      element_count: { value: number; editable: boolean; structural: boolean };
      interpolation: { value: string };
    };
    expect(node.elements).toHaveLength(3);
    expect(node.elements[0].position.value).toBe(0);
    expect(node.element_count.value).toBe(3);
    expect(node.element_count.editable).toBe(false);
    expect(node.element_count.structural).toBe(true);
    expect(node.interpolation.value).toBe("LINEAR");
  });

  it("色标数量即使被后端误标成可编辑，前端也按只读处理", () => {
    const param = parseGraphParam(graphSharedFixture("mg-ramp-ro", { group: "", users: [] }));
    expect(param).toBeTruthy();
    const ramp = parseGraphParam(
      graphParamFixture("mg-ramp2", {
        kind: "ramp",
        elements: [
          { index: 0, position: { value: 0, baseline: 0, minimum: 0, maximum: 1 }, color: { value: [0, 0, 0, 1], baseline: null } },
          { index: 1, position: { value: 1, baseline: 1, minimum: 0, maximum: 1 }, color: { value: [1, 1, 1, 1], baseline: null } },
        ],
        interpolation: "CONSTANT",
        element_count: 2,
      })
    );
    const node = ramp?.node as { element_count: { editable: boolean; readonly_reason: string }; interpolation: { value: string } };
    expect(node.element_count.editable).toBe(false);
    expect(node.element_count.readonly_reason).toBe("structural");
    expect(node.interpolation.value).toBe("CONSTANT");
  });

  it("interpolation / element_count 是复合形状时必须读进一层（不是裸标量）", () => {
    // 后端 `to_spec` 直接把 `RampNode.to_public()` 的值搬过来：
    // `interpolation` = {value, baseline, options}，`element_count` = {value, baseline, …}。
    // 少读这一层会静默退化成 "LINEAR" / 元素个数，而色带的插值显示就永远是错的。
    const param = parseGraphParam(
      graphRampFixture("mg-ramp-composite", { interpolation: "CONSTANT" })
    );
    const node = param?.node as {
      interpolation: { value: string };
      element_count: { value: number };
    };
    expect(node.interpolation.value).toBe("CONSTANT");
    expect(node.element_count.value).toBe(3);

    // 形状读不懂时：插值回落到 LINEAR，数量回落到实际色标数（不编一个数）
    const broken = parseGraphParam(
      graphParamFixture("mg-ramp-broken", {
        kind: "ramp",
        elements: [
          { index: 0, position: { value: 0, baseline: 0, minimum: 0, maximum: 1 }, color: { value: [0, 0, 0, 1], baseline: null } },
          { index: 1, position: { value: 1, baseline: 1, minimum: 0, maximum: 1 }, color: { value: [1, 1, 1, 1], baseline: null } },
        ],
        interpolation: null,
        element_count: { nonsense: true },
      })
    );
    const brokenNode = broken?.node as {
      interpolation: { value: string };
      element_count: { value: number };
    };
    expect(brokenNode.interpolation.value).toBe("LINEAR");
    expect(brokenNode.element_count.value).toBe(2);
  });

  it("读不懂的值不会变成「看起来合理」的取值", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-bad", { kind: "float", value: { current: "half", baseline: null, effective: null } })
    );
    expect((param?.node as { value: unknown }).value).toBeNull();
  });

  it("回滚能力按「会不会产生写入」如实描述", () => {
    expect(rollbackNote(true, true)).toContain("可回滚");
    expect(rollbackNote(true, false)).toContain("无需回滚");
    expect(rollbackNote(false, false)).toContain("不会产生任何写入");
  });

  it("来源节点与插座：label 里的插座名解析出来，否则回落到字段名", () => {
    const withSocket = parseGraphParam(
      graphParamFixture("mg-s1", { label: "Principled BSDF · Roughness" })
    );
    expect(withSocket && socketLabelOf(withSocket)).toBe("Roughness");
    const noSocket = parseGraphParam(
      graphParamFixture("mg-s2", { label: "ColorRamp 色带", binding: { object_type: "MATERIAL_GRAPH_RAMP", field: "elements" } })
    );
    expect(noSocket && socketLabelOf(noSocket)).toBe("elements");
  });
});

describe("材质图 schema 解析 · 影响范围与共享组", () => {
  it("受影响对象 / 材质来自服务端，前端不反推", () => {
    const param = parseGraphParam(
      graphParamFixture("mg-impact", { affected_materials: ["model_mesh", "model_hair"] })
    );
    expect(param?.meta.affectedMaterials).toEqual(["model_mesh", "model_hair"]);
  });

  it("共享组名逐字取服务端的 in_group —— 那是确认键", () => {
    const schema = schemaOf([
      graphMaterialFixture({
        parameters: [graphSharedFixture("mg-shared", { group: "Cel_Shared" })],
        sharedGroups: { Cel_Shared: ["皮肤_脸", "头发_前"] },
      }),
    ]);
    const material = schema.materials[0];
    const param = material.params[0];
    expect(param.meta.shared).toBe(true);
    expect(sharedGroupOf(param, material)).toBe("Cel_Shared");
  });

  it("未确认的共享组：只在草稿真的写了它的时候才要求确认", () => {
    const schema = schemaOf([
      graphMaterialFixture({
        parameters: [graphSharedFixture("mg-shared", { group: "Cel_Shared" })],
        sharedGroups: { Cel_Shared: ["皮肤_脸", "头发_前"] },
      }),
    ]);
    expect(unconfirmedSharedGroupsInSchema(schema, {}, [])).toEqual([]);
    const pending = unconfirmedSharedGroupsInSchema(schema, { "mg-shared": 1.0 }, []);
    expect(pending).toHaveLength(1);
    expect(pending[0].sharedGroup).toBe("Cel_Shared");
    expect(pending[0].affectedMaterials).toEqual(["皮肤_脸", "头发_前"]);
    // 已经确认过的不再要求
    expect(unconfirmedSharedGroupsInSchema(schema, { "mg-shared": 1.0 }, ["Cel_Shared"])).toEqual([]);
  });

  it("非共享参数的草稿不会触发共享确认", () => {
    const schema = schemaOf([graphMaterialFixture()]);
    const ids = writableGraphParams(schema.materials[0]).map((param) => param.node.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(unconfirmedSharedGroupsInSchema(schema, Object.fromEntries(ids.map((id) => [id, 1])), [])).toEqual([]);
  });

  it("服务端的确认 details 形状不对时返回 null，绝不编影响范围", () => {
    expect(confirmRequestFromDetails({ affected_materials: ["a"] })).toBeNull();
    expect(confirmRequestFromDetails(null)).toBeNull();
    expect(confirmRequestFromDetails({ shared_group: "G", parameter: "mg-1", affected_materials: ["a", "b"] })).toEqual({
      sharedGroup: "G",
      parameter: "mg-1",
      affectedMaterials: ["a", "b"],
    });
  });
});

describe("材质图草稿拆分", () => {
  it("按索引把 id 分成参数面与材质图两半，两边的 id 不会串门", () => {
    const schema = schemaOf([graphMaterialFixture()]);
    const graphId = schema.materials[0].params[0].node.id;
    const split = splitGraphDraft(schema, {
      [graphId]: 1,
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" },
    });
    expect(Object.keys(split.graph)).toEqual([graphId]);
    expect(Object.keys(split.surface)).toEqual(["cel.Cel_Skin.ramp"]);
  });

  it("没有材质图 schema 时全部归参数面（不把未知 id 当成材质图）", () => {
    const split = splitGraphDraft(null, { "cel.Cel_Skin.ramp": 1 });
    expect(Object.keys(split.surface)).toEqual(["cel.Cel_Skin.ramp"]);
    expect(split.graph).toEqual({});
  });
});

describe("材质图参数筛选", () => {
  const schema = schemaOf([
    graphMaterialFixture({
      parameters: [
        ...defaultGraphParameters(),
        graphSharedFixture("mg-shared2", { group: "Cel_Shared", users: ["皮肤_脸", "头发_前"] }),
      ],
      sharedGroups: { Cel_Shared: ["皮肤_脸", "头发_前"] },
    }),
  ]);
  const params = schema.materials[0].params;

  it("空筛选放行全部", () => {
    expect(params.filter((param) => matchesGraphFilter(param, emptyGraphFilter(), new Set())).length).toBe(
      params.length
    );
  });

  it("搜索命中来源节点名、语义与影响对象", () => {
    const bySource = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), query: "ColorRamp" }, new Set())
    );
    expect(bySource.length).toBeGreaterThan(0);
    const bySemantic = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), query: "色带" }, new Set())
    );
    expect(bySemantic.length).toBeGreaterThan(0);
    const byImpact = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), query: "model_mesh" }, new Set())
    );
    expect(byImpact.length).toBeGreaterThan(0);
  });

  it("等级筛选只留该等级", () => {
    const inferred = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), confidence: "inferred" }, new Set())
    );
    expect(inferred.length).toBeGreaterThan(0);
    expect(inferred.every((param) => param.meta.confidence === "inferred")).toBe(true);
  });

  it("「仅已修改」按草稿判定，「仅共享」按 shared 判定", () => {
    const target = params[0].node.id;
    const dirty = new Set([target]);
    const modified = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), onlyModified: true }, dirty)
    );
    expect(modified.map((param) => param.node.id)).toEqual([target]);

    const shared = params.filter((param) =>
      matchesGraphFilter(param, { ...emptyGraphFilter(), onlyShared: true }, new Set())
    );
    expect(shared.every((param) => param.meta.shared)).toBe(true);
    expect(shared.length).toBe(1);
  });
});

describe("材质图 schema 解析 · 不可用材质", () => {
  it("材质图不可用时如实报告，不伪造参数", () => {
    const schema = schemaOf([
      graphMaterialFixture({ available: false, error: "该材质没有可用的节点图。" }),
    ]);
    const material = schema.materials[0];
    expect(material.available).toBe(false);
    expect(material.params).toEqual([]);
    expect(material.error).toContain("没有可用的节点图");
  });

  it("降级原因照实带出来（整份只读的依据）", () => {
    const schema = schemaOf([
      graphMaterialFixture({
        degradedReason: "ambiguous_entry",
        parameters: [graphUnsupportedFixture("mg-u")],
      }),
    ]);
    expect(schema.materials[0].degradedReason).toBe("ambiguous_entry");
    expect(schema.materials[0].params[0].node.editable).toBe(false);
  });

  it("schema 形状完全对不上时给出空壳，而不是抛错", () => {
    const schema = parseMaterialGraphSchema({ nonsense: true });
    expect(schema.materials).toEqual([]);
    expect(schema.structureHash).toBeNull();
    expect(schema.index.size).toBe(0);
  });

  it("推测参数的等级与只读原因同时保留", () => {
    const schema = schemaOf([
      graphMaterialFixture({ parameters: [graphInferredFixture("mg-i")] }),
    ]);
    const param = schema.materials[0].params[0];
    expect(param.meta.confidence).toBe("inferred");
    expect(param.meta.section).toBe("inferred");
    expect(param.node.editable).toBe(false);
    expect(param.node.readonly_reason).toBe("inferred_not_confirmed");
  });
});
