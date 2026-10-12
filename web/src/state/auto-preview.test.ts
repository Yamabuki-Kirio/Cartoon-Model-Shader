/**
 * 自动预览的交互规则。
 *
 * 用**假计时器**：规则本身就是「等多久才渲染」，用真计时器测就只能靠 sleep，
 * 既慢又不稳。假计时器让「300ms 之后」成为一个可精确断言的事实。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AUTO_PREVIEW_COMMIT_MS,
  AUTO_PREVIEW_INPUT_MS,
  createWorkspace,
} from "./workspace";
import { jobPayload } from "../testing/fixtures";
import { makeTestEndpoints, makeTestWorkspace } from "../testing/endpoints";
import type { JobState } from "../api/types";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

const RAMP = "cel.Cel_Skin.ramp";

function ramp(interpolation: string) {
  return {
    elements: [
      { position: 0, color: [0, 0, 0, 1] },
      { position: 1, color: [1, 1, 1, 1] },
    ],
    interpolation,
  };
}

async function setup(autoPreview = true, overrides: Record<string, unknown> = {}) {
  const { workspace, endpoints } = makeTestWorkspace(overrides, { autoPreview });
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

/** 推进假计时器并冲刷随之产生的微任务（任务轮询用的是 0ms 定时器）。 */
async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
}

describe("自动预览开关", () => {
  it("生产默认开启（测试夹具才默认关掉）", () => {
    const endpoints = makeTestEndpoints();
    const workspace = createWorkspace({ endpoints, pollIntervalMs: 0, now: () => 0 });
    expect(workspace.store.getState().autoPreview).toBe(true);
    expect(workspace.store.getState().autoPreviewPending).toBe(false);
    workspace.actions.dispose();
  });

  it("关掉后改草稿不渲染，只留「待预览」提示", async () => {
    const { workspace, endpoints } = await setup(false);
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_INPUT_MS * 3);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    expect(workspace.store.getState().autoPreviewPending).toBe(false);
  });

  it("重新打开时，若草稿还没被渲染就立刻排一次", async () => {
    const { workspace, endpoints } = await setup(false);
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(50);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    // 先渲染一次，让 lastSuccessfulPreview 存在（否则「画面过期」无从谈起）
    await workspace.actions.applyAndPreview();
    await advance(50);
    endpoints.submitPreview.mockClear();

    // 关着开关改一次 → 画面过期
    workspace.actions.setValue(RAMP, ramp("EASE"));
    await advance(50);
    expect(workspace.store.getState().previewStale).toBe(true);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    workspace.actions.setAutoPreview(true);
    expect(workspace.store.getState().autoPreviewPending).toBe(true);
    await advance(AUTO_PREVIEW_COMMIT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
  });
});

describe("自动预览的等待时长", () => {
  it("连续输入：停手约 300ms 才渲染", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));

    await advance(AUTO_PREVIEW_INPUT_MS - 1);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    await advance(1);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
  });

  it("操作结束（sealHistory）：等待缩短到 120ms", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    workspace.actions.sealHistory();

    await advance(AUTO_PREVIEW_COMMIT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
  });

  it("sealHistory 之后再改，等待重新回到 300ms", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.sealHistory();
    // 只有 seal 没有改动 ⇒ 草稿没变 ⇒ 不排期
    await advance(AUTO_PREVIEW_COMMIT_MS);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_COMMIT_MS);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    await advance(AUTO_PREVIEW_INPUT_MS - AUTO_PREVIEW_COMMIT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
  });
});

describe("快速连续修改只执行最后一次", () => {
  it("三次改动只渲染一次，且渲染的是最后一次的值", async () => {
    const { workspace, endpoints } = await setup();

    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(100);
    workspace.actions.setValue(RAMP, ramp("EASE"));
    await advance(100);
    workspace.actions.setValue(RAMP, ramp("LINEAR"));
    await advance(100);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    expect(endpoints.submitPreview.mock.calls[0][0].draft[RAMP]).toEqual(ramp("LINEAR"));
  });

  it("画面已经是当前草稿时不再重复渲染", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);

    // 值没变（`commitCommand` 会提前返回）⇒ 没有新排期
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_INPUT_MS * 2);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);

    // 撤销回基线（草稿变成空）⇒ 画面与草稿确实不一致，必须重渲染
    workspace.actions.undo();
    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(2);
  });
});

describe("渲染期间的行为", () => {
  it("渲染中不打断：排期到点也不提交，等任务结束后只补渲染一次", async () => {
    // 任务停在 running，直到我们显式放行 —— `pollIntervalMs: 0` 下任务会瞬间跑完，
    // 不这么控制就根本测不到「排期到点时任务还在跑」这个分支。
    let release = false;
    const { workspace, endpoints } = await setup(true, {
      job: vi.fn(
        async (jobId: string) =>
          jobPayload({ job_id: jobId, status: release ? "done" : "running" }) as unknown as JobState
      ),
    });

    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    expect(workspace.store.getState().activeJob?.status).toBe("running");

    // 渲染期间又改一次：排期到点时任务还在跑 ⇒ 不提交（也不取代在途渲染）
    workspace.actions.setValue(RAMP, ramp("EASE"));
    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);

    // 放行任务 ⇒ 终态回收时补渲染一次，且只补一次
    release = true;
    await advance(50);
    expect(workspace.store.getState().activeJob).toBeNull();
    await advance(AUTO_PREVIEW_COMMIT_MS + 10);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(2);
    // 第二次渲染的就是渲染期间那次改动
    expect(endpoints.submitPreview.mock.calls[1][0].draft[RAMP]).toEqual(ramp("EASE"));
  });

  it("任务失败不续排（否则会对同一份必然失败的草稿反复重试）", async () => {
    const { workspace, endpoints } = await setup(true, {
      job: vi.fn(
        async () =>
          jobPayload({
            status: "failed",
            result: null,
            error: { code: "PREVIEW_FAILED", message: "渲染失败", retryable: true },
          }) as unknown as JobState
      ),
    });

    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    await advance(AUTO_PREVIEW_INPUT_MS);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);

    await advance(AUTO_PREVIEW_COMMIT_MS * 10);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    expect(workspace.store.getState().autoPreviewPending).toBe(false);
    expect(workspace.store.getState().error?.code).toBe("PREVIEW_FAILED");
  });
});

describe("排期的取消", () => {
  it("结构失效撤销排期，且此后不再自动渲染", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    expect(workspace.store.getState().autoPreviewPending).toBe(true);

    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "结构已变化",
      retryable: true,
    });
    expect(workspace.store.getState().autoPreviewPending).toBe(false);

    await advance(AUTO_PREVIEW_INPUT_MS * 4);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("刷新基线撤销排期（旧草稿已作废，不能在新基线上渲染它）", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));

    await workspace.actions.refreshBaseline();
    expect(workspace.store.getState().autoPreviewPending).toBe(false);
    expect(workspace.store.getState().draft).toEqual({});

    await advance(AUTO_PREVIEW_INPUT_MS * 4);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("手动「应用并预览」会撤掉排期，不会紧接着再渲染一次", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    expect(workspace.store.getState().autoPreviewPending).toBe(true);

    await workspace.actions.applyAndPreview();
    expect(workspace.store.getState().autoPreviewPending).toBe(false);
    await advance(AUTO_PREVIEW_INPUT_MS * 3);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
  });

  it("dispose 之后计时器不再提交任务（卸载后的排期必须失效）", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(RAMP, ramp("CONSTANT"));
    workspace.actions.dispose();

    await advance(AUTO_PREVIEW_INPUT_MS * 4);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });
});
