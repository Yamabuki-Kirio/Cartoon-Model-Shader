/** 渲染工作流测试夹具：与后端响应同形，全部可控且自洽。 */

import { vi } from "vitest";
import type { RenderEndpoints } from "../api/render-endpoints";
import type { RenderJob, RenderModel } from "../api/render-types";

export function modelFixture(overrides: Partial<RenderModel> = {}): RenderModel {
  return {
    model_id: "model-3f7a91c4d20b5e68",
    name: "示例模型",
    fingerprint: "3f7a91c4d20b5e68",
    material_count: 3,
    ...overrides,
  };
}

export function renderJobFixture(overrides: Partial<RenderJob> = {}): RenderJob {
  // 终态契约字段按 state 推导默认值：只有 success 默认可交付。
  // 这样既有用例（只传 state）不会被新字段破坏，而新用例又能显式覆盖。
  const state = overrides.state ?? "queued";
  const outcome: RenderJob["outcome"] =
    state === "success" || state === "rejected" || state === "diagnostic" || state === "failed"
      ? state
      : null;
  return {
    ok: true,
    job_id: "rj-3f7a91c4d20b-5e68",
    model_id: "model-3f7a91c4d20b5e68",
    model_name: "示例模型",
    fingerprint: "3f7a91c4d20b5e68",
    state: "queued",
    step: 0,
    stage: "选择模型",
    step_count: 6,
    settings: { mode: "faithful", auto_frame: false },
    created_at: "2026-10-13T10:00:00",
    updated_at: "2026-10-13T10:00:00",
    is_terminal: false,
    materials: [],
    confirm_url: null,
    confirmed_count: 0,
    required_count: 0,
    pinned: false,
    disk_bytes: 0,
    retention_state: "kept",
    outcome,
    shippable: state === "success",
    error_code: null,
    degraded: false,
    cancel_reason: null,
    logs: [],
    artifacts: [],
    ...overrides,
  };
}

export function makeTestRenderEndpoints(overrides: Partial<RenderEndpoints> = {}): RenderEndpoints {
  return {
    listModels: vi.fn(async () => ({ ok: true as const, models: [modelFixture()] })),
    refreshModels: vi.fn(async () => ({ ok: true as const, models: [modelFixture()] })),
    createJob: vi.fn(async () => renderJobFixture()),
    listJobs: vi.fn(async () => [renderJobFixture()]),
    getJob: vi.fn(async (jobId: string) => renderJobFixture({ job_id: jobId })),
    retryJob: vi.fn(async (jobId: string) => renderJobFixture({ job_id: jobId, state: "queued" })),
    cancelJob: vi.fn(async (jobId: string) => renderJobFixture({ job_id: jobId, state: "cancelled" })),
    materials: vi.fn(async (jobId: string) => ({
      ok: true as const,
      job_id: jobId,
      materials: [],
      confirmed_count: 0,
      required_count: 0,
      confirm_url: null,
      waiting: false,
    })),
    artifacts: vi.fn(async (jobId: string) => ({
      ok: true as const,
      job_id: jobId,
      artifacts: [],
    })),
    artifactUrl: (jobId: string, artifactId: string) =>
      `/api/render/jobs/${jobId}/artifacts/${artifactId}`,
    openOutput: vi.fn(async () => ({ ok: true as const, opened: true })),
    pinJob: vi.fn(async (jobId: string, pinned: boolean) =>
      renderJobFixture({ job_id: jobId, pinned, retention_state: pinned ? "pinned" : "kept" })
    ),
    storage: vi.fn(async () => ({
      ok: true as const,
      job_count: 0,
      pinned_count: 0,
      total_bytes: 0,
      max_total_bytes: 20 * 1024 ** 3,
      max_jobs: 30,
      max_age_days: 14,
      evictable_jobs: 0,
      reclaimable_bytes: 0,
      roots: ["out"],
    })),
    cleanupStorage: vi.fn(async (dryRun: boolean) => ({
      ok: true as const,
      dry_run: dryRun,
      removed_jobs: [],
      released_bytes: 0,
      failed: [],
    })),
    ...overrides,
  } as RenderEndpoints;
}
