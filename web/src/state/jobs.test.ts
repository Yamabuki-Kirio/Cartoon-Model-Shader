import { describe, expect, it, vi } from "vitest";
import { JobRunner, TERMINAL_STATUSES } from "./jobs";
import { jobPayload } from "../testing/fixtures";
import type { JobState } from "../api/types";
/** 可控时钟：`wait` 不真的等待，`now` 由测试推进。 */
function makeClock() {
  let current = 0;
  return {
    now: () => current,
    advance: (ms: number) => {
      current += ms;
    },
    wait: async () => {
      /* 立即返回，由 fake 队列驱动 */
    },
  };
}

function makeEndpoints(options: {
  statuses: string[];
  onSubmit?: (payload: unknown, index: number) => void;
  failSubmit?: Error;
  jobError?: Error;
}) {
  let submitCount = 0;
  let pollCount = 0;
  const endpoints = {
    submitPreview: vi.fn(async (payload: unknown) => {
      const index = submitCount;
      submitCount += 1;
      options.onSubmit?.(payload, index);
      if (options.failSubmit) {
        throw options.failSubmit;
      }
      return { job_id: `job-${index}`, seq: index + 1, status: "queued" };
    }),
    job: vi.fn(async (jobId: string): Promise<JobState> => {
      if (options.jobError) {
        throw options.jobError;
      }
      const status = options.statuses[Math.min(pollCount, options.statuses.length - 1)];
      pollCount += 1;
      return jobPayload({ job_id: jobId, status }) as unknown as JobState;
    }),
  };
  return { endpoints, submitCount: () => submitCount, pollCount: () => pollCount };
}

describe("JobRunner：代次与取代", () => {
  it("新任务取消旧任务，旧任务的终态不会分发（代次挡掉）", async () => {
    const clock = makeClock();
    const seen: string[] = [];
    const { endpoints } = makeEndpoints({ statuses: ["done"] });

    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: {
        onSucceeded: (job) => seen.push(job.job_id),
      },
    });

    const first = runner.start({ draft: { a: 1 } });
    // 第二个任务立刻取代第一个
    const second = runner.start({ draft: { a: 2 } });
    await Promise.all([first, second]);

    expect(seen).toEqual(["job-1"]);
  });

  it("取消后 onCancelled 被调用一次，且不再分发终态", async () => {
    const clock = makeClock();
    const onCancelled = vi.fn();
    const onSucceeded = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running", "done"] });

    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onCancelled, onSucceeded },
    });

    const started = runner.start({ draft: {} });
    runner.cancel("用户取消");
    await started;

    expect(onCancelled).toHaveBeenCalledTimes(1);
    expect(onSucceeded).not.toHaveBeenCalled();
  });

  it("已完结的任务不再算「活动任务」：顺序提交不触发取消", async () => {
    // 修正：旧实现把 controller 一直留着，于是「上一个任务已 done」也会被判成
    // 「有活动任务要取代」，onCancelled 被无端触发（调用方据此清状态、报「已被取代」，全是假的）。
    const clock = makeClock();
    const cancelled: string[] = [];
    const terminals: string[] = [];
    const { endpoints } = makeEndpoints({ statuses: ["done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: {
        onCancelled: (reason) => cancelled.push(reason),
        onTerminal: (job, outcome) => terminals.push(`${job.job_id}:${outcome}`),
      },
    });

    await runner.start({ draft: {} });
    await expect(runner.start({ draft: {} })).resolves.toBe(true);

    expect(cancelled).toEqual([]);
    expect(terminals).toEqual(["job-0:succeeded", "job-1:succeeded"]);
  });

  it("真正重叠提交才算取代", async () => {
    const clock = makeClock();
    const cancelled: string[] = [];
    const { endpoints } = makeEndpoints({ statuses: ["running", "done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onCancelled: (reason) => cancelled.push(reason) },
    });

    const first = runner.start({ draft: {} });
    const second = runner.start({ draft: {} });
    await Promise.all([first, second]);
    expect(cancelled).toEqual(["提交新任务"]);
  });

  it("superseded 是终态，按失败处理但不触发 onSucceeded", async () => {
    const clock = makeClock();
    const onSucceeded = vi.fn();
    const onFailed = vi.fn();
    const onTerminal = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["superseded"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onSucceeded, onFailed, onTerminal },
    });

    const ok = await runner.start({ draft: {} });
    expect(ok).toBe(false);
    expect(onSucceeded).not.toHaveBeenCalled();
    expect(onFailed).not.toHaveBeenCalled();
    expect(TERMINAL_STATUSES).toContain("superseded");
    // 被取代同样是终态：必须走统一清理，否则它会一直挂在调用方的「活动任务」上
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal.mock.calls[0][1]).toBe("superseded");
  });

  it("adopt：跟踪一个已提交过的任务（建立基线的路径）", async () => {
    const clock = makeClock();
    const updates: string[] = [];
    const onSucceeded = vi.fn();
    const onTerminal = vi.fn();
    const { endpoints, submitCount } = makeEndpoints({ statuses: ["queued", "running", "done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onUpdate: (job) => updates.push(`${job.job_id}:${job.status}`), onSucceeded, onTerminal },
    });

    const ok = await runner.adopt("base-job");
    expect(ok).toBe(true);
    // adopt 不提交任何任务：基线任务由服务端在建立基线的响应里创建
    expect(submitCount()).toBe(0);
    expect(updates).toEqual(["base-job:queued", "base-job:running", "base-job:done"]);
    expect(onSucceeded.mock.calls[0][0].job_id).toBe("base-job");
    expect(onTerminal.mock.calls[0][1]).toBe("succeeded");
  });

  it("adopt 失败时同样走 onFailed + 终态清理", async () => {
    const clock = makeClock();
    const onFailed = vi.fn();
    const onTerminal = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["failed"] });
    endpoints.job.mockImplementation(
      async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          status: "failed",
          result: null,
          error: { code: "PREVIEW_FAILED", message: "渲染失败", retryable: true },
        }) as unknown as JobState
    );
    const runner = new JobRunner({ endpoints, now: clock.now, wait: clock.wait, hooks: { onFailed, onTerminal } });

    await expect(runner.adopt("base-job")).resolves.toBe(false);
    expect(onFailed.mock.calls[0][1]).toMatchObject({ code: "PREVIEW_FAILED" });
    expect(onTerminal.mock.calls[0][1]).toBe("failed");
  });
});

describe("JobRunner：统一终态清理", () => {
  it("成功 / 失败 / 超时 / 轮询报错 / 提交失败都恰好触发一次 onTerminal", async () => {
    const cases: Array<{ label: string; statuses: string[]; jobError?: Error; failSubmit?: Error }> = [
      { label: "成功", statuses: ["done"] },
      { label: "失败", statuses: ["failed"] },
      { label: "轮询报错", statuses: ["running"], jobError: new Error("boom") },
      { label: "提交失败", statuses: ["done"], failSubmit: new Error("network") },
    ];

    for (const item of cases) {
      const clock = makeClock();
      const onTerminal = vi.fn();
      const { endpoints } = makeEndpoints({
        statuses: item.statuses,
        ...(item.jobError ? { jobError: item.jobError } : {}),
        ...(item.failSubmit ? { failSubmit: item.failSubmit } : {}),
      });
      const runner = new JobRunner({
        endpoints,
        now: clock.now,
        wait: clock.wait,
        hooks: { onTerminal },
      });
      await runner.start({ draft: {} });
      expect(onTerminal, item.label).toHaveBeenCalledTimes(1);
    }
  });

  it("超时也走终态清理", async () => {
    const clock = makeClock();
    const onTerminal = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: async () => {
        clock.advance(60_000);
      },
      timeoutMs: 90_000,
      hooks: { onTerminal },
    });

    await runner.start({ draft: {} });
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal.mock.calls[0][1]).toBe("failed");
  });

  it("被取代（代次前移）的旧任务不触发终态清理", async () => {
    const clock = makeClock();
    const onTerminal = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running", "done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onTerminal },
    });

    const first = runner.start({ draft: {} });
    const second = runner.start({ draft: {} });
    await Promise.all([first, second]);

    // 只有最新的那个任务到达终态；旧任务被代次挡掉，不能替新任务「宣告结束」
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal.mock.calls[0][0].job_id).toBe("job-1");
  });
});

describe("JobRunner：轮询、超时、失败", () => {
  it("轮询到终态后分发成功结果，并带上每一步进度", async () => {
    const clock = makeClock();
    const updates: string[] = [];
    const onSucceeded = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["queued", "running", "done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onUpdate: (job) => updates.push(job.status), onSucceeded },
    });

    const ok = await runner.start({ draft: {} });
    expect(ok).toBe(true);
    expect(updates).toEqual(["queued", "running", "done"]);
    expect(onSucceeded).toHaveBeenCalledTimes(1);
  });

  it("超过超时上限后停止轮询并报 JOB_TIMEOUT", async () => {
    const clock = makeClock();
    const onFailed = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: async () => {
        clock.advance(60_000);
      },
      timeoutMs: 90_000,
      hooks: { onFailed },
    });

    const ok = await runner.start({ draft: {} });
    expect(ok).toBe(false);
    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(onFailed.mock.calls[0][1].code).toBe("JOB_TIMEOUT");
  });

  it("任务失败时把服务端的错误码原样传出", async () => {
    const clock = makeClock();
    const onFailed = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["failed"] });
    endpoints.job.mockImplementation(
      async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          status: "failed",
          error: {
            code: "STRUCTURE_CHANGED",
            message: "结构已变化",
            retryable: true,
            details: { structure_changed: ["Cel_Skin"] },
          },
        }) as unknown as JobState
    );
    const runner = new JobRunner({ endpoints, now: clock.now, wait: clock.wait, hooks: { onFailed } });

    await runner.start({ draft: {} });
    expect(onFailed.mock.calls[0][1]).toMatchObject({ code: "STRUCTURE_CHANGED", retryable: true });
  });

  it("提交阶段失败也走 onFailed（不静默）", async () => {
    const clock = makeClock();
    const onFailed = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["done"], failSubmit: new Error("network") });
    const runner = new JobRunner({ endpoints, now: clock.now, wait: clock.wait, hooks: { onFailed } });

    const ok = await runner.start({ draft: {} });
    expect(ok).toBe(false);
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("轮询请求报错也走 onFailed", async () => {
    const clock = makeClock();
    const onFailed = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running"], jobError: new Error("boom") });
    const runner = new JobRunner({ endpoints, now: clock.now, wait: clock.wait, hooks: { onFailed } });

    await runner.start({ draft: {} });
    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(onFailed.mock.calls[0][1].code).toBe("INTERNAL_ERROR");
  });

  it("dispose() 停止轮询：卸载组件后不再有回调", async () => {
    const clock = makeClock();
    const onSucceeded = vi.fn();
    const onUpdate = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["running", "done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onSucceeded, onUpdate },
    });

    const started = runner.start({ draft: {} });
    runner.dispose();
    await started;

    expect(onSucceeded).not.toHaveBeenCalled();
  });

  it("提交的 payload 原样送达（含结构指纹与取景）", async () => {
    const clock = makeClock();
    const seen: unknown[] = [];
    const { endpoints } = makeEndpoints({ statuses: ["done"], onSubmit: (payload) => seen.push(payload) });
    const runner = new JobRunner({ endpoints, now: clock.now, wait: clock.wait });
    await runner.start({
      draft: { "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" } },
      framing: { mode: "auto_headshot", margin: 0.2 },
      expected_structure_hash: "abc",
    });
    expect(seen[0]).toEqual({
      draft: { "cel.Cel_Skin.ramp": { elements: [], interpolation: "LINEAR" } },
      framing: { mode: "auto_headshot", margin: 0.2 },
      expected_structure_hash: "abc",
    });
  });
});
