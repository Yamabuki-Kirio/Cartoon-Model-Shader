import { describe, expect, it, vi } from "vitest";
import { ownerGroupId } from "./workspace";
import { jobPayload, baselinePayload, schemaPayload } from "../testing/fixtures";
import { makeTestWorkspace, settle, settleTimers } from "../testing/endpoints";
import type { JobState } from "../api/types";

function makeWorkspace(overrides: Record<string, unknown> = {}) {
  return makeTestWorkspace(overrides);
}

describe("工作台状态机", () => {
  it("bootstrap 拉取 schema、基线、连接与取景，并选中第一个可用分组", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();
    const state = workspace.store.getState();
    expect(state.schema?.schema_version).toBe("toon-surface/2");
    expect(state.baselineId).toBe("bl0000000001");
    expect(state.structureHash).toBe("f".repeat(64));
    expect(state.activeGroupId).toBe("cel.Cel_Skin");
    expect(state.connection.status).toBe("connected");
    expect(state.framing?.camera).toBe("Camera");
  });

  it("schema 与 baseline 必须来自同一 baseline_id（不一致时以基线为准并保留指纹）", async () => {
    const { workspace } = makeWorkspace({
      surfaceBaseline: vi.fn(async () => baselinePayload({ baselineId: "other", structureHash: "a".repeat(64) })),
      surfaceSchema: vi.fn(async () => schemaPayload({ structureHash: "a".repeat(64) })),
    });
    await workspace.actions.bootstrap();
    const state = workspace.store.getState();
    expect(state.baselineId).toBe("other");
    expect(state.structureHash).toBe("a".repeat(64));
  });

  it("打开草稿后统计脏项，复位后清零", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();

    workspace.actions.dragElement(
      "cel.Cel_Skin",
      1,
      [
        { position: 0, color: [0.05, 0.05, 0.08, 1] },
        { position: 0.6, color: [0.5, 0.48, 0.46, 1] },
        { position: 1, color: [0.95, 0.94, 0.92, 1] },
      ],
      "LINEAR"
    );
    expect(workspace.store.getState().dirtyIds.has("cel.Cel_Skin.ramp")).toBe(true);
    expect(workspace.store.getState().canUndo).toBe(true);

    workspace.actions.resetAllDraft();
    expect(workspace.store.getState().dirtyIds.size).toBe(0);
  });

  it("撤销 / 重做改回草稿但不碰服务端任务（命令栈独立）", async () => {
    const { workspace, endpoints } = makeWorkspace();
    await workspace.actions.bootstrap();
    endpoints.submitPreview.mockClear();

    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    expect(workspace.store.getState().undoDepth).toBe(1);
    workspace.actions.undo();
    expect(workspace.store.getState().dirtyIds.size).toBe(0);
    expect(workspace.store.getState().canRedo).toBe(true);
    workspace.actions.redo();
    expect(workspace.store.getState().dirtyIds.size).toBe(1);
    // 撤销/重做不触发任何请求
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("复制到兼容组成功；不兼容时返回原因且不写草稿", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();

    // 参考组恒不可编辑
    const readonlyReason = workspace.actions.copyRampToGroup("cel.Cel_Skin", "cel.Sakura_Hair_Reference");
    expect(readonlyReason).toContain("不可编辑");
    expect(workspace.store.getState().dirtyIds.size).toBe(0);

    // 色标数量相同的组可以复制
    const ok = workspace.actions.copyRampToGroup("cel.Cel_Skin", "cel.Cel_Hair");
    expect(ok).toBeNull();
    const draft = workspace.store.getState().draft;
    expect(draft["cel.Cel_Hair.ramp"]).toBeTruthy();
    expect(workspace.store.getState().dirtyIds.has("cel.Cel_Hair.ramp")).toBe(true);
  });

  it("色标数量不同的目标组被拒绝，原因经状态机原样返回", async () => {
    const groups = schemaPayload().groups as Array<{ id: string; children: unknown[] }>;
    const twoElement = {
      ...groups[0],
      id: "cel.Cel_Cloth",
      label: "Cel_Cloth",
      children: [
        {
          ...(groups[0].children[0] as Record<string, unknown>),
          id: "cel.Cel_Cloth.ramp",
          elements: (groups[0].children[0] as { elements: unknown[] }).elements.slice(0, 2),
        },
      ],
    };
    const { workspace } = makeWorkspace({
      surfaceSchema: vi.fn(async () => schemaPayload({ groups: [...schemaPayload().groups, twoElement] })),
    });
    await workspace.actions.bootstrap();

    const reason = workspace.actions.copyRampToGroup("cel.Cel_Skin", "cel.Cel_Cloth");
    expect(reason).toContain("色标数量不同");
    expect(workspace.store.getState().draft["cel.Cel_Cloth.ramp"]).toBeUndefined();
  });

  it("结构失效：草稿作废、命令栈清空、预览被禁用，但保留最后成功预览", async () => {
    const { workspace, endpoints } = makeWorkspace();
    await workspace.actions.bootstrap();
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await workspace.actions.applyAndPreview();
    await settle();
    const preview = workspace.store.getState().lastSuccessfulPreview;
    expect(preview).toBeTruthy();

    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "结构已变化",
      retryable: true,
    });
    const state = workspace.store.getState();
    expect(state.historyBlocked).toBe(true);
    expect(state.draft).toEqual({});
    expect(state.canUndo).toBe(false);
    expect(state.lastSuccessfulPreview).toBe(preview);
    expect(state.notice).toContain("作废");

    // 预览被禁用：提交直接返回 false 且**不再产生请求**
    endpoints.submitPreview.mockClear();
    await expect(workspace.actions.applyAndPreview()).resolves.toBe(false);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    expect(workspace.store.getState().error?.code).toBe("STRUCTURE_CHANGED");
  });

  it("失败任务不清除最后成功预览，且错误被展示", async () => {
    const failing = jobPayload({
      status: "failed",
      result: null,
      error: {
        code: "APPLY_VERIFY_FAILED",
        message: "草稿写入后回读不一致，已中止本次预览。",
        retryable: true,
        details: { steps: ["应用完整草稿"], restore: { attempted: true, verified: true } },
      },
    }) as unknown as JobState;
    let mode: "ok" | "fail" = "ok";
    const { workspace } = makeWorkspace({
      job: vi.fn(async () => (mode === "ok" ? (jobPayload() as unknown as JobState) : failing)),
    });
    await workspace.actions.bootstrap();
    await workspace.actions.applyAndPreview();
    await settle();
    const good = workspace.store.getState().lastSuccessfulPreview;
    expect(good?.jobId).toBe("job0000000000001");

    mode = "fail";
    await workspace.actions.applyAndPreview();
    await settle();

    const state = workspace.store.getState();
    expect(state.error?.code).toBe("APPLY_VERIFY_FAILED");
    // 修正：任务已经终结，就**不该**继续占据「活动任务」位；它属于 lastJob。
    expect(state.activeJob).toBeNull();
    expect(state.lastJob?.status).toBe("failed");
    // 关键：画面还在（失败不得把最后一张成功预览挤掉）
    expect(state.lastSuccessfulPreview).toBe(good);
  });

  it("预览任务以 STRUCTURE_CHANGED 终结时，走同一条结构阻断路径", async () => {
    // 服务端同样会用任务终态报结构失效（提交后工程被改）。若这里只写一句 error，
    // 草稿不作废、historyBlocked 仍为 false，用户还能继续提交已作废的草稿。
    let mode: "ok" | "structure" = "ok";
    const { workspace, endpoints } = makeWorkspace({
      job: vi.fn(async () =>
        mode === "ok"
          ? (jobPayload() as unknown as JobState)
          : (jobPayload({
              status: "failed",
              result: null,
              error: {
                code: "STRUCTURE_CHANGED",
                message: "工程结构已变化：色标数量与基线不一致。",
                retryable: true,
                details: { structure_changed: ["Cel_Skin"] },
              },
            }) as unknown as JobState)
      ),
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await workspace.actions.applyAndPreview();
    await settle();
    const preview = workspace.store.getState().lastSuccessfulPreview;
    expect(preview).toBeTruthy();

    mode = "structure";
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "LINEAR" });
    await workspace.actions.applyAndPreview();
    await settle();

    const state = workspace.store.getState();
    expect(state.error?.code).toBe("STRUCTURE_CHANGED");
    expect(state.historyBlocked).toBe(true);
    expect(state.draft).toEqual({});
    expect(state.dirtyIds.size).toBe(0);
    expect(state.canUndo).toBe(false);
    expect(state.notice).toContain("作废");
    // 任务上下文照旧留痕（lastJob），但**不**再算活动任务；最后一张成功预览不受影响
    expect(state.activeJob).toBeNull();
    expect(state.lastJob?.status).toBe("failed");
    expect(state.lastSuccessfulPreview).toBe(preview);

    // 阻断生效：再提交直接被拒，且不产生新任务
    endpoints.submitPreview.mockClear();
    await expect(workspace.actions.applyAndPreview()).resolves.toBe(false);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("IDENTITY_MISSING 的任务终态同样触发阻断", async () => {
    const { workspace } = makeWorkspace({
      job: vi.fn(async () =>
        jobPayload({
          status: "failed",
          result: null,
          error: { code: "IDENTITY_MISSING", message: "对象已重命名。", retryable: true },
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await workspace.actions.applyAndPreview();
    await settle();
    expect(workspace.store.getState().historyBlocked).toBe(true);
    expect(workspace.store.getState().error?.code).toBe("IDENTITY_MISSING");
  });

  it("关闭错误提示不解除结构锁（锁只能由成功刷新基线解除）", async () => {
    const { workspace, endpoints } = makeWorkspace();
    await workspace.actions.bootstrap();
    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "结构已变化",
      retryable: true,
    });

    workspace.actions.clearError();
    let state = workspace.store.getState();
    expect(state.error).toBeNull();
    expect(state.historyBlocked).toBe(true);

    // 关掉横幅之后依然提交不了
    endpoints.submitPreview.mockClear();
    await expect(workspace.actions.applyAndPreview()).resolves.toBe(false);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    // 只有刷新基线成功才解锁
    await workspace.actions.refreshBaseline();
    state = workspace.store.getState();
    expect(state.historyBlocked).toBe(false);
  });

  it("外部值变化只提示，不作废草稿", async () => {
    const external = [{ id: "cel.Cel_Skin.ramp[1].color.0", baseline: 0.5, current: 0.9 }];
    const { workspace } = makeWorkspace({
      job: vi.fn(async () =>
        jobPayload({
          external_changes: external,
          result: { ...jobPayload().result, external_changes: external },
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await workspace.actions.applyAndPreview();
    await settle();

    const state = workspace.store.getState();
    expect(state.externalChanges).toHaveLength(1);
    expect(state.historyBlocked).toBe(false);
    expect(state.dirtyIds.size).toBe(1);
  });

  it("渲染期间改过草稿 ⇒ 画面标记为过期且不写 effective", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();
    const started = workspace.actions.applyAndPreview();
    // 渲染期间继续编辑
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await started;
    await settle();

    const state = workspace.store.getState();
    expect(state.previewStale).toBe(true);
    expect(state.effective["cel.Cel_Skin.ramp"]).toBeUndefined();
    expect(state.notice).toContain("过期");
  });

  it("成功时写入 effective 并落防缓存预览 URL", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();
    await workspace.actions.applyAndPreview();
    await settle();

    const state = workspace.store.getState();
    expect(state.previewStale).toBe(false);
    expect(state.effective["cel.Cel_Skin.ramp"]).toBeTruthy();
    expect(state.lastSuccessfulPreview?.url).toContain("?v=job0000000000001");
    expect(state.lastSuccessfulPreview?.restoreVerified).toBe(true);
  });

  it("刷新基线清空草稿与命令栈，并清除结构失效状态", async () => {
    const { workspace } = makeWorkspace();
    await workspace.actions.bootstrap();
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "x", retryable: true });

    await workspace.actions.refreshBaseline({ mode: "auto_headshot", margin: 0.2 });
    const state = workspace.store.getState();
    expect(state.historyBlocked).toBe(false);
    expect(state.dirtyIds.size).toBe(0);
    expect(state.canUndo).toBe(false);
    expect(state.framingChoice.mode).toBe("auto_headshot");
  });

  it("提交时带上结构指纹与取景选择", async () => {
    const { workspace, endpoints } = makeWorkspace();
    await workspace.actions.bootstrap();
    workspace.actions.setFramingChoice({ mode: "auto_upper_body", margin: 0.1 });
    await workspace.actions.applyAndPreview();
    await settle();

    const payload = endpoints.submitPreview.mock.calls[0][0] as {
      expected_structure_hash?: string;
      framing?: { mode: string };
      draft: Record<string, unknown>;
    };
    expect(payload.expected_structure_hash).toBe("f".repeat(64));
    expect(payload.framing?.mode).toBe("auto_upper_body");
  });

  it("只提交可写参数：只读项与参考组被过滤掉", async () => {
    const { workspace, endpoints } = makeWorkspace();
    await workspace.actions.bootstrap();
    workspace.store.setState({
      draft: {
        "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" },
        "cel.Cel_Skin.managed_mode": "editable",
        "cel.Sakura_Hair_Reference.ramp": { elements: [], interpolation: "LINEAR" },
      },
    });
    await workspace.actions.applyAndPreview();
    await settle();
    const payload = endpoints.submitPreview.mock.calls[0][0] as { draft: Record<string, unknown> };
    expect(Object.keys(payload.draft)).toEqual(["cel.Cel_Skin.ramp"]);
  });

  it("没有基线时 bootstrap 只读请求 409 不致命", async () => {
    const { workspace } = makeWorkspace({
      surfaceBaseline: vi.fn(async () => {
        throw new Error("409");
      }),
    });
    await workspace.actions.bootstrap();
    const state = workspace.store.getState();
    expect(state.schema).toBeTruthy();
    expect(state.baseline).toBeNull();
    expect(state.error).toBeTruthy();
  });
});

describe("建立基线：轮询首张预览任务", () => {
  /** 记录状态迁移，用来断言「任务完成前不许有基线图」。 */
  function recorder(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]) {
    const frames: Array<{ job: string | null; baseline: string; url: string | null }> = [];
    const unsubscribe = workspace.store.subscribe(() => {
      const state = workspace.store.getState();
      frames.push({
        job: state.activeJob?.status ?? null,
        baseline: state.baselinePreviewStatus,
        url: state.baselinePreviewUrl,
      });
    });
    return { frames, stop: unsubscribe };
  }

  it("queued → running → done：完成后才给出基线图 URL", async () => {
    const statuses = ["queued", "queued", "running", "running", "done"];
    let poll = 0;
    const { workspace } = makeTestWorkspace({
      job: vi.fn(async (jobId: string) => {
        const status = statuses[Math.min(poll, statuses.length - 1)];
        poll += 1;
        return jobPayload({ job_id: jobId, status }) as unknown as JobState;
      }),
    });
    await workspace.actions.bootstrap();

    const { frames, stop } = recorder(workspace);
    const started = workspace.actions.refreshBaseline();
    await settleTimers(12);
    await started;
    stop();

    // 轮询真的经过了排队与渲染两个阶段
    const jobStates = frames.map((frame) => frame.job).filter((value): value is string => value !== null);
    expect(jobStates).toContain("queued");
    expect(jobStates).toContain("running");

    // 任务没结束时：状态是 pending，且**没有** URL（否则 img 会指向还没落盘的文件）
    const pendingFrames = frames.filter((frame) => frame.baseline === "pending");
    expect(pendingFrames.length).toBeGreaterThan(0);
    expect(pendingFrames.every((frame) => frame.url === null)).toBe(true);

    const state = workspace.store.getState();
    expect(state.baselineId).toBe("bl0000000001");
    expect(state.baselinePreviewStatus).toBe("ready");
    expect(state.baselinePreviewUrl).toBe("/api/preview/base0");
    // 终态清理：任务结束后不再占着活动位，但留下「上次」痕迹
    expect(state.activeJob).toBeNull();
    expect(state.lastJob?.status).toBe("done");
    expect(state.loading).toBe(false);
  });

  it("基线任务失败：基线仍算建立，但基线图判为失败并给提示", async () => {
    const { workspace } = makeTestWorkspace({
      job: vi.fn(async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          status: "failed",
          result: null,
          error: { code: "PREVIEW_FAILED", message: "渲染失败", retryable: true },
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    await expect(workspace.actions.refreshBaseline()).resolves.toBe(true);

    const state = workspace.store.getState();
    expect(state.baselineId).toBeTruthy();
    expect(state.baselinePreviewStatus).toBe("failed");
    expect(state.baselinePreviewUrl).toBeNull();
    expect(state.notice).toContain("基线已建立");
    expect(state.activeJob).toBeNull();
    expect(state.lastJob?.status).toBe("failed");
    expect(state.loading).toBe(false);
  });

  it("基线任务被取代：改判为失败而不是永远停在「生成中」", async () => {
    const { workspace } = makeTestWorkspace({
      job: vi.fn(async (jobId: string) =>
        jobPayload({ job_id: jobId, status: "superseded", result: null }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    await workspace.actions.refreshBaseline();

    const state = workspace.store.getState();
    expect(state.baselinePreviewStatus).toBe("failed");
    expect(state.baselinePreviewUrl).toBeNull();
    expect(state.notice).toContain("取代");
    expect(state.activeJob).toBeNull();
  });

  it("刷新基线途中提交预览：基线图改判为失败，不会卡在 pending", async () => {
    // 基线任务一直排队（永不完结），此时用户点了「应用并预览」
    const { workspace } = makeTestWorkspace({
      job: vi.fn(async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          status: jobId.startsWith("base") ? "running" : "done",
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();

    const notices: string[] = [];
    const stop = workspace.store.subscribe(() => {
      const notice = workspace.store.getState().notice;
      if (notice) {
        notices.push(notice);
      }
    });

    const started = workspace.actions.refreshBaseline();
    await settleTimers(2);
    expect(workspace.store.getState().baselinePreviewStatus).toBe("pending");

    await workspace.actions.applyAndPreview();
    await settleTimers(8);
    await started;
    stop();

    const state = workspace.store.getState();
    expect(state.baselinePreviewStatus).toBe("failed");
    // 改判时给出原因（之后被新任务的成功提示覆盖，所以要在过程里看）
    expect(notices.some((notice) => notice.includes("取代"))).toBe(true);
    // 新任务照常完成并给出画面
    expect(state.activeJob).toBeNull();
    expect(state.lastSuccessfulPreview?.jobId).toBe("job0000000000001");
  });

  it("刷新基线失败时基线图状态同步改判，不会假装还在生成", async () => {
    const { workspace } = makeTestWorkspace({
      createBaseline: vi.fn(async () => {
        throw new Error("Blender 连不上");
      }),
    });
    await workspace.actions.bootstrap();
    await expect(workspace.actions.refreshBaseline()).resolves.toBe(false);
    const state = workspace.store.getState();
    expect(state.error).toBeTruthy();
    expect(state.baselinePreviewStatus).toBe("failed");
    expect(state.loading).toBe(false);
  });

  it("基线原本干净、预览后变脏 ⇒ 提示「工程未被保存」（O3）", async () => {
    const { workspace } = makeWorkspace({
      job: vi.fn(async () =>
        jobPayload({
          result: {
            ...jobPayload().result,
            project: {
              dirty_at_baseline: false,
              dirty_after_preview: true,
              dirty_flagged: true,
              file_name: "一个工程.blend",
            },
          },
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    await workspace.actions.applyAndPreview();
    await settle();

    const state = workspace.store.getState();
    // 只承诺我们能保证的：恢复原值 + 不写盘；不承诺「Blender 看起来是干净的」
    expect(state.projectNotice).toContain("不会保存或覆盖");
    expect(state.projectNotice).toContain("不代表工程内容有差异");
    // 绝对路径/用户名不得出现在提示里
    expect(state.projectNotice).not.toContain("\\\\");
    expect(state.projectNotice).not.toContain("Users");

    workspace.actions.clearProjectNotice();
    expect(workspace.store.getState().projectNotice).toBeNull();
  });

  it("工程本来就脏（或服务端未标记）⇒ 不提示（O3）", async () => {
    const { workspace } = makeWorkspace({
      job: vi.fn(async () =>
        jobPayload({
          result: {
            ...jobPayload().result,
            project: {
              dirty_at_baseline: true,
              dirty_after_preview: true,
              dirty_flagged: false,
            },
          },
        }) as unknown as JobState
      ),
    });
    await workspace.actions.bootstrap();
    await workspace.actions.applyAndPreview();
    await settle();
    expect(workspace.store.getState().projectNotice).toBeNull();
  });
});

describe("分组 id 反推", () => {
  it("从参数 id 推出分组节点 id", () => {
    expect(ownerGroupId("cel.Cel_Skin.ramp")).toBe("cel.Cel_Skin");
    expect(ownerGroupId("cel.Cel_Skin.emission_strength")).toBe("cel.Cel_Skin");
    expect(ownerGroupId("solo")).toBe("solo");
  });
});
