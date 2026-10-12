import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PresetPanel } from "./PresetPanel";
import { AppError } from "../../api/types";
import { presetDetail, presetListPayload, presetSummary } from "../../testing/fixtures";
import { makeTestWorkspace, settle } from "../../testing/endpoints";

afterEach(cleanup);

async function setup(overrides: Record<string, unknown> = {}) {
  const { workspace, endpoints } = makeTestWorkspace(overrides);
  await workspace.actions.bootstrap();
  const view = render(<PresetPanel workspace={workspace} />);
  workspace.actions.openPresetPanel();
  await settle(4);
  return { workspace, endpoints, ...view };
}

describe("预设面板", () => {
  it("默认收起；打开后列出预设、存储位置与「完整 / L0」的区分", async () => {
    const { workspace, getByTestId, queryByTestId } = await setup();
    expect(getByTestId("preset-panel")).toBeTruthy();
    expect(getByTestId("preset-storage").textContent).toContain("%LOCALAPPDATA%");
    expect(getByTestId("preset-item-p0000000001")).toBeTruthy();
    // 有参数面草稿的 → 完整预设；没有的 → L0 预设。两者不能混成一句。
    expect(getByTestId("preset-meta-p0000000001").textContent).toContain("完整预设");
    expect(getByTestId("preset-meta-p0000000001").textContent).toContain("2 项");
    expect(queryByTestId("preset-empty")).toBeNull();
    void workspace;
  });

  it("只含 L0 的旧预设要如实说「不含 Cel / 材质草稿」", async () => {
    const { getByTestId } = await setup({
      listPresets: vi.fn(async () =>
        presetListPayload({ presets: [presetSummary({ surface_count: 0, parameter_count: 5 })] })
      ),
    });
    const meta = getByTestId("preset-meta-p0000000001").textContent ?? "";
    expect(meta).toContain("L0 预设");
    expect(meta).toContain("不含 Cel / 材质草稿");
    expect(meta).toContain("L0 5 项");
  });

  it("没有预设时给出空态，而不是一片空白", async () => {
    const { getByTestId } = await setup({
      listPresets: vi.fn(async () => presetListPayload({ presets: [] })),
    });
    expect(getByTestId("preset-empty")).toBeTruthy();
  });

  it("读不动的预设文件必须列出来，不静默忽略", async () => {
    const { getByTestId } = await setup({
      listPresets: vi.fn(async () =>
        presetListPayload({
          skipped: [{ file: "坏.json", code: "PRESET_SCHEMA_UNSUPPORTED", message: "不支持的预设 schema。" }],
        })
      ),
    });
    const skipped = getByTestId("preset-skipped").textContent ?? "";
    expect(skipped).toContain("1 个预设文件读不动");
    expect(skipped).toContain("坏.json");
    expect(skipped).toContain("PRESET_SCHEMA_UNSUPPORTED");
  });

  it("保存完整预设：名字为空时按钮禁用；填了名字才发请求", async () => {
    const { workspace, endpoints, getByTestId } = await setup();
    expect((getByTestId("preset-save") as HTMLButtonElement).disabled).toBe(true);

    workspace.actions.setValue("cel.Cel_Skin.emission_strength", 2);
    await settle(2);
    fireEvent.input(getByTestId("preset-name"), { target: { value: "柔和肤色 v2" } });
    await settle(2);
    expect((getByTestId("preset-save") as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(getByTestId("preset-save"));
    await settle(6);
    expect(endpoints.createPreset).toHaveBeenCalledTimes(1);
    expect(endpoints.createPreset.mock.calls[0][0].surface).toEqual({
      "cel.Cel_Skin.emission_strength": 2,
    });
    expect(getByTestId("preset-notice").textContent).toContain("柔和肤色 v2");
    // 存完清空输入框：留着上一次的名字容易让人重复保存同一个名字
    expect((getByTestId("preset-name") as HTMLInputElement).value).toBe("");
  });

  it("草稿为空时提醒「会是一份空预设」", async () => {
    const { workspace, getByTestId } = await setup();
    expect(getByTestId("preset-draft-hint").textContent).toContain("空预设");
    workspace.actions.setValue("cel.Cel_Skin.emission_strength", 2);
    await settle(2);
    expect(getByTestId("preset-draft-hint").textContent).toContain("将保存 1 项改动");
  });

  it("同名冲突显示在面板里，且不弹全局错误横幅", async () => {
    const { workspace, getByTestId } = await setup({
      createPreset: vi.fn(async () => {
        throw new AppError(
          { code: "PRESET_NAME_CONFLICT", message: "已存在同名预设。", retryable: false },
          409
        );
      }),
    });
    fireEvent.input(getByTestId("preset-name"), { target: { value: "撞名" } });
    await settle(2);
    fireEvent.click(getByTestId("preset-save"));
    await settle(6);
    expect(getByTestId("preset-error").textContent).toContain("PRESET_NAME_CONFLICT");
    expect(workspace.store.getState().error).toBeNull();
  });

  it("覆盖需要先确认，确认后才发 PUT", async () => {
    const { workspace, endpoints, getByTestId, queryByTestId } = await setup();
    expect(queryByTestId("preset-overwrite-box")).toBeNull();

    fireEvent.click(getByTestId("preset-overwrite-p0000000001"));
    await settle(2);
    expect(getByTestId("preset-overwrite-box").textContent).toContain("柔和肤色 v2");
    expect(endpoints.updatePreset).not.toHaveBeenCalled();

    fireEvent.click(getByTestId("preset-overwrite-confirm"));
    await settle(6);
    expect(endpoints.updatePreset).toHaveBeenCalledTimes(1);
    expect(endpoints.updatePreset.mock.calls[0][0]).toBe("p0000000001");
    expect(workspace.store.getState().presets.notice).toContain("已覆盖预设");
    expect(queryByTestId("preset-overwrite-box")).toBeNull();
  });

  it("取消覆盖不发请求", async () => {
    const { endpoints, getByTestId, queryByTestId } = await setup();
    fireEvent.click(getByTestId("preset-overwrite-p0000000001"));
    await settle(2);
    fireEvent.click(getByTestId("preset-overwrite-cancel"));
    await settle(2);
    expect(queryByTestId("preset-overwrite-box")).toBeNull();
    expect(endpoints.updatePreset).not.toHaveBeenCalled();
  });

  it("套用预设后提示套用了几项、跳过了几项", async () => {
    const { workspace, endpoints, getByTestId } = await setup({
      readPreset: vi.fn(async () => ({
        ok: true as const,
        preset: presetDetail({
          surface: {
            "cel.Cel_Skin.emission_strength": 6,
            "material.别的工程.roughness": 0.4,
          },
        }),
      })),
    });
    fireEvent.click(getByTestId("preset-apply-p0000000001"));
    await settle(6);
    expect(endpoints.readPreset.mock.calls[0][0]).toBe("p0000000001");
    const notice = getByTestId("preset-notice").textContent ?? "";
    expect(notice).toContain("已套用");
    expect(notice).toContain("1 项在当前工程里找不到");
    expect(workspace.store.getState().draft).toEqual({ "cel.Cel_Skin.emission_strength": 6 });
    // 套用是可撤销的一步
    expect(workspace.store.getState().canUndo).toBe(true);
  });

  it("删除预设后重新拉列表", async () => {
    const { endpoints, getByTestId } = await setup();
    endpoints.listPresets.mockClear();
    fireEvent.click(getByTestId("preset-delete-p0000000001"));
    await settle(6);
    expect(endpoints.deletePreset).toHaveBeenCalledTimes(1);
    expect(endpoints.listPresets).toHaveBeenCalledTimes(1);
  });

  it("结构失效时禁用「保存完整预设」「套用」「覆盖」", async () => {
    const { workspace, getByTestId } = await setup();
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "变了", retryable: true });
    await settle(2);
    expect((getByTestId("preset-save") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("preset-apply-p0000000001") as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId("preset-overwrite-p0000000001") as HTMLButtonElement).disabled).toBe(true);
    // 删除不受结构影响：它跟工程结构无关
    expect((getByTestId("preset-delete-p0000000001") as HTMLButtonElement).disabled).toBe(false);
  });

  it("「重新读取」会再拉一次列表", async () => {
    const { endpoints, getByTestId } = await setup();
    endpoints.listPresets.mockClear();
    fireEvent.click(getByTestId("preset-refresh"));
    await settle(4);
    expect(endpoints.listPresets).toHaveBeenCalledTimes(1);
  });
});
