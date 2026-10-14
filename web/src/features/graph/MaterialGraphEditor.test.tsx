import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MaterialGraphEditor } from "./MaterialGraphEditor";
import { makeTestWorkspace, settle, settleTimers } from "../../testing/endpoints";
import {
  defaultGraphParameters,
  graphInferredFixture,
  graphMaterialFixture,
  graphParamFixture,
  graphRampFixture,
  graphSchemaPayload,
  graphSharedFixture,
  materialNode,
  schemaPayload,
} from "../../testing/fixtures";
import { AppError } from "../../api/types";

afterEach(cleanup);

const MATERIAL = "皮肤_脸";
const OTHER = "头发_前";
const SHARED_PARAM = "mg-shared01";

function surfaceFixture() {
  return schemaPayload({
    groups: [materialNode({ material: MATERIAL }), materialNode({ material: OTHER })],
  });
}

interface SetupOptions {
  materials?: unknown[];
  /** 便于在用例里替换 submitPreview / job。 */
  overrides?: Record<string, unknown>;
}

async function setup(options: SetupOptions = {}) {
  const materials =
    options.materials ??
    [
      graphMaterialFixture({
        material: MATERIAL,
        parameters: [
          graphSharedFixture(SHARED_PARAM, { group: "Cel_Shared", users: [MATERIAL, OTHER] }),
          ...defaultGraphParameters(),
        ],
        sharedGroups: { Cel_Shared: [MATERIAL, OTHER] },
      }),
      graphMaterialFixture({ material: OTHER, parameters: [graphParamFixture("mg-other01")] }),
    ];
  const { workspace, endpoints } = makeTestWorkspace({
    surfaceSchema: vi.fn(async () => surfaceFixture()),
    materialGraphSchema: vi.fn(async () => graphSchemaPayload({ materials })),
    ...(options.overrides ?? {}),
  });
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

function draftOf(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]) {
  return workspace.store.getState().draft;
}

describe("材质图编辑器 · 分区与等级", () => {
  it("按七个固定分区渲染，并显示可调项数", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );

    expect(getByTestId(`graph-editor-${MATERIAL}`)).toBeTruthy();
    for (const id of [
      "common",
      "color_ramp",
      "numeric",
      "node_group",
      "texture",
      "inferred",
      "advanced",
    ]) {
      expect(getByTestId(`graph-section-${id}`)).toBeTruthy();
    }
    // 默认只展开「常用参数」：一上来摊开七个分区等于把「页面过长」搬回界面
    expect(getByTestId("graph-section-common").getAttribute("data-open")).toBe("1");
    expect(getByTestId("graph-section-numeric").getAttribute("data-open")).toBe("0");
    expect(queryByTestId(`graph-param-${SHARED_PARAM}`)).toBeNull();
  });

  it("推测参数带「推测」芯片，绝不显示成「已确认」", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );

    fireEvent.click(getByTestId("graph-section-toggle-inferred"));

    const badge = getByTestId("graph-param-confidence-mg-inferred01");
    expect(badge.textContent).toBe("推测");
    expect(badge.getAttribute("data-confidence")).toBe("inferred");
    expect(queryByTestId("graph-param-confidence-mg-inferred01")?.textContent).not.toBe("已确认");
    // 推测项一定不可写
    expect(getByTestId("graph-param-mg-inferred01").getAttribute("data-editable")).toBe("0");
    expect(getByTestId("graph-param-readonly-mg-inferred01")).toBeTruthy();
    expect(getByTestId("graph-readonly-reason-mg-inferred01").textContent).toContain("推测语义");
  });

  it("五个等级各有自己的芯片文案", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-numeric"));
    fireEvent.click(getByTestId("graph-section-toggle-inferred"));
    fireEvent.click(getByTestId("graph-section-toggle-advanced"));

    expect(getByTestId("graph-param-confidence-mg-numeric01").textContent).toBe("已确认");
    expect(getByTestId("graph-param-confidence-mg-inferred01").textContent).toBe("推测");
    expect(getByTestId("graph-param-confidence-mg-advanced01").textContent).toBe("实验性");
    expect(getByTestId("graph-param-confidence-mg-advanced02").textContent).toBe("不支持");
  });

  it("每个参数显示当前值 / 基线值 / 草稿值 / 生效值四列与来源节点、插座、回滚能力", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    const id = "mg-common02";
    expect(getByTestId(`graph-current-${id}`).textContent).toBe("0.5000");
    expect(getByTestId(`graph-baseline-${id}`).textContent).toBe("0.5000");
    expect(getByTestId(`graph-draft-${id}`).textContent).toBe("未改动");
    expect(getByTestId(`graph-effective-${id}`).textContent).toBe("未确认");
    expect(getByTestId(`graph-source-${id}`).textContent).toContain("Principled BSDF");
    expect(getByTestId(`graph-source-${id}`).textContent).toContain("Roughness");
    expect(getByTestId(`graph-rollback-${id}`).textContent).toContain("可回滚");
    expect(getByTestId(`graph-impact-${id}`).textContent).toContain("model_mesh");
    expect(getByTestId(`graph-shared-group-${id}`).textContent).toContain("否");
  });

  it("只读项不画控件，只给只读输出与原因", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-texture"));
    const id = "mg-texture01";
    expect(queryByTestId(`graph-number-${id}`)).toBeNull();
    expect(queryByTestId(`graph-color-${id}`)).toBeNull();
    expect(getByTestId(`graph-value-output-${id}`)).toBeTruthy();
    expect(getByTestId(`graph-readonly-reason-${id}`).textContent).toContain("没有探到可编辑的输入插座");
  });
});

describe("材质图编辑器 · 折叠与状态保留", () => {
  it("分区可折叠，展开状态按材质分别保留", async () => {
    const { workspace } = await setup();
    const { getByTestId, rerender } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );

    fireEvent.click(getByTestId("graph-section-toggle-numeric"));
    expect(getByTestId("graph-section-numeric").getAttribute("data-open")).toBe("1");
    fireEvent.click(getByTestId(`graph-collapse-all-${MATERIAL}`));
    expect(getByTestId("graph-section-numeric").getAttribute("data-open")).toBe("0");

    // 切到另一个材质：它的默认视图只有「常用参数」展开
    rerender(<MaterialGraphEditor workspace={workspace} materialName={OTHER} />);
    expect(getByTestId("graph-section-common").getAttribute("data-open")).toBe("1");
    expect(getByTestId("graph-section-numeric").getAttribute("data-open")).toBe("0");

    // 切回来：之前「折叠全部」的状态还在
    rerender(<MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />);
    expect(getByTestId("graph-section-common").getAttribute("data-open")).toBe("0");
    expect(getByTestId("graph-section-numeric").getAttribute("data-open")).toBe("0");
  });

  it("搜索词与筛选也按材质保留", async () => {
    const { workspace } = await setup();
    const { getByTestId, rerender } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.input(getByTestId(`graph-search-${MATERIAL}`), { target: { value: "ColorRamp" } });
    expect(getByTestId("graph-section-common").getAttribute("data-shown")).toBe("0");

    rerender(<MaterialGraphEditor workspace={workspace} materialName={OTHER} />);
    expect((getByTestId(`graph-search-${OTHER}`) as HTMLInputElement).value).toBe("");

    rerender(<MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />);
    expect((getByTestId(`graph-search-${MATERIAL}`) as HTMLInputElement).value).toBe("ColorRamp");
  });
});

describe("材质图编辑器 · 搜索与筛选", () => {
  it("按来源节点搜索，命中分区显示筛选后的数量", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.input(getByTestId(`graph-search-${MATERIAL}`), { target: { value: "RGB" } });
    fireEvent.click(getByTestId("graph-section-toggle-color_ramp"));

    expect(getByTestId("graph-section-color_ramp").getAttribute("data-shown")).toBe("1");
    expect(getByTestId("graph-param-mg-color01")).toBeTruthy();
    // 颜色分区原本有 2 项（RGB + 色带），被筛掉 1 项
    expect(getByTestId("graph-section-count-color_ramp").textContent).toContain("1 / 2");
  });

  it("按识别等级筛选", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId(`graph-filter-confidence-inferred-${MATERIAL}`));
    expect(getByTestId("graph-section-inferred").getAttribute("data-shown")).toBe("1");
    expect(getByTestId("graph-section-common").getAttribute("data-shown")).toBe("0");
  });

  it("「仅已修改」与「仅共享节点组」", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    // 共享参数在 numeric 区（与 mg-common02 并列）
    fireEvent.click(getByTestId("graph-section-toggle-numeric"));
    workspace.actions.setValue("mg-numeric01", 5);
    await settle();

    fireEvent.click(getByTestId(`graph-filter-modified-${MATERIAL}`));
    expect(getByTestId("graph-param-mg-numeric01")).toBeTruthy();
    expect(queryByTestId("graph-param-mg-common02")).toBeNull();

    fireEvent.click(getByTestId(`graph-filter-modified-${MATERIAL}`));
    fireEvent.click(getByTestId(`graph-filter-shared-${MATERIAL}`));
    expect(getByTestId(`graph-param-${SHARED_PARAM}`)).toBeTruthy();
    expect(queryByTestId("graph-param-mg-numeric01")).toBeNull();
  });

  it("长列表走虚拟列表，不同时把几百个控件挂进 DOM", async () => {
    const many = Array.from({ length: 60 }, (_, index) =>
      graphParamFixture(`mg-bulk${String(index).padStart(3, "0")}`, {
        label: `Bulk ${index}`,
        semantic: "math",
        semantic_label: "数值运算",
        category: "numeric",
        value: { current: index, baseline: index, effective: index },
        minimum: 0,
        maximum: 100,
      })
    );
    const { workspace } = await setup({
      materials: [graphMaterialFixture({ material: MATERIAL, parameters: many })],
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-expand-all-" + MATERIAL));

    const list = getByTestId("graph-list-numeric");
    expect(list.getAttribute("data-total")).toBe("60");
    expect(Number(list.getAttribute("data-rendered"))).toBeLessThan(60);
    expect(list.getAttribute("data-mode")).toBe("window");
  });
});

describe("材质图编辑器 · 编辑参数", () => {
  it("改颜色写回 RGBA，并保留原有 alpha", async () => {
    const { workspace } = await setup({
      materials: [
        graphMaterialFixture({
          material: MATERIAL,
          parameters: [
            graphParamFixture("mg-color", {
              label: "RGB · Color",
              kind: "color",
              semantic: "color",
              category: "color",
              value: { current: [0.5, 0.5, 0.5, 0.4], baseline: [0.5, 0.5, 0.5, 0.4], effective: [0.5, 0.5, 0.5, 0.4] },
            }),
          ],
        }),
      ],
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-color_ramp"));
    const picker = getByTestId("graph-color-mg-color") as HTMLInputElement;
    expect(picker.value).toBe("#808080");

    fireEvent.input(picker, { target: { value: "#ff8000" } });
    const written = draftOf(workspace)["mg-color"] as number[];
    expect(written.slice(0, 3)).toEqual([1, 128 / 255, 0]);
    expect(written[3]).toBeCloseTo(0.4);
    // L1：编辑不触发渲染
    expect(workspace.store.getState().previewStale).toBe(false);
  });

  it("数字输入写草稿；有值域时给滑块，没值域时只给精确输入", async () => {
    const { workspace } = await setup({
      materials: [
        graphMaterialFixture({
          material: MATERIAL,
          parameters: [
            graphParamFixture("mg-bounded", { minimum: 0, maximum: 1, step: 0.01 }),
            graphParamFixture("mg-unbounded", { minimum: null, maximum: null }),
          ],
        }),
      ],
    });
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-numeric"));

    const input = getByTestId("graph-number-mg-bounded") as HTMLInputElement;
    fireEvent.input(input, { target: { value: "0.25" } });
    expect(draftOf(workspace)["mg-bounded"]).toBe(0.25);

    fireEvent.input(getByTestId("graph-slider-mg-bounded"), { target: { value: "0.75" } });
    expect(draftOf(workspace)["mg-bounded"]).toBe(0.75);

    expect(queryByTestId("graph-slider-mg-unbounded")).toBeNull();
    expect(getByTestId("graph-no-slider-mg-unbounded")).toBeTruthy();
  });

  it("色带：位置与颜色都写进这**一个** opaque 参数 id，且可撤销", async () => {
    const { workspace } = await setup({
      materials: [
        graphMaterialFixture({
          material: MATERIAL,
          parameters: [graphRampFixture("mg-ramp")],
        }),
      ],
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-color_ramp"));

    // 中间色标的位置可拖（端点固定在 0 / 1）
    fireEvent.input(getByTestId("ramp-position-mg-ramp-1"), { target: { value: "0.3" } });
    let value = draftOf(workspace)["mg-ramp"] as {
      elements: Array<{ position: number; color: number[] }>;
    };
    expect(value.elements[1].position).toBeCloseTo(0.3);

    fireEvent.blur(getByTestId("ramp-position-mg-ramp-1"));
    fireEvent.input(getByTestId("ramp-color-mg-ramp-1"), { target: { value: "#123456" } });
    value = draftOf(workspace)["mg-ramp"] as {
      elements: Array<{ position: number; color: number[] }>;
    };
    expect(value.elements[1].color[0]).toBeCloseTo(0x12 / 255);
    // 写的是完整色带（服务端整体替换才原子）
    expect(value.elements).toHaveLength(3);

    // 色标数量与插值都是结构信息：只读展示，不提供增删与改写
    expect(getByTestId("graph-element-count-mg-ramp").textContent).toContain("3");
    expect(getByTestId("graph-interpolation-mg-ramp").textContent).toBe("LINEAR");
    expect(getByTestId("graph-interpolation-mg-ramp").tagName).toBe("OUTPUT");

    // 端点固定在 0 / 1，不给拖
    expect((getByTestId("ramp-handle-mg-ramp-0") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("ramp-handle-mg-ramp-2") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("ramp-handle-mg-ramp-1") as HTMLButtonElement).disabled).toBe(false);

    // 失焦封存过历史，因此位置与颜色是**两条**独立的撤销记录
    workspace.actions.undo();
    const afterColorUndo = draftOf(workspace)["mg-ramp"] as {
      elements: Array<{ position: number; color: number[] }>;
    };
    expect(afterColorUndo.elements[1].color[0]).toBeCloseTo(0.55);
    expect(afterColorUndo.elements[1].position).toBeCloseTo(0.3);

    workspace.actions.undo();
    expect("mg-ramp" in draftOf(workspace)).toBe(false);
  });

  it("单项复位只清这一项", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    workspace.actions.setValue("mg-common01", [1, 1, 1, 1]);
    workspace.actions.setValue("mg-common02", 0.9);
    await settle();

    fireEvent.click(getByTestId("graph-reset-mg-common02"));
    expect("mg-common02" in draftOf(workspace)).toBe(false);
    expect(draftOf(workspace)["mg-common01"]).toEqual([1, 1, 1, 1]);
  });

  it("材质分组复位只清该材质，不碰别的材质与 Cel", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    workspace.actions.setValue("mg-common02", 0.9);
    workspace.actions.setValue("mg-other01", 1.5);
    workspace.actions.setValue("cel.Cel_Skin.managed_mode", "simple");
    await settle();

    fireEvent.click(getByTestId(`graph-reset-material-${MATERIAL}`));
    const draft = draftOf(workspace);
    expect("mg-common02" in draft).toBe(false);
    expect(draft["mg-other01"]).toBe(1.5);
    expect(draft["cel.Cel_Skin.managed_mode"]).toBe("simple");
  });

  it("撤销与重做覆盖材质图改动", async () => {
    const { workspace } = await setup();
    render(<MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />);
    workspace.actions.setValue("mg-common02", 0.9);
    await settle();
    expect(workspace.store.getState().canUndo).toBe(true);

    workspace.actions.undo();
    expect("mg-common02" in draftOf(workspace)).toBe(false);
    expect(workspace.store.getState().canRedo).toBe(true);

    workspace.actions.redo();
    expect(draftOf(workspace)["mg-common02"]).toBe(0.9);
  });

  it("结构失效时禁用编辑与复位", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    workspace.actions.setValue("mg-common02", 0.9);
    await settle();

    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "材质节点图结构已变化",
      retryable: true,
    });
    await settle();

    expect((getByTestId("graph-reset-mg-common02") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId(`graph-reset-material-${MATERIAL}`) as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId(`graph-isolate-${MATERIAL}`) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("材质图编辑器 · 隔离预览", () => {
  it("「隔离该材质预览」提交带 isolate_material 的渲染，再点一次取消", async () => {
    const { workspace, endpoints } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );

    fireEvent.click(getByTestId(`graph-isolate-${MATERIAL}`));
    await settleTimers();
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    expect((endpoints.submitPreview.mock.calls[0][0] as { framing: unknown }).framing).toEqual({
      mode: "current_camera",
      margin: 0.15,
      isolate_material: MATERIAL,
    });
    expect(workspace.store.getState().framingChoice.isolateMaterial).toBe(MATERIAL);

    fireEvent.click(getByTestId(`graph-isolate-${MATERIAL}`));
    await settleTimers();
    expect((endpoints.submitPreview.mock.calls[1][0] as { framing: unknown }).framing).toEqual({
      mode: "current_camera",
      margin: 0.15,
    });
  });
});

describe("材质图编辑器 · 共享节点组", () => {
  it("共享参数行标出共享组，说明会一起改到谁", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-numeric"));

    expect(getByTestId(`graph-param-shared-${SHARED_PARAM}`)).toBeTruthy();
    expect(getByTestId(`graph-shared-group-${SHARED_PARAM}`).textContent).toContain("Cel_Shared");
    expect(getByTestId(`graph-impact-${SHARED_PARAM}`).textContent).toContain("会一起改到的材质");
    expect(getByTestId(`graph-impact-${SHARED_PARAM}`).textContent).toContain(OTHER);
  });

  it("确认面板列出组名与受影响材质，确认后进入已确认名单", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    fireEvent.click(getByTestId("graph-section-toggle-numeric"));
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    await settle();
    await workspace.actions.applyAndPreview();
    await settle();

    expect(queryByTestId("shared-confirm-panel")).toBeTruthy();
    expect(getByTestId("shared-confirm-group").textContent).toContain("Cel_Shared");
    const affected = getByTestId("shared-confirm-affected").textContent ?? "";
    expect(affected).toContain(MATERIAL);
    expect(affected).toContain(OTHER);
    // 确认按钮把后果写在文字里，而不是一个中性的「确定」
    expect(getByTestId("shared-confirm-accept").textContent).toContain("我已了解影响范围");

    fireEvent.click(getByTestId("shared-confirm-accept"));
    await settleTimers();
    expect(workspace.store.getState().graph.confirmedSharedGroups).toEqual(["Cel_Shared"]);
    expect(getByTestId("shared-confirmed-Cel_Shared")).toBeTruthy();
  });

  it("取消确认会撤销该组草稿并给出说明", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    await settle();
    await workspace.actions.applyAndPreview();
    await settle();

    fireEvent.click(getByTestId("shared-confirm-cancel"));
    await settle();
    expect(SHARED_PARAM in draftOf(workspace)).toBe(false);
    expect(workspace.store.getState().notice).toContain("已取消");
  });
});

describe("材质图编辑器 · 不可用的情形", () => {
  it("没有材质图基线时提示先刷新基线，而不是显示一个空列表", async () => {
    const { workspace } = await setup({
      overrides: {
        materialGraphSchema: vi.fn(async () => {
          throw new AppError(
            { code: "NO_BASELINE", message: "尚未建立材质图基线。", retryable: true },
            409
          );
        }),
      },
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    expect(getByTestId("graph-editor-unavailable")).toBeTruthy();
    expect(getByTestId("graph-unavailable-reason").textContent).toContain("刷新基线");
  });

  it("schema 里没有该材质时给出明确说明，而不是渲染半套界面", async () => {
    const { workspace } = await setup({
      materials: [graphMaterialFixture({ material: "别的材质", parameters: [graphInferredFixture("mg-x")] })],
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    expect(getByTestId(`graph-editor-missing-${MATERIAL}`)).toBeTruthy();
    expect(getByTestId("graph-missing-reason").textContent).toContain(MATERIAL);
  });

  it("材质图不可用的材质如实说明", async () => {
    const { workspace } = await setup({
      materials: [
        graphMaterialFixture({ material: MATERIAL, available: false, error: "未启用节点。" }),
      ],
    });
    const { getByTestId } = render(
      <MaterialGraphEditor workspace={workspace} materialName={MATERIAL} />
    );
    expect(getByTestId(`graph-editor-${MATERIAL}`)).toBeTruthy();
    expect(getByTestId(`graph-summary-${MATERIAL}`)).toBeTruthy();
    for (const id of ["common", "numeric", "inferred"]) {
      expect(getByTestId(`graph-section-${id}`).getAttribute("data-total")).toBe("0");
    }
  });
});
