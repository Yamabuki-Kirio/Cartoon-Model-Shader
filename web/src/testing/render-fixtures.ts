/** 渲染工作流测试夹具：与后端响应同形，全部可控且自洽。 */

import { vi } from "vitest";
import type { RenderEndpoints } from "../api/render-endpoints";
import type { RenderJob, RenderModel, RenderScheme } from "../api/render-types";

export function modelFixture(overrides: Partial<RenderModel> = {}): RenderModel {
  return {
    model_id: "model-3f7a91c4d20b5e68",
    name: "示例模型",
    fingerprint: "3f7a91c4d20b5e68",
    material_count: 3,
    ...overrides,
  };
}

/**
 * 方案夹具（PR #8）：形状与 `GET /api/render/schemes` 一致。
 *
 * 默认给两个方案 —— 经典（无参数）与体积雾（1 个可调 + 4 个只读），
 * 用来覆盖「切换方案保留各自草稿」与「只读项不提交」两条规格。
 */
export function schemeFixture(overrides: Partial<RenderScheme> = {}): RenderScheme {
  return {
    id: "volume_fog",
    version: 1,
    label: "体积雾灯光",
    description: "7 灯冷暖 + 程序化体积雾，按模型包围盒缩放。",
    recommended: true,
    recommended_use: "需要氛围感与体积光的角色特写。",
    has_volume_fog: true,
    performance_note: "体积雾会增加渲染时间，低配机器建议先用经典方案。",
    adjustable_count: 1,
    parameters: [
      {
        id: "fog_density",
        kind: "number",
        label: "雾密度",
        default: 0.018,
        description: "体积雾浓度。数值越大雾越厚。",
        readonly: false,
        minimum: 0,
        maximum: 0.1,
        step: 0.001,
      },
      {
        id: "light_intensity",
        kind: "number",
        label: "灯光强度",
        default: 1.0,
        description: "未经真实 Blender 标定，暂不开放调节。",
        readonly: true,
      },
    ],
    ...overrides,
  };
}

export function schemeFixtures(): RenderScheme[] {
  return [
    schemeFixture(),
    schemeFixture({
      id: "classic",
      version: 1,
      label: "经典灯光",
      description: "旧版 21+6 灯复刻，不含体积雾。",
      recommended: false,
      recommended_use: "需要与历史产物一致的复现渲染。",
      has_volume_fog: false,
      performance_note: "灯数多但无体积雾，渲染时间与历史版本一致。",
      adjustable_count: 0,
      parameters: [],
    }),
  ];
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
    // PR #8：任务记录使用的方案；旧任务为 null。
    scheme: null,
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
    environment: vi.fn(async () => ({
      ok: true as const,
      ready: true,
      blockers: [],
      checks: [],
    })),
    listSchemes: vi.fn(async () => ({
      ok: true as const,
      default: "volume_fog",
      schemes: schemeFixtures(),
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
