import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SaveDialog } from "./SaveDialog";
import { AppError } from "../../api/types";
import { commitPreparedPayload, commitResponsePayload, commitStatusPayload } from "../../testing/fixtures";
import { makeTestWorkspace, settle } from "../../testing/endpoints";

afterEach(cleanup);

const TARGET = "D:\\项目\\角色-调参.blend";
const BACKUP = "D:\\项目\\角色-调参.backup-20261009.blend";

async function setup(overrides: Record<string, unknown> = {}) {
  const { workspace, endpoints } = makeTestWorkspace(overrides);
  await workspace.actions.bootstrap();
  const view = render(<SaveDialog workspace={workspace} />);
  return { workspace, endpoints, ...view };
}

function confirmError() {
  return new AppError(
    {
      code: "SAVE_CONFIRM_REQUIRED",
      message: "即将覆盖已有文件。",
      retryable: false,
      details: {
        mode: "save_as",
        target_path: TARGET,
        backup_path: BACKUP,
        warnings: ["目标文件已存在"],
      },
    },
    409
  );
}

describe("保存面板", () => {
  it("默认收起；打开后默认「另存为」", async () => {
    const { workspace, getByTestId, queryByTestId } = await setup();
    expect(queryByTestId("save-dialog")).toBeNull();
    workspace.actions.openSaveDialog();
    await settle(2);
    expect(getByTestId("save-dialog")).toBeTruthy();
    expect((getByTestId("save-mode-save_as") as HTMLInputElement).checked).toBe(true);
    expect((getByTestId("save-mode-overwrite") as HTMLInputElement).checked).toBe(false);
  });

  it("路径不合法时禁用「准备保存」并给出原因（不发必然 422 的请求）", async () => {
    const { workspace, getByTestId, queryByTestId } = await setup();
    workspace.actions.openSaveDialog("save_as");
    await settle(2);

    expect(queryByTestId("save-target-error")).toBeNull();
    fireEvent.input(getByTestId("save-target"), { target: { value: "角色.blend" } });
    await settle(2);
    expect(getByTestId("save-target-error").textContent).toContain("绝对路径");
    expect((getByTestId("save-prepare") as HTMLButtonElement).disabled).toBe(true);

    fireEvent.input(getByTestId("save-target"), { target: { value: TARGET } });
    await settle(2);
    expect(queryByTestId("save-target-error")).toBeNull();
    expect((getByTestId("save-prepare") as HTMLButtonElement).disabled).toBe(false);
  });

  it("切到「覆盖当前工程」时不显示路径输入框，并说明备份约定", async () => {
    const { workspace, getByTestId, queryByTestId, getByText } = await setup();
    workspace.actions.openSaveDialog("overwrite");
    await settle(2);
    expect(queryByTestId("save-target")).toBeNull();
    expect(getByText(/覆盖模式的目标由 Blender 当前工程决定/)).toBeTruthy();
    expect(getByText(/一定先生成同目录备份/)).toBeTruthy();
    void getByTestId;
  });

  it("覆盖需要确认：把绝对目标路径与备份路径都摊开，且不当作错误", async () => {
    const { workspace, getByTestId, queryByTestId } = await setup({
      commitPrepare: vi.fn(async () => {
        throw confirmError();
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    fireEvent.click(getByTestId("save-prepare"));
    await settle(4);

    expect(getByTestId("save-confirm-box")).toBeTruthy();
    expect(getByTestId("save-confirm-target").textContent).toBe(TARGET);
    expect(getByTestId("save-confirm-backup").textContent).toBe(BACKUP);
    expect(getByTestId("save-confirm-warnings").textContent).toContain("目标文件已存在");
    // 待确认不是失败：不该出现错误块
    expect(queryByTestId("save-error")).toBeNull();
    // 确认之前不能重复准备
    expect((getByTestId("save-prepare") as HTMLButtonElement).disabled).toBe(true);
  });

  it("点「确认并覆盖」后重新准备并保存成功", async () => {
    const { workspace, endpoints, getByTestId } = await setup({
      commitPrepare: vi.fn(async (body: { confirm: boolean }) => {
        if (!body.confirm) {
          throw confirmError();
        }
        return commitPreparedPayload({ backup_path: BACKUP, confirmation_required: true });
      }),
      // 确认过的覆盖**必然**生成备份 —— 夹具也要照这个来，否则「备份路径有没有被显示」
      // 这件事在测试里根本测不到。
      commitApply: vi.fn(async () =>
        commitResponsePayload({
          backup_path: BACKUP,
          status: commitStatusPayload({
            backup: { required: true, created: true, path: BACKUP, path_confirmed: BACKUP },
          }),
        })
      ),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    fireEvent.click(getByTestId("save-prepare"));
    await settle(4);
    fireEvent.click(getByTestId("save-confirm"));
    await settle(6);

    expect(endpoints.commitPrepare.mock.calls[1][0].confirm).toBe(true);
    expect(getByTestId("save-ok").textContent).toContain(TARGET);
    expect(getByTestId("save-ok").textContent).toContain(BACKUP);
    expect(getByTestId("save-ok").textContent).toContain("基线已按新内容刷新");
  });

  it("成功时五行状态表全部渲染出来", async () => {
    const { workspace, getByTestId } = await setup({
      commitApply: vi.fn(async () =>
        commitResponsePayload({
          backup_path: BACKUP,
          status: commitStatusPayload({
            backup: { required: true, created: true, path: BACKUP, path_confirmed: BACKUP },
          }),
        })
      ),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    fireEvent.click(getByTestId("save-prepare"));
    await settle(6);

    expect(getByTestId("save-status-row-applied")).toBeTruthy();
    expect(getByTestId("save-status-row-readback")).toBeTruthy();
    expect(getByTestId("save-status-row-backup").textContent).toContain(BACKUP);
    expect(getByTestId("save-status-row-saved").textContent).toContain(TARGET);
    expect(getByTestId("save-status-row-rollback").textContent).toContain("未触发");
    // 参数面那一半必须单独列出来
    expect(getByTestId("save-status-row-applied-surface")).toBeTruthy();
    expect(getByTestId("save-steps")).toBeTruthy();
  });

  it("失败时明说磁盘没被写、草稿与最后一张预览都保留", async () => {
    const { workspace, getByTestId } = await setup({
      commitApply: vi.fn(async () => {
        throw new AppError(
          {
            code: "SAVE_FAILED",
            message: "Blender 报告保存完成，但回读的工程路径/文件与目标不一致。",
            retryable: true,
            details: {
              status: commitStatusPayload({ saved: false, save: undefined }),
              steps: ["应用完整草稿", "回读校验", "保存工程"],
            },
          },
          409
        );
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    fireEvent.click(getByTestId("save-prepare"));
    await settle(6);

    expect(getByTestId("save-error").textContent).toContain("SAVE_FAILED");
    expect(getByTestId("save-failure").textContent).toContain("草稿与最后一张成功预览都已保留");
    expect(getByTestId("save-steps").textContent).toContain("保存工程");
    expect(getByTestId("save-status-row-saved").textContent).toContain("未写入");
  });

  it("结构失效时禁用「准备保存」并说明原因", async () => {
    const { workspace, getByTestId } = await setup();
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "变了", retryable: true });
    await settle(2);

    expect((getByTestId("save-prepare") as HTMLButtonElement).disabled).toBe(true);
    expect(getByTestId("save-hint").textContent).toContain("作废");
  });

  it("「收起」只收面板，不清掉刚才写到了哪里的信息", async () => {
    const { workspace, getByTestId, queryByTestId } = await setup();
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await settle(2);
    fireEvent.click(getByTestId("save-prepare"));
    await settle(6);
    expect(getByTestId("save-ok")).toBeTruthy();

    fireEvent.click(getByTestId("save-close"));
    await settle(2);
    expect(queryByTestId("save-dialog")).toBeNull();

    workspace.actions.openSaveDialog("save_as");
    await settle(2);
    expect(getByTestId("save-ok")).toBeTruthy();
  });
});
