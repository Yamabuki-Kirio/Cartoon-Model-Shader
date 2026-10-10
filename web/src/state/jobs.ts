/**
 * 预览任务的前端调度：提交 → 轮询 → 终态分发。
 *
 * 为什么需要「代次」（generation）而不只是一个 AbortController：
 *
 * * 取消只能打断 **在途请求**；而一个已经返回、正在被 await 链处理的旧响应，
 *   仍可能在取消之后走到 `onSucceeded`。届时代次已经变了，我们据此丢弃它。
 *
 * 因此每个终态回调前都检查 `isCurrent(generation)`。「旧任务结果不得更新当前画面或状态」
 * 这条靠的就是它，而不是靠 abort 的时序运气。
 *
 * 另注意：`onFailed` **不清除**最后一张成功预览 —— 那是调用方的责任，
 * 本模块只负责把终态如实分发出去。
 */

import { isAbortError, AppError, type JobError, type JobResult, type JobState } from "../api/types";

export interface JobSubmitPayload {
  draft: Record<string, unknown>;
  framing?: { mode: string; margin: number };
  expected_structure_hash?: string;
}

interface EndpointsLike {
  submitPreview(
    payload: JobSubmitPayload,
    options?: { signal?: AbortSignal }
  ): Promise<{ job_id: string; seq: number; status: string }>;
  job(jobId: string, options?: { signal?: AbortSignal }): Promise<JobState>;
}

export type JobOutcome = "succeeded" | "failed" | "superseded";

export interface JobRunnerHooks {
  /** 任务状态有更新（含排队中）。**终态也会走这里**，所以不要据此认定它还在跑。 */
  onUpdate?: (job: JobState) => void;
  onSucceeded?: (job: JobState, result: JobResult) => void;
  onFailed?: (job: JobState, error: JobError) => void;
  onCancelled?: (reason: string) => void;
  /**
   * **统一终态清理**：任何终态（成功 / 失败 / 被取代 / 超时 / 提交失败）都**恰好触发一次**，
   * 且一定排在 `onSucceeded` / `onFailed` 之后。
   *
   * 为什么需要它：终态任务不再是「活动任务」。少了这一步，`done` / `superseded` 的任务
   * 会一直挂在调用方的 `activeJob` 上 —— 界面显示「渲染中」，实际早已结束；
   * 下一次「清空活动任务」也无从谈起（没人知道该在哪清）。
   */
  onTerminal?: (job: JobState, outcome: JobOutcome) => void;
}

export interface JobRunnerOptions {
  endpoints: EndpointsLike;
  hooks?: JobRunnerHooks;
  pollIntervalMs?: number;
  timeoutMs?: number;
  /** 注入时钟与等待，便于测试。 */
  now?: () => number;
  wait?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export const TERMINAL_STATUSES: readonly string[] = ["done", "failed", "superseded"];

/** 终态或异常路径上用来占位的任务壳（服务端没给出完整对象时）。 */
function stubJob(jobId: string, status: JobState["status"]): JobState {
  return {
    job_id: jobId,
    seq: 0,
    status,
    created_at: "",
    updated_at: "",
    superseded: false,
    steps: [],
    error: null,
  };
}

function defaultWait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export class JobRunner {
  private generation = 0;
  private controller: AbortController | null = null;
  private readonly pollIntervalMs: number;
  private readonly timeoutMs: number;
  private readonly now: () => number;
  private readonly wait: (ms: number, signal: AbortSignal) => Promise<void>;
  private readonly endpoints: EndpointsLike;
  private hooks: JobRunnerHooks;

  constructor(options: JobRunnerOptions) {
    this.endpoints = options.endpoints;
    this.hooks = options.hooks ?? {};
    this.pollIntervalMs = options.pollIntervalMs ?? 250;
    this.timeoutMs = options.timeoutMs ?? 120_000;
    this.now = options.now ?? (() => Date.now());
    this.wait = options.wait ?? defaultWait;
  }

  setHooks(hooks: JobRunnerHooks): void {
    this.hooks = hooks;
  }

  get currentGeneration(): number {
    return this.generation;
  }

  isCurrent(generation: number): boolean {
    return generation === this.generation;
  }

  /** 打断在途请求（不递增代次）。返回此前是否有活动任务。 */
  private abortActive(): boolean {
    const hadActive = this.controller !== null;
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    return hadActive;
  }

  /**
   * 释放「活动任务」标志。
   *
   * 这一条属于**终态清理**：任务已经结束，就不该再被当成活动任务 ——
   * 否则下一次提交会把一个早已完结的任务误判成「有旧任务要取代」，
   * `onCancelled` 会被无端触发（调用方据此清状态、报「已被取代」，全是假的）。
   */
  private releaseActive(controller: AbortController): void {
    if (this.controller === controller) {
      this.controller = null;
    }
  }

  /** 取消在途任务：递增代次 + 打断请求。旧响应回来也会被代次挡掉。 */
  cancel(reason = "被更新的任务取代"): void {
    const hadActive = this.abortActive();
    this.generation += 1;
    if (hadActive) {
      this.hooks.onCancelled?.(reason);
    }
  }

  /** 组件卸载时调用：停止轮询，丢弃一切在途结果。 */
  dispose(): void {
    this.generation += 1;
    this.abortActive();
  }

  /**
   * 提交并轮询。**会先取消上一次**——预览是「后一次取代前一次」的语义。
   * 返回是否顺利走到终态（`false` = 被取消 / 被取代 / 出错）。
   */
  async start(payload: JobSubmitPayload): Promise<boolean> {
    // 代次**只递增一次**：先打断旧请求，再取新代次。若走 `cancel()` 再自增，
    // 一次提交会推进两次，「第几代」与「第几次提交」就不是一对一了。
    const handled = this.begin();
    try {
      let jobId: string;
      try {
        const submitted = await this.endpoints.submitPreview(payload, {
          signal: handled.controller.signal,
        });
        if (!this.isCurrent(handled.generation)) {
          return false;
        }
        jobId = submitted.job_id;
      } catch (error) {
        if (isAbortError(error) || !this.isCurrent(handled.generation)) {
          return false;
        }
        this.settle(handled.generation, stubJob("", "failed"), "failed", toJobError(error));
        return false;
      }
      // 轮询阶段抛出的非取消错误**照旧向上传播**（那是内部不变量出错，不该被悄悄吞掉）。
      return await this.watch(jobId, handled);
    } finally {
      this.releaseActive(handled.controller);
    }
  }

  /**
   * 跟踪一个**已经提交过**的任务。
   *
   * 建立基线就是这种情况：`POST /api/session/baseline` 自己会创建「首张基线预览」任务，
   * 前端只拿到 `job_id`，并没有提交动作 —— 但它同样需要排队/轮询/终态清理，
   * 否则基线图会在文件还没落盘时就挂上去（表现为 404 或上一张旧图）。
   */
  async adopt(jobId: string): Promise<boolean> {
    const handled = this.begin();
    try {
      return await this.watch(jobId, handled);
    } finally {
      this.releaseActive(handled.controller);
    }
  }

  /** 开启一次新的跟踪：打断上一个、取新代次、记下起始时刻。 */
  private begin(): { generation: number; controller: AbortController; startedAt: number } {
    const hadActive = this.abortActive();
    const controller = new AbortController();
    this.controller = controller;
    const generation = ++this.generation;
    const startedAt = this.now();
    if (hadActive) {
      this.hooks.onCancelled?.("提交新任务");
    }
    return { generation, controller, startedAt };
  }

  /** 轮询直到终态；终态一律经 `settle` 分发，保证清理只在一处发生。 */
  private async watch(
    jobId: string,
    handled: { generation: number; controller: AbortController; startedAt: number }
  ): Promise<boolean> {
    const { generation, controller, startedAt } = handled;
    for (;;) {
      if (!this.isCurrent(generation)) {
        return false;
      }
      if (this.now() - startedAt > this.timeoutMs) {
        this.settle(generation, stubJob(jobId, "running"), "failed", {
          code: "JOB_TIMEOUT",
          message: `任务在 ${Math.round(this.timeoutMs / 1000)} 秒内没有结束，已停止轮询。`,
          retryable: true,
        });
        return false;
      }

      let job: JobState;
      try {
        job = await this.endpoints.job(jobId, { signal: controller.signal });
      } catch (error) {
        if (isAbortError(error) || !this.isCurrent(generation)) {
          return false;
        }
        this.settle(generation, stubJob(jobId, "failed"), "failed", toJobError(error));
        return false;
      }

      // 代次检查放在**分发之前**：旧响应即使侥幸返回，也不会写进状态。
      if (!this.isCurrent(generation)) {
        return false;
      }
      this.hooks.onUpdate?.(job);

      if (TERMINAL_STATUSES.includes(job.status)) {
        if (!this.isCurrent(generation)) {
          return false;
        }
        if (job.status === "done") {
          this.settle(generation, job, "succeeded");
        } else if (job.status === "failed") {
          this.settle(
            generation,
            job,
            "failed",
            job.error ?? {
              code: "INTERNAL_ERROR",
              message: "任务失败但服务端没有给出原因。",
              retryable: true,
            }
          );
        } else {
          // `superseded` 同样是终态：以前这条分支什么都不做，
          // 于是被取代的任务会一直挂在调用方的「活动任务」上。
          this.settle(generation, job, "superseded");
        }
        return job.status === "done";
      }

      try {
        await this.wait(this.pollIntervalMs, controller.signal);
      } catch (error) {
        if (isAbortError(error) || !this.isCurrent(generation)) {
          return false;
        }
        throw error;
      }
    }
  }

  /**
   * 分发终态并做**统一清理**：先给结果回调，再通知终态。
   *
   * 所有终态路径（成功 / 失败 / 被取代 / 超时 / 提交失败 / 轮询报错）都收敛到这里 ——
   * 分散处理时漏掉任何一条，界面就会停在「还在跑」的假象上。
   */
  private settle(
    generation: number,
    job: JobState,
    outcome: JobOutcome,
    error?: JobError
  ): void {
    if (!this.isCurrent(generation)) {
      return;
    }
    if (outcome === "succeeded") {
      this.hooks.onSucceeded?.(job, job.result ?? ({} as JobResult));
    } else if (outcome === "failed") {
      this.hooks.onFailed?.(
        job,
        error ?? {
          code: "INTERNAL_ERROR",
          message: "任务失败但服务端没有给出原因。",
          retryable: true,
        }
      );
    }
    this.hooks.onTerminal?.(job, outcome);
  }
}

function toJobError(error: unknown): JobError {
  if (error instanceof AppError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      hint: error.hint,
    };
  }
  return {
    code: "INTERNAL_ERROR",
    message: error instanceof Error ? error.message : String(error),
    retryable: true,
  };
}
