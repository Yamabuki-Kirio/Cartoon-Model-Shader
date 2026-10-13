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
    ...overrides,
  } as RenderEndpoints;
}
