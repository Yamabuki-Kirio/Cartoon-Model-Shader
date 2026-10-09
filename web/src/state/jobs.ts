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

export interface JobRunnerHooks {
  /** 任务状态有更新（含排队中）。 */
  onUpdate?: (job: JobState) => void;
  onSucceeded?: (job: JobState, result: JobResult) => void;
  onFailed?: (job: JobState, error: JobError) => void;
  onCancelled?: (reason: string) => void;
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
    const hadActive = this.abortActive();
    const controller = new AbortController();
    this.controller = controller;
    const generation = ++this.generation;
    const startedAt = this.now();
    if (hadActive) {
      this.hooks.onCancelled?.("提交新任务");
    }

    let jobId: string;
    try {
      const submitted = await this.endpoints.submitPreview(payload, { signal: controller.signal });
      if (!this.isCurrent(generation)) {
        return false;
      }
      jobId = submitted.job_id;
    } catch (error) {
      if (isAbortError(error) || !this.isCurrent(generation)) {
        return false;
      }
      this.fail(
        generation,
        {
          job_id: "",
          seq: 0,
          status: "failed",
          created_at: "",
          updated_at: "",
          superseded: false,
          steps: [],
          error: null,
        },
        toJobError(error)
      );
      return false;
    }

    for (;;) {
      if (!this.isCurrent(generation)) {
        return false;
      }
      if (this.now() - startedAt > this.timeoutMs) {
        this.fail(
          generation,
          {
            job_id: jobId,
            seq: 0,
            status: "running",
            created_at: "",
            updated_at: "",
            superseded: false,
            steps: [],
            error: null,
          },
          {
            code: "JOB_TIMEOUT",
            message: `任务在 ${Math.round(this.timeoutMs / 1000)} 秒内没有结束，已停止轮询。`,
            retryable: true,
          }
        );
        return false;
      }

      let job: JobState;
      try {
        job = await this.endpoints.job(jobId, { signal: controller.signal });
      } catch (error) {
        if (isAbortError(error) || !this.isCurrent(generation)) {
          return false;
        }
        this.fail(
          generation,
          {
            job_id: jobId,
            seq: 0,
            status: "failed",
            created_at: "",
            updated_at: "",
            superseded: false,
            steps: [],
            error: null,
          },
          toJobError(error)
        );
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
          this.hooks.onSucceeded?.(job, job.result ?? ({} as JobResult));
        } else if (job.status === "failed") {
          this.fail(
            generation,
            job,
            job.error ?? {
              code: "INTERNAL_ERROR",
              message: "任务失败但服务端没有给出原因。",
              retryable: true,
            }
          );
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

  private fail(generation: number, job: JobState, error: JobError): void {
    if (!this.isCurrent(generation)) {
      return;
    }
    this.hooks.onFailed?.(job, error);
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
