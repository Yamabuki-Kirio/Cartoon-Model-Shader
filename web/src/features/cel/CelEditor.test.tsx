import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { CelEditor } from "./CelEditor";
import { makeTestWorkspace, settle } from "../../testing/endpoints";
import { groupNode, rampNode, emissionScalar, schemaPayload } from "../../testing/fixtures";
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

/** jsdom 里没有布局：给轨道一个固定的几何，位置才能从 clientX 算出来。 */
function stubTrackGeometry(track: Element, width = 200): void {
  (track as HTMLElement).getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: 10, width, height: 10, x: 0, y: 0 }) as DOMRect;
}

/** 用 MouseEvent 造指针事件：jsdom 的 PointerEvent 支持不完整，而处理函数只读坐标。 */
function pointer(type: "pointerdown" | "pointermove" | "pointerup", clientX: number): MouseEvent {
  return new MouseEvent(type, { bubbles: true, cancelable: true, clientX });
}

function rampDraft(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]) {
  return workspace.store.getState().draft["cel.Cel_Skin.ramp"] as {
    elements: Array<{ position: number; color: number[] }>;
    interpolation: string;
  };
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

  it("真实拖动生命周期：pointerdown → pointermove → pointerup 改草稿并封存历史", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const track = getByTestId("ramp-track-cel.Cel_Skin");
    stubTrackGeometry(track, 200);
    const handle = getByTestId("ramp-handle-cel.Cel_Skin-1");

    // 拖动前先动一次别的东西，验证「拖动只追加一条记录」
    fireEvent.input(getByTestId("ramp-position-cel.Cel_Skin-1"), { target: { value: "0.5" } });
    fireEvent.blur(getByTestId("ramp-position-cel.Cel_Skin-1"));
    expect(workspace.store.getState().undoDepth).toBe(1);

    fireEvent(handle, pointer("pointerdown", 100));
    // 每次事件之间让 Preact 完成重渲染 —— 浏览器里就是这样，拖动跨多帧。
    // 若「当前拖第几号」靠 state 判定，这里每一步都会读到 null 而整段失效。
    await settle(2);
    // 两次 move 用同一个 mergeKey ⇒ 只算一条撤销记录
    fireEvent(track, pointer("pointermove", 40)); // → 0.2
    await settle(2);
    fireEvent(track, pointer("pointermove", 60)); // → 0.3
    await settle(2);
    expect(rampDraft(workspace).elements[1].position).toBeCloseTo(0.3);
    expect(workspace.store.getState().undoDepth).toBe(2);

    // 邻居约束仍然生效：拖过右侧色标（1.0）会被夹住
    fireEvent(track, pointer("pointermove", 400)); // → 被夹到 1 - MIN_POSITION_GAP
    await settle(2);
    expect(rampDraft(workspace).elements[1].position).toBeLessThan(1);
    expect(rampDraft(workspace).elements[1].position).toBeGreaterThan(0.99);

    fireEvent(track, pointer("pointerup", 400));
    await settle(2);
    // 松开后仍继续 move ⇒ 不应再改草稿（证明拖动真的结束了）
    const afterUp = rampDraft(workspace).elements[1].position;
    fireEvent(track, pointer("pointermove", 20));
    await settle(2);
    expect(rampDraft(workspace).elements[1].position).toBe(afterUp);

    // 松开已经封存历史：下一次拖动是新的一条记录
    fireEvent(handle, pointer("pointerdown", 100));
    await settle(2);
    fireEvent(track, pointer("pointermove", 80)); // → 0.4
    await settle(2);
    expect(workspace.store.getState().undoDepth).toBe(3);

    // 撤销回到上一次拖动结束时的那条记录，而不是回到拖动中途
    workspace.actions.undo();
    expect(rampDraft(workspace).elements[1].position).toBeCloseTo(afterUp);
  });

  it("轨道宽度为 0（布局未就绪）时拖动不写入草稿", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    const track = getByTestId("ramp-track-cel.Cel_Skin");
    // 不 stub：jsdom 的 getBoundingClientRect 全是 0
    fireEvent(getByTestId("ramp-handle-cel.Cel_Skin-1"), pointer("pointerdown", 100));
    fireEvent(track, pointer("pointermove", 100));
    expect(workspace.store.getState().dirtyIds.size).toBe(0);
    expect(workspace.store.getState().undoDepth).toBe(0);
  });

  it("只读组即使收到指针事件也不改草稿", async () => {
    const { workspace } = await setup([
      groupNode("Sakura_Hair_Reference", {
        editable: false,
        children: [rampNode({ group: "Sakura_Hair_Reference", editable: false })],
      }),
    ]);
    const { getByTestId } = render(
      <CelEditor workspace={workspace} groupId="cel.Sakura_Hair_Reference" />
    );
    const track = getByTestId("ramp-track-cel.Sakura_Hair_Reference");
    stubTrackGeometry(track);
    fireEvent(
      getByTestId("ramp-handle-cel.Sakura_Hair_Reference-1"),
      pointer("pointerdown", 100)
    );
    fireEvent(track, pointer("pointermove", 20));
    expect(workspace.store.getState().dirtyIds.size).toBe(0);
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

  it("schema 里没有 emission 子节点时给出「未探测到」说明", async () => {
    const { workspace } = await setup([celGroup("Cel_Skin", 3)]);
    const { getByTestId, container } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    // 夹具的 Cel_Skin 不含 emission 子节点
    expect(getByTestId("cel-emission-absent")).toBeTruthy();
    expect(container.querySelector('input[type="number"][data-testid^="cel-emission-"]')).toBeNull();
  });

  it("Emission 可编辑时才出现控件（schema 说了算，真机确认后即可写）", async () => {
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

  it("Emission 被降级只读（插座被连线 / 参考组）时只显示值，不渲染输入框", async () => {
    const group = celGroup("Cel_Skin", 3) as {
      children: unknown[];
    } & Record<string, unknown>;
    group.children.push({
      ...emissionScalar("cel.Cel_Skin.emission_strength", 0.5),
      editable: false,
      readonly_reason: "structural",
      binding: undefined,
    });
    const { workspace } = await setup([group]);
    const { getByTestId, container } = render(<CelEditor workspace={workspace} groupId="cel.Cel_Skin" />);
    expect(getByTestId("cel-emission-readonly-cel.Cel_Skin.emission_strength").textContent).toBe("0.5");
    expect(container.querySelector('input[data-testid^="cel-emission-"]')).toBeNull();
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
