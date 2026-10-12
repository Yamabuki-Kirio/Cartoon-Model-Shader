import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { MaterialEditor } from "./MaterialEditor";
import { makeTestWorkspace, settle } from "../../testing/endpoints";
import { materialNode, jobPayload, schemaPayload } from "../../testing/fixtures";

afterEach(cleanup);

const MATERIAL = "皮肤_脸";
const GROUP = `material.${MATERIAL}`;

async function setup(groups?: unknown[], submitImpl?: () => unknown) {
  const { workspace, endpoints } = makeTestWorkspace({
    ...(groups ? { surfaceSchema: async () => schemaPayload({ groups }) } : {}),
    ...(submitImpl ? { submitPreview: submitImpl as never } : {}),
  });
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

function draftOf(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]) {
  return workspace.store.getState().draft;
}

describe("逐材质编辑器", () => {
  it("展示分类、目标 Cel 组、使用对象与影响范围", async () => {
    const { workspace } = await setup([
      materialNode({
        material: MATERIAL,
        classification: "skin",
        classificationLabel: "皮肤",
        targetGroup: "Cel_Skin",
        users: ["model_mesh"],
        nodeCount: 4,
        blendMethod: "OPAQUE",
      }),
    ]);
    const { getByTestId, getByText } = render(
      <MaterialEditor workspace={workspace} groupId={GROUP} />
    );

    expect(getByTestId(`material-editor-${MATERIAL}`)).toBeTruthy();
    expect(getByTestId(`material-class-${MATERIAL}`).textContent).toContain("皮肤");
    expect(getByTestId(`material-class-${MATERIAL}`).textContent).toContain("skin");
    expect(getByTestId(`material-target-${MATERIAL}`).textContent).toContain("Cel_Skin");
    expect(getByTestId(`material-users-${MATERIAL}`).textContent).toContain("model_mesh");
    const impact = getByTestId(`material-impact-${MATERIAL}`).textContent ?? "";
    expect(impact).toContain("1 个对象");
    expect(impact).toContain("4 个节点");
    expect(impact).toContain("OPAQUE");
    // 6 项全部可写
    expect(getByTestId(`material-count-${MATERIAL}`).textContent).toContain("6 / 6");
    expect(getByText("使用对象")).toBeTruthy();
  });

  it("只画 schema 确认可写的控件；只读项显示值与原因", async () => {
    const { workspace } = await setup([
      materialNode({
        material: MATERIAL,
        params: {
          roughness: {
            editable: false,
            readonlyReason: "structural",
            reason: "该材质的 Roughness 插座已被上游连线。",
          },
          specular: { supported: false, editable: false, readonlyReason: "not_found" },
        },
      }),
    ]);
    const { getByTestId, queryByTestId } = render(
      <MaterialEditor workspace={workspace} groupId={GROUP} />
    );

    // 可写项：标量给数字框、颜色给取色器
    expect(getByTestId(`material-param-number-material.${MATERIAL}.metallic`)).toBeTruthy();
    expect(getByTestId(`material-param-color-material.${MATERIAL}.base_color`)).toBeTruthy();
    // 只读项：没有控件，只有只读输出
    expect(queryByTestId(`material-param-number-material.${MATERIAL}.roughness`)).toBeNull();
    expect(getByTestId(`material-param-readonly-material.${MATERIAL}.roughness`).textContent).toContain(
      "0.5000"
    );
    expect(queryByTestId(`material-param-number-material.${MATERIAL}.specular`)).toBeNull();
    expect(getByTestId(`material-param-readonly-material.${MATERIAL}.specular`)).toBeTruthy();
    expect(getByTestId(`material-count-${MATERIAL}`).textContent).toContain("4 / 6");
  });

  it("改标量只进草稿（L1：不触发渲染），连续输入合并成一条撤销", async () => {
    const { workspace, endpoints } = await setup([materialNode({ material: MATERIAL })]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);
    const input = getByTestId(
      `material-param-number-material.${MATERIAL}.roughness`
    ) as HTMLInputElement;

    fireEvent.input(input, { target: { value: "0.25" } });
    fireEvent.input(input, { target: { value: "0.75" } });
    expect(draftOf(workspace)[`material.${MATERIAL}.roughness`]).toBe(0.75);
    expect(workspace.store.getState().undoDepth).toBe(1);
    // L1：编辑本身绝不发请求
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    fireEvent.blur(input);
    fireEvent.input(input, { target: { value: "0.9" } });
    expect(workspace.store.getState().undoDepth).toBe(2);

    workspace.actions.undo();
    expect(draftOf(workspace)[`material.${MATERIAL}.roughness`]).toBe(0.75);
  });

  it("改颜色写回 RGBA，并保留原有 alpha", async () => {
    const { workspace } = await setup([
      materialNode({
        material: MATERIAL,
        params: { base_color: { value: [0.5, 0.5, 0.5, 0.4] } },
      }),
    ]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);
    const picker = getByTestId(
      `material-param-color-material.${MATERIAL}.base_color`
    ) as HTMLInputElement;

    expect(picker.value).toBe("#808080");
    fireEvent.input(picker, { target: { value: "#ff8000" } });

    const written = draftOf(workspace)[`material.${MATERIAL}.base_color`] as number[];
    expect(written.slice(0, 3)).toEqual([1, 128 / 255, 0]);
    // alpha 不能被悄悄改成 1
    expect(written[3]).toBeCloseTo(0.4);
  });

  it("「隔离显示该材质」提交带 isolate_material 的预览，再点一次取消隔离", async () => {
    const { workspace, endpoints } = await setup([materialNode({ material: MATERIAL })]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);

    fireEvent.click(getByTestId(`material-isolate-${MATERIAL}`));
    await settle();
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    expect(endpoints.submitPreview.mock.calls[0][0].framing).toEqual({
      mode: "current_camera",
      margin: 0.15,
      isolate_material: MATERIAL,
    });
    expect(workspace.store.getState().framingChoice.isolateMaterial).toBe(MATERIAL);

    fireEvent.click(getByTestId(`material-isolate-${MATERIAL}`));
    await settle();
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(2);
    // 取消隔离时不带该字段（服务端 framing 契约是 extra=forbid，少发比发 null 稳）
    expect(endpoints.submitPreview.mock.calls[1][0].framing).toEqual({
      mode: "current_camera",
      margin: 0.15,
    });
    expect(workspace.store.getState().framingChoice.isolateMaterial).toBeNull();
  });

  it("隔离只影响渲染：草稿保持不变", async () => {
    const { workspace } = await setup([materialNode({ material: MATERIAL })]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);

    fireEvent.input(
      getByTestId(`material-param-number-material.${MATERIAL}.metallic`) as HTMLInputElement,
      { target: { value: "0.6" } }
    );
    const before = { ...draftOf(workspace) };
    fireEvent.click(getByTestId(`material-isolate-${MATERIAL}`));
    await settle();

    expect(draftOf(workspace)).toEqual(before);
  });

  it("结构失效时禁用编辑与隔离（不发送任何请求）", async () => {
    const { workspace, endpoints } = await setup([materialNode({ material: MATERIAL })]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);

    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "结构已变化",
      retryable: true,
    });
    await settle();

    expect((getByTestId(`material-isolate-${MATERIAL}`) as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId(`material-reset-${MATERIAL}`) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(getByTestId(`material-isolate-${MATERIAL}`));
    await settle();
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("非法材质名（整体降级只读）显示原因，且不画任何控件", async () => {
    const { workspace } = await setup([
      materialNode({
        material: "皮肤_脸",
        editable: false,
        params: Object.fromEntries(
          [
            "base_color",
            "metallic",
            "roughness",
            "emission_color",
            "emission_strength",
            "specular",
          ].map((field) => [
            field,
            {
              editable: false,
              readonlyReason: "structural",
              reason: "材质名含路径分隔符或引号，无法安全地拼进参数身份；该材质整体降级为只读。",
            },
          ])
        ),
      }),
    ]);
    const { getByTestId, queryByTestId } = render(
      <MaterialEditor workspace={workspace} groupId={GROUP} />
    );

    expect(getByTestId("material-readonly-reason").textContent).toContain("只读");
    expect(queryByTestId(`material-param-color-material.${MATERIAL}.base_color`)).toBeNull();
    expect(getByTestId(`material-count-${MATERIAL}`).textContent).toContain("0 / 6");
  });

  it("分组不是材质分组时给出明确说明，而不是渲染半套界面", async () => {
    const { workspace } = await setup([materialNode({ material: MATERIAL })]);
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    expect(getByTestId("material-missing")).toBeTruthy();
  });

  it("渲染上一次隔离结果回执（隐藏了谁 / 有没有写回）", async () => {
    const groups = [materialNode({ material: MATERIAL })];
    const { workspace: plain } = await setup(groups);
    const first = render(<MaterialEditor workspace={plain} groupId={GROUP} />);
    // 还没有预览结果时不显示回执
    expect(first.queryByTestId("material-isolation-report")).toBeNull();

    // 用一个带 isolation 载荷的任务回话（真实服务端在请求隔离时会带上它）
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => schemaPayload({ groups }),
      job: (async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          result: {
            ...jobPayload().result,
            isolation: {
              requested: true,
              material: MATERIAL,
              applied: true,
              reason: null,
              objects_kept: ["model_mesh"],
              objects_hidden: ["model_mesh_outline"],
              restored_ok: true,
              mismatches: {},
            },
          },
        })) as never,
    });
    await workspace.actions.bootstrap();
    const { getByTestId } = render(<MaterialEditor workspace={workspace} groupId={GROUP} />);

    await workspace.actions.applyAndPreview({ isolateMaterial: MATERIAL });
    await settle();

    const report = getByTestId("material-isolation-report");
    expect(report.textContent).toContain(MATERIAL);
    expect(report.textContent).toContain("已生效");
    expect(report.textContent).toContain("model_mesh");
    expect(report.textContent).toContain("model_mesh_outline");
    expect(report.textContent).toContain("可见性已逐项写回原值");
  });
});
