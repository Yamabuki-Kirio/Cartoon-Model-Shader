import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { CelEditor } from "./CelEditor";
import { makeTestWorkspace, settle } from "../../testing/endpoints";
import { groupNode, rampNode, schemaPayload } from "../../testing/fixtures";
import { parseSchema, topGroups } from "../../schema/parse";

afterEach(cleanup);

async function setup(groups?: unknown[]) {
  const { workspace, endpoints } = makeTestWorkspace({
    ...(groups
      ? { surfaceSchema: async () => schemaPayload({ groups }) }
      : {}),
  });
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

function celGroup(name: string, count: number, extra: Record<string, unknown> = {}) {
  return groupNode(name, {
    ...extra,
    children: [
      rampNode({
        group: name,
        elements: Array.from({ length: count }, (_, index) => [
          index / Math.max(1, count - 1),
          [0.1 * index, 0.2, 0.3, 1.0],
        ]) as Array<[number, number[]]>,
        ...(extra["rampOptions"] as Record<string, unknown> | undefined ?? {}),
      }),
    ],
  });
}

describe("Cel 编辑器", () => {
  it("2 / 3 / 4 个色标都渲染出对应数量的控件", async () => {
    for (const count of [2, 3, 4]) {
      const { workspace } = await setup([celGroup("Cel_Skin", count)]);
      const { getAllByTestId, unmount } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
      expect(getAllByTestId(/^ramp-position-cel\.Cel_Skin-/)).toHaveLength(count);
      expect(getAllByTestId(/^ramp-color-cel\.Cel_Skin-/)).toHaveLength(count);
      expect(getAllByTestId(/^ramp-handle-cel\.Cel_Skin-/)).toHaveLength(count);
      unmount();
    }
  });

  it("端点色标（首尾）不可拖动，中间色标可拖动", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    expect((getByTestId("ramp-handle-cel.Cel_Skin-0") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("ramp-handle-cel.Cel_Skin-2") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("ramp-handle-cel.Cel_Skin-1") as HTMLButtonElement).disabled).toBe(false);
  });

  it("数值输入越界被夹回合法区间（0–1 且严格递增）", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const input = getByTestId("ramp-position-cel.Cel_Skin-1") as HTMLInputElement;

    fireEvent.input(input, { target: { value: "9" } });
    let draft = workspace.store.getState().draft["cel.Cel_Skin.ramp"] as {
      elements: Array<{ position: number }>;
    };
    expect(draft.elements[1].position).toBeLessThan(1);

    fireEvent.input(input, { target: { value: "-3" } });
    draft = workspace.store.getState().draft["cel.Cel_Skin.ramp"] as {
      elements: Array<{ position: number }>;
    };
    expect(draft.elements[1].position).toBeGreaterThan(0);
  });

  it("连续输入合并成一条撤销记录，失焦后不再合并", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const input = getByTestId("ramp-position-cel.Cel_Skin-1") as HTMLInputElement;

    fireEvent.input(input, { target: { value: "0.4" } });
    fireEvent.input(input, { target: { value: "0.5" } });
    fireEvent.input(input, { target: { value: "0.6" } });
    expect(workspace.store.getState().undoDepth).toBe(1);

    fireEvent.blur(input);
    fireEvent.input(input, { target: { value: "0.7" } });
    expect(workspace.store.getState().undoDepth).toBe(2);

    workspace.actions.undo();
    const draft = workspace.store.getState().draft["cel.Cel_Skin.ramp"] as {
      elements: Array<{ position: number }>;
    };
    expect(draft.elements[1].position).toBeCloseTo(0.6);
  });

  it("编辑不自动触发渲染（L1：只改草稿）", async () => {
    const { workspace, endpoints } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    fireEvent.input(getByTestId("ramp-position-cel.Cel_Skin-1"), { target: { value: "0.6" } });
    fireEvent.input(getByTestId("ramp-alpha-cel.Cel_Skin-1"), { target: { value: "0.5" } });
    await settle(2);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    expect(workspace.store.getState().dirtyIds.size).toBe(1);
  });

  it("插值切换写入草稿", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    fireEvent.change(getByTestId("cel-interpolation-cel.Cel_Skin"), { target: { value: "CONSTANT" } });
    const draft = workspace.store.getState().draft["cel.Cel_Skin.ramp"] as { interpolation: string };
    expect(draft.interpolation).toBe("CONSTANT");
  });

  it("参考组（Sakura）只读：控件禁用并给出原因", async () => {
    const { workspace } = await setup([
      groupNode("Sakura_Hair_Reference", {
        editable: false,
        children: [rampNode({ group: "Sakura_Hair_Reference", editable: false })],
      }),
    ]);
    const { getByTestId, getAllByTestId } = render(
      <CelEditor workspace={workspace} groupId="cel.Sakura_Hair_Reference" />
    );
    expect(getByTestId("cel-readonly-reason").textContent).toContain("参考对象");
    const positions = getAllByTestId(/^ramp-position-cel\.Sakura_Hair_Reference-/) as HTMLInputElement[];
    expect(positions.every((input) => input.disabled)).toBe(true);
    // 只读时不会写入草稿
    fireEvent.input(positions[1] ?? positions[0], { target: { value: "0.5" } });
    expect(workspace.store.getState().dirtyIds.size).toBe(0);
  });

  it("结构失效时复制与复位被禁用", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3), celGroup("Cel_Hair", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "x", retryable: true });
    await settle(2);
    expect((getByTestId("cel-reset-cel.Cel_Skin") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("cel-copy-target-cel.Cel_Skin") as HTMLSelectElement).disabled).toBe(true);
  });

  it("不兼容目标在下拉里被禁用并列出原因", async () => {
    const { workspace } = await setup([
      celGroup("Cel_Skin", 3),
      groupNode("Sakura_Hair_Reference", {
        editable: false,
        children: [rampNode({ group: "Sakura_Hair_Reference", editable: false })],
      }),
    ]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const option = getByTestId("cel-copy-target-cel.Cel_Skin").querySelector(
      'option[value="cel.Sakura_Hair_Reference"]'
    ) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain("不兼容");
    expect(getByTestId("cel-copy-notes-cel.Cel_Skin").textContent).toContain("不可编辑");
  });

  it("Emission 未经真机确认时只读显示，不渲染输入框", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId, container } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    // 夹具的 Cel_Skin 不含 emission 子节点
    expect(getByTestId("cel-emission-absent")).toBeTruthy();
    expect(container.querySelector('input[type="number"][data-testid^="cel-emission-"]')).toBeNull();
  });

  it("Emission 可编辑时才出现控件（schema 说了算）", async () => {
    const group = celGroup("Cel_Skin", 3) as {
      children: unknown[];
    } & Record<string, unknown>;
    group.children.push({
      id: "cel.Cel_Skin.emission_strength",
      kind: "float",
      group: "cel",
      cost: "L1",
      label: "Emission",
      supported: true,
      editable: true,
      active: true,
      value_source: "scene",
      value: 2,
      baseline: 2,
      effective: 2,
      minimum: 0,
      maximum: 10,
      step: 0.1,
    });
    const { workspace } = await setup([group]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const input = getByTestId("cel-emission-cel.Cel_Skin.emission_strength").querySelector("input");
    expect(input).toBeTruthy();
    fireEvent.input(input as HTMLInputElement, { target: { value: "3.5" } });
    expect(workspace.store.getState().draft["cel.Cel_Skin.emission_strength"]).toBe(3.5);
  });

  it("色标数量只读展示，且提示不提供增删", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 4)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const text = getByTestId("cel-count-cel.Cel_Skin").textContent ?? "";
    expect(text).toContain("4");
    expect(text).toContain("只读");
  });

  it("没有色带的组给出明确说明", async () => {
    const { workspace } = await setup([groupNode("Cel_Dark", { children: [] })]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Dark" />);
    expect(getByTestId("cel-no-ramp").textContent).toContain("没有色带");
  });

  it("schema 未加载时提示而不是崩溃", () => {
    const { workspace } = makeTestWorkspace();
    const { getByText } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    expect(getByText("尚未加载 schema。")).toBeTruthy();
  });
});

describe("Cel 编辑器与 schema 解析一致", () => {
  it("控件数量来自解析后的 schema（4 档夹具）", () => {
    const schema = parseSchema(schemaPayload({ groups: [celGroup("Cel_Skin", 4)] }));
    const ramp = topGroups(schema)[0].children.find((node) => node.kind === "ramp");
    expect(ramp?.kind === "ramp" && ramp.elements.length).toBe(4);
  });
});
