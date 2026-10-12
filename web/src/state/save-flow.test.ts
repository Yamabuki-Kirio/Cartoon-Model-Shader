import { describe, expect, it, vi } from "vitest";
import { AppError } from "../api/types";
import { commitPreparedPayload, commitResponsePayload, commitStatusPayload } from "../testing/fixtures";
import { makeTestWorkspace, settle } from "../testing/endpoints";

const TARGET = "D:\\项目\\角色-调参.blend";
const BACKUP = "D:\\项目\\角色-调参.backup-20261009.blend";
/** 第一次 prepare 冻结的确认单号：第二次确认必须原样回传。 */
const CONFIRMATION = "cf0000000001";

async function makeWorkspace(overrides: Record<string, unknown> = {}) {
  const { workspace, endpoints } = makeTestWorkspace(overrides);
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

/** 开一个真实的草稿：没有草稿就测不出「失败时草稿还在」。 */
function edit(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]): void {
  workspace.actions.setValue("cel.Cel_Skin.emission_strength", 3.5);
}

function confirmError() {
  return new AppError(
    {
      code: "SAVE_CONFIRM_REQUIRED",
      message: `即将覆盖已有文件：${TARGET}。`,
      retryable: false,
      details: {
        mode: "save_as",
        target_path: TARGET,
        backup_path: BACKUP,
        warnings: ["目标文件已存在"],
        confirmation_id: CONFIRMATION,
      },
    },
    409
  );
}

describe("保存闭环：正向路径", () => {
  it("prepare → commit → 已保存，且草稿与基线一起对齐", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    edit(workspace);
    expect(workspace.store.getState().dirtyIds.size).toBeGreaterThan(0);

    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    const saved = await workspace.actions.prepareSave(false);

    expect(saved).toBe(true);
    // 两步各调一次，且**都**带上了结构指纹与参数面草稿
    expect(endpoints.commitPrepare).toHaveBeenCalledTimes(1);
    expect(endpoints.commitApply).toHaveBeenCalledTimes(1);
    const prepared = endpoints.commitPrepare.mock.calls[0][0];
    expect(prepared.mode).toBe("save_as");
    expect(prepared.target_path).toBe(TARGET);
    expect(prepared.confirm).toBe(false);
    expect(prepared.structure_hash).toBe("f".repeat(64));
    expect(prepared.draft).toEqual({});
    expect(prepared.surface_draft).toEqual({ "cel.Cel_Skin.emission_strength": 3.5 });

    const applied = endpoints.commitApply.mock.calls[0][0];
    expect(applied.token).toBe("tok-0000000001");
    expect(applied.mode).toBe("save_as");
    // 目标路径以**票据**为准：前端不重复传一遍，少一处可以不一致的地方。
    expect(applied.target_path).toBeUndefined();
    expect(applied.surface_draft).toEqual({ "cel.Cel_Skin.emission_strength": 3.5 });

    const state = workspace.store.getState();
    expect(state.save.phase).toBe("saved");
    expect(state.save.outcome?.saved).toBe(true);
    expect(state.save.outcome?.baselineRefreshed).toBe(true);
    // 保存成功后草稿清空：磁盘上的内容现在就是那份草稿。
    expect(state.draft).toEqual({});
    expect(state.dirtyIds.size).toBe(0);
    expect(state.previewStale).toBe(false);
    expect(state.historyBlocked).toBe(false);
  });

  it("成功后按新内容重新读基线，但**不**再渲染一张多余的图", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    endpoints.surfaceBaseline.mockClear();
    endpoints.createBaseline.mockClear();
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    await settle();

    expect(endpoints.surfaceBaseline).toHaveBeenCalledTimes(1);
    // createBaseline 会白渲染一张与刚才那张一模一样的图 —— 保存路径绝不该调它。
    expect(endpoints.createBaseline).not.toHaveBeenCalled();
  });

  it("另存为路径不合法时当场拦下，一个请求都不发", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget("角色.blend");
    const ok = await workspace.actions.prepareSave(false);
    expect(ok).toBe(false);
    expect(endpoints.commitPrepare).not.toHaveBeenCalled();
    expect(workspace.store.getState().save.error?.code).toBe("SAVE_TARGET_INVALID");
  });

  it("覆盖模式不传目标路径（由 Blender 当前工程决定）", async () => {
    const { workspace, endpoints } = await makeWorkspace({
      commitPrepare: vi.fn(async () =>
        commitPreparedPayload({ mode: "overwrite", backup_path: BACKUP, confirmation_required: true })
      ),
    });
    workspace.actions.openSaveDialog("overwrite");
    await workspace.actions.prepareSave(false);
    expect(endpoints.commitPrepare.mock.calls[0][0].target_path).toBeNull();
    expect(endpoints.commitPrepare.mock.calls[0][0].mode).toBe("overwrite");
  });
});

describe("保存闭环：覆盖需要二次确认", () => {
  it("SAVE_CONFIRM_REQUIRED 停在「待确认」，不当失败、不签发、不提交", async () => {
    const { workspace, endpoints } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        throw confirmError();
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    const ok = await workspace.actions.prepareSave(false);

    expect(ok).toBe(false);
    const state = workspace.store.getState();
    expect(state.save.phase).toBe("confirming");
    // 这不是错误 —— 用户还没做错任何事，只是还没看清要写到哪里。
    expect(state.save.error).toBeNull();
    expect(state.save.confirm?.targetPath).toBe(TARGET);
    expect(state.save.confirm?.backupPath).toBe(BACKUP);
    expect(state.save.confirm?.warnings).toEqual(["目标文件已存在"]);
    // 确认单号一并收下：点确认时要原样回传，服务端才能沿用这条备份路径
    expect(state.save.confirm?.confirmationId).toBe(CONFIRMATION);
    expect(endpoints.commitApply).not.toHaveBeenCalled();
  });

  it("确认之后重新 prepare（confirm=true）并提交", async () => {
    let confirmed = false;
    const { workspace, endpoints } = await makeWorkspace({
      commitPrepare: vi.fn(async (body: { confirm: boolean }) => {
        if (!body.confirm) {
          throw confirmError();
        }
        confirmed = true;
        return commitPreparedPayload({ backup_path: BACKUP, confirmation_required: true });
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    const ok = await workspace.actions.prepareSave(true);

    expect(ok).toBe(true);
    expect(confirmed).toBe(true);
    expect(endpoints.commitPrepare).toHaveBeenCalledTimes(2);
    expect(endpoints.commitPrepare.mock.calls[1][0].confirm).toBe(true);
    // 第二次确认必须回传第一次拿到的确认单号：服务端据此沿用已展示的备份路径
    expect(endpoints.commitPrepare.mock.calls[1][0].confirmation_id).toBe(CONFIRMATION);
    // 第一次不带（那时还没有单号）
    expect(endpoints.commitPrepare.mock.calls[0][0].confirmation_id ?? null).toBeNull();
    expect(workspace.store.getState().save.phase).toBe("saved");
  });

  it("details 里没有路径时如实说拿不到，绝不编一条出来让用户确认", async () => {
    const { workspace } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        throw new AppError({ code: "SAVE_CONFIRM_REQUIRED", message: "要确认", retryable: false }, 409);
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    const state = workspace.store.getState();
    expect(state.save.phase).toBe("failed");
    expect(state.save.confirm).toBeNull();
    expect(state.save.error?.message).toContain("没有给出目标路径与备份路径");
  });

  it("切换保存模式会丢掉上一次的确认信息（它指向的已经不是同一件事）", async () => {
    const { workspace } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        throw confirmError();
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    expect(workspace.store.getState().save.confirm).toBeTruthy();

    workspace.actions.setSaveMode("overwrite");
    const state = workspace.store.getState();
    expect(state.save.confirm).toBeNull();
    expect(state.save.status).toBeNull();
  });
});

describe("保存闭环：失败保留一切", () => {
  it("commit 失败时草稿与最后一张成功预览都还在，且四态与步骤都能看到", async () => {
    const failedStatus = commitStatusPayload({
      readback_verified: false,
      mismatches: [{ parameter: "cel.Cel_Skin.emission_strength" }],
      backup: { required: true, created: false, path: null, path_confirmed: BACKUP },
      saved: false,
      save: undefined,
    });
    const { workspace, endpoints } = await makeWorkspace({
      commitApply: vi.fn(async () => {
        throw new AppError(
          {
            code: "APPLY_VERIFY_FAILED",
            message: "草稿写入后回读不一致，已中止保存（磁盘上的工程文件未被写入）。",
            retryable: true,
            details: { status: failedStatus, steps: ["复核确认时的前提", "应用完整草稿", "回读校验"] },
          },
          409
        );
      }),
    });
    // 先拿一张成功的预览图，才能验证失败不会把它冲掉。
    await workspace.actions.applyAndPreview();
    await settle();
    const previewBefore = workspace.store.getState().lastSuccessfulPreview;
    expect(previewBefore).toBeTruthy();

    edit(workspace);
    const draftBefore = workspace.store.getState().draft;
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    const ok = await workspace.actions.prepareSave(false);

    expect(ok).toBe(false);
    const state = workspace.store.getState();
    expect(state.save.phase).toBe("failed");
    expect(state.save.status?.readback_verified).toBe(false);
    expect(state.save.steps).toEqual(["复核确认时的前提", "应用完整草稿", "回读校验"]);
    expect(state.save.error?.code).toBe("APPLY_VERIFY_FAILED");
    // ★ 两条最重要的不变量
    expect(state.draft).toEqual(draftBefore);
    expect(state.lastSuccessfulPreview).toBe(previewBefore);
    expect(state.dirtyIds.size).toBeGreaterThan(0);
    void endpoints;
  });

  it("commit 返回 saved:false 但没抛错时同样算失败，草稿照旧保留", async () => {
    const { workspace } = await makeWorkspace({
      commitApply: vi.fn(async () =>
        commitResponsePayload({ saved: false, status: commitStatusPayload({ saved: false, save: undefined }) })
      ),
    });
    edit(workspace);
    const draftBefore = workspace.store.getState().draft;
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    const ok = await workspace.actions.prepareSave(false);
    expect(ok).toBe(false);
    expect(workspace.store.getState().save.phase).toBe("failed");
    expect(workspace.store.getState().draft).toEqual(draftBefore);
  });

  it("prepare 阶段就失败（例如路径非法）同样保留草稿", async () => {
    const { workspace } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        throw new AppError(
          { code: "SAVE_TARGET_INVALID", message: "目标路径必须是绝对路径。", retryable: false },
          400
        );
      }),
    });
    edit(workspace);
    const draftBefore = workspace.store.getState().draft;
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    expect(workspace.store.getState().save.phase).toBe("failed");
    expect(workspace.store.getState().draft).toEqual(draftBefore);
  });

  it("令牌被篡改 / 过期时如实报错，且草稿保留（不静默重试）", async () => {
    const { workspace, endpoints } = await makeWorkspace({
      commitApply: vi.fn(async () => {
        throw new AppError(
          { code: "COMMIT_TOKEN_MISMATCH", message: "草稿与准备阶段不一致。", retryable: false },
          409
        );
      }),
    });
    edit(workspace);
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    const state = workspace.store.getState();
    expect(state.save.error?.code).toBe("COMMIT_TOKEN_MISMATCH");
    expect(state.draft).toEqual({ "cel.Cel_Skin.emission_strength": 3.5 });
    // 不自动重试：一次提交只调一次。
    expect(endpoints.commitApply).toHaveBeenCalledTimes(1);
  });
});

describe("保存闭环：结构闸门", () => {
  it("保存期间结构变化 → 草稿作废、确认信息撤掉、令牌不可能再用", async () => {
    const { workspace } = await makeWorkspace({
      commitApply: vi.fn(async () => {
        throw new AppError(
          {
            code: "STRUCTURE_CHANGED",
            message: "工程结构已变化（节点增删或色标数量）。",
            retryable: false,
            details: { status: commitStatusPayload({ saved: false, save: undefined }), steps: ["复核 v4 结构"] },
          },
          409
        );
      }),
    });
    edit(workspace);
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);

    const state = workspace.store.getState();
    expect(state.historyBlocked).toBe(true);
    expect(state.draft).toEqual({});
    expect(state.save.confirm).toBeNull();
    expect(state.notice).toContain("草稿与已签发的保存确认令牌均已作废");
  });

  it("结构失效期间拒绝准备保存（连请求都不发）", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "变了", retryable: true });
    endpoints.commitPrepare.mockClear();
    const ok = await workspace.actions.prepareSave(false);
    expect(ok).toBe(false);
    expect(endpoints.commitPrepare).not.toHaveBeenCalled();
    expect(workspace.store.getState().save.phase).toBe("idle");
  });

  it("刷新基线会撤掉待确认信息（那个令牌已经随基线一起失效）", async () => {
    const { workspace } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        throw confirmError();
      }),
    });
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    await workspace.actions.prepareSave(false);
    expect(workspace.store.getState().save.confirm).toBeTruthy();

    await workspace.actions.refreshBaseline();
    const state = workspace.store.getState();
    expect(state.save.confirm).toBeNull();
    expect(state.save.phase).toBe("idle");
  });

  it("保存进行中不重复签发令牌（按钮点了两次也只发一次）", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { workspace, endpoints } = await makeWorkspace({
      commitPrepare: vi.fn(async () => {
        await gate;
        return commitPreparedPayload();
      }),
    });
    edit(workspace);
    workspace.actions.openSaveDialog("save_as");
    workspace.actions.setSaveTarget(TARGET);
    const first = workspace.actions.prepareSave(false);
    const second = workspace.actions.prepareSave(false);
    release();
    await Promise.all([first, second]);
    expect(endpoints.commitPrepare).toHaveBeenCalledTimes(1);
  });
});

describe("预设闭环", () => {
  it("打开面板顺带拉一次列表（读接口，不带令牌）", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    workspace.actions.openPresetPanel();
    await settle();
    expect(endpoints.listPresets).toHaveBeenCalledTimes(1);
    const state = workspace.store.getState();
    expect(state.presets.open).toBe(true);
    expect(state.presets.list).toHaveLength(1);
    expect(state.presets.storage).toContain("%LOCALAPPDATA%");
  });

  it("保存完整预设：参数面草稿整份带上，L0 留空", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    edit(workspace);
    const ok = await workspace.actions.savePreset("  柔和肤色 v2  ");
    expect(ok).toBe(true);
    const body = endpoints.createPreset.mock.calls[0][0];
    expect(body.name).toBe("柔和肤色 v2");
    expect(body.parameters).toEqual({});
    expect(body.surface).toEqual({ "cel.Cel_Skin.emission_strength": 3.5 });
    expect(workspace.store.getState().presets.notice).toContain("柔和肤色 v2");
  });

  it("空名字当场拒绝，不发请求", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    const ok = await workspace.actions.savePreset("   ");
    expect(ok).toBe(false);
    expect(endpoints.createPreset).not.toHaveBeenCalled();
    expect(workspace.store.getState().presets.error?.code).toBe("PRESET_INVALID");
  });

  it("同名冲突走面板自己的错误，不动全局错误横幅", async () => {
    const { workspace } = await makeWorkspace({
      createPreset: vi.fn(async () => {
        throw new AppError(
          { code: "PRESET_NAME_CONFLICT", message: "已存在同名预设。", retryable: false },
          409
        );
      }),
    });
    await workspace.actions.savePreset("撞名");
    const state = workspace.store.getState();
    expect(state.presets.error?.code).toBe("PRESET_NAME_CONFLICT");
    // 改个名字就能解决的事，不该同时弹两条提示。
    expect(state.error).toBeNull();
  });

  it("覆盖预设走 PUT，身份保留", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    edit(workspace);
    await workspace.actions.savePreset("柔和肤色 v2", "p0000000001");
    expect(endpoints.updatePreset).toHaveBeenCalledTimes(1);
    expect(endpoints.updatePreset.mock.calls[0][0]).toBe("p0000000001");
    expect(endpoints.createPreset).not.toHaveBeenCalled();
    expect(workspace.store.getState().presets.notice).toContain("已覆盖预设");
  });

  it("套用预设写进草稿、可撤销，并把对不上的项报出来", async () => {
    const { workspace, endpoints } = await makeWorkspace({
      readPreset: vi.fn(async () => ({
        ok: true as const,
        preset: {
          preset_id: "p0000000001",
          name: "别的工程",
          schema: "toon-tuner-preset/1",
          created_at: "2026-10-01T10:00:00+08:00",
          updated_at: "2026-10-01T10:00:00+08:00",
          pipeline_mode: "faithful",
          framing_mode: "current_camera",
          framing_margin: 0.15,
          preview_quality: "standard",
          parameter_count: 0,
          active_count: 0,
          surface_count: 2,
          parameters: {},
          surface: {
            "cel.Cel_Skin.emission_strength": 7,
            "material.别的工程才有的材质.roughness": 0.4,
          },
        },
      })),
    });
    const ok = await workspace.actions.applyPreset("p0000000001");
    expect(ok).toBe(true);
    expect(workspace.store.getState().draft).toEqual({ "cel.Cel_Skin.emission_strength": 7 });
    const notice = workspace.store.getState().presets.notice ?? "";
    expect(notice).toContain("已套用");
    expect(notice).toContain("1 项在当前工程里找不到");
    // 套用必须能撤销 —— 按错一下不该让人手动改回去。
    expect(workspace.store.getState().canUndo).toBe(true);
    workspace.actions.undo();
    expect(workspace.store.getState().draft).toEqual({});
    void endpoints;
  });

  it("结构失效期间拒绝套用预设", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "变了", retryable: true });
    endpoints.readPreset.mockClear();
    const ok = await workspace.actions.applyPreset("p0000000001");
    expect(ok).toBe(false);
    expect(endpoints.readPreset).not.toHaveBeenCalled();
    expect(workspace.store.getState().presets.error?.code).toBe("STRUCTURE_CHANGED");
  });

  it("删除预设后重新拉列表（不靠本地删缓存假装成功）", async () => {
    const { workspace, endpoints } = await makeWorkspace();
    await workspace.actions.openPresetPanel();
    await settle();
    endpoints.listPresets.mockClear();
    await workspace.actions.deletePreset("p0000000001");
    expect(endpoints.deletePreset).toHaveBeenCalledTimes(1);
    expect(endpoints.listPresets).toHaveBeenCalledTimes(1);
  });
});
