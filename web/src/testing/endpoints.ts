/** 测试用的 endpoints 工厂：与 `WorkspaceEndpoints` 同形，全部可控。 */

import { vi } from "vitest";
import { parseSchema } from "../schema/parse";
import { baselinePayload, jobPayload, schemaPayload } from "./fixtures";
import { createWorkspace, type WorkspaceEndpoints } from "../state/workspace";
import type { JobState } from "../api/types";

export type TestEndpoints = WorkspaceEndpoints & {
  surfaceSchema: ReturnType<typeof vi.fn>;
  surfaceBaseline: ReturnType<typeof vi.fn>;
  createBaseline: ReturnType<typeof vi.fn>;
  submitPreview: ReturnType<typeof vi.fn>;
  job: ReturnType<typeof vi.fn>;
  blenderStatus: ReturnType<typeof vi.fn>;
  framingContext: ReturnType<typeof vi.fn>;
};

export function makeTestEndpoints(overrides: Partial<WorkspaceEndpoints> = {}): TestEndpoints {
  const endpoints: TestEndpoints = {
    surfaceSchema: vi.fn(async () => schemaPayload()),
    surfaceBaseline: vi.fn(async () => baselinePayload()),
    createBaseline: vi.fn(async () => ({
      ok: true as const,
      baseline_id: "bl0000000001",
      captured_at: "2026-10-09T18:00:00+08:00",
      blender: "5.2.1 LTS",
      surface: baselinePayload(),
      preview_url: "/api/preview/base0",
      job_id: "base0",
    })),
    submitPreview: vi.fn(async () => ({
      job_id: "job0000000000001",
      seq: 7,
      status: "queued",
    })),
    job: vi.fn(async () => jobPayload() as unknown as JobState),
    blenderStatus: vi.fn(async () => ({
      ok: true,
      status: "connected",
      target: "127.0.0.1:9876",
      checked_at: "2026-10-09T18:00:00+08:00",
      latency_ms: 12,
      error: null,
    })),
    framingContext: vi.fn(async () => ({
      ok: true as const,
      camera: "Camera",
      frame_current: 1,
      baseline: { established: true, stale: false },
      framing_modes: {
        default: "current_camera",
        modes: [
          { id: "current_camera", label: "当前相机预览", uses_temporary_camera: false },
          { id: "auto_headshot", label: "临时自动取景 · 头像", uses_temporary_camera: true },
        ],
      },
    })),
    parseSchema,
    ...overrides,
  } as TestEndpoints;
  return endpoints;
}

export function makeTestWorkspace(overrides: Partial<WorkspaceEndpoints> = {}) {
  const endpoints = makeTestEndpoints(overrides);
  const workspace = createWorkspace({ endpoints, pollIntervalMs: 0, now: () => 0 });
  return { workspace, endpoints };
}

/** 让在途任务跑完（wait 是 0ms，直接冲刷微任务队列）。 */
export async function settle(times = 12): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await Promise.resolve();
  }
}
