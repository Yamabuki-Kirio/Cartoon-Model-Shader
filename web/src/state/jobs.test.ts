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

  it("任务取代：只有一个任务处于活动状态", async () => {
    const clock = makeClock();
    const cancelled: string[] = [];
    const { endpoints } = makeEndpoints({ statuses: ["done"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onCancelled: (reason) => cancelled.push(reason) },
    });

    await runner.start({ draft: {} });
    await runner.start({ draft: {} });
    await runner.start({ draft: {} });
    expect(cancelled.length).toBe(2);
    expect(runner.currentGeneration).toBe(3);
  });

  it("superseded 是终态，按失败处理但不触发 onSucceeded", async () => {
    const clock = makeClock();
    const onSucceeded = vi.fn();
    const onFailed = vi.fn();
    const { endpoints } = makeEndpoints({ statuses: ["superseded"] });
    const runner = new JobRunner({
      endpoints,
      now: clock.now,
      wait: clock.wait,
      hooks: { onSucceeded, onFailed },
    });

    const ok = await runner.start({ draft: {} });
    expect(ok).toBe(false);
    expect(onSucceeded).not.toHaveBeenCalled();
    expect(onFailed).not.toHaveBeenCalled();
    expect(TERMINAL_STATUSES).toContain("superseded");
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
