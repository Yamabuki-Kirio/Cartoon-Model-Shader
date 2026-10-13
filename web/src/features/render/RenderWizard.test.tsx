import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { RenderWizard } from "./RenderWizard";
import { createRenderStore } from "../../state/render-store";
import { makeTestRenderEndpoints, renderJobFixture } from "../../testing/render-fixtures";
import type { RenderEndpoints } from "../../api/render-endpoints";

afterEach(cleanup);

/** 冲刷微任务（Promise 链）。 */
async function settle(times = 16): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await Promise.resolve();
  }
}

/** 冲刷宏任务（setTimeout 级轮询）。 */
async function settleTimers(times = 10): Promise<void> {
  for (let index = 0; index < times; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function setup(overrides: Partial<RenderEndpoints> = {}) {
  const endpoints = makeTestRenderEndpoints(overrides);
  const controller = createRenderStore({ endpoints, pollIntervalMs: 0 });
  return { controller, endpoints };
}

describe("一键渲染向导", () => {
  it("渲染 6 步与模型列表，自动选中首个模型后开始按钮可用", async () => {
    const { controller } = setup();
    const { getByTestId, queryAllByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    expect(getByTestId("render-steps")).toBeTruthy();
    expect(queryAllByTestId(/^render-step-\d$/)).toHaveLength(6);
    expect(getByTestId("render-model-list")).toBeTruthy();
    expect(getByTestId("model-pick-model-3f7a91c4d20b5e68")).toBeTruthy();
    expect((getByTestId("render-start") as HTMLButtonElement).disabled).toBe(false);
  });

  it("开始渲染：轮询到 success，展示结果与产物", async () => {
    let calls = 0;
    const { controller, endpoints } = setup({
      getJob: async (jobId: string) => {
        calls += 1;
        const state = calls < 3 ? "rendering" : "success";
        return renderJobFixture({
          job_id: jobId,
          state,
          is_terminal: state === "success",
          step: state === "success" ? 5 : 4,
        });
      },
      artifacts: async (jobId: string) => ({
        ok: true as const,
        job_id: jobId,
        artifacts: [{ artifact_id: "art-result.png", name: "result.png", size: 2048 }],
      }),
    });

    const { getByTestId, queryByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(getByTestId("render-start"));
    await settle();
    await settleTimers(12);

    expect(endpoints.createJob).toHaveBeenCalledTimes(1);
    expect(queryByTestId("render-result")).toBeTruthy();
    expect(getByTestId("render-artifact-art-result.png")).toBeTruthy();
  });

  it("等待确认状态：展示确认入口与材质表，不谎称完成", async () => {
    const waiting = (jobId: string) =>
      renderJobFixture({
        job_id: jobId,
        state: "waiting_confirmation",
        step: 2,
        stage: "材质确认",
        required_count: 2,
        confirm_url: "http://127.0.0.1:8770/",
        materials: [
          { material: "皮肤_脸", proposed_class: "skin", requires_user: true, confirmed: false },
          { material: "头发", proposed_class: "hair", requires_user: true, confirmed: false },
        ],
      });
    const { controller } = setup({
      createJob: async () => waiting("rj-000000000001"),
      getJob: async (jobId: string) => waiting(jobId),
    });

    const { getByTestId, queryByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(getByTestId("render-start"));
    await settle();
    await settleTimers(6);

    expect(getByTestId("render-confirm-required")).toBeTruthy();
    expect(getByTestId("render-confirm-link")).toBeTruthy();
    expect(getByTestId("render-state").textContent).toContain("等待材质确认");
    expect(getByTestId("render-materials")).toBeTruthy();
    // 未完成：不应出现结果区
    expect(queryByTestId("render-result")).toBeNull();
  });

  it("失败任务：给出失败横幅与重试按钮，不显示结果", async () => {
    const failed = (jobId: string) =>
      renderJobFixture({ job_id: jobId, state: "failed", is_terminal: true, step: 5 });
    const { controller } = setup({
      createJob: async () => failed("rj-000000000001"),
      getJob: async (jobId: string) => failed(jobId),
    });

    const { getByTestId, queryByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(getByTestId("render-start"));
    await settle();
    await settleTimers(6);

    expect(queryByTestId("render-result")).toBeNull();
    expect(getByTestId("render-result-failed")).toBeTruthy();
    expect(getByTestId("render-retry")).toBeTruthy();
  });

  it("模型为空时给出引导，不生成伪模型项", async () => {
    const { controller } = setup({
      listModels: async () => ({ ok: true as const, models: [] }),
    });
    const { getByTestId, queryAllByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    expect(getByTestId("render-model-list").children).toHaveLength(0);
    // 精确选择器：只匹配模型项，不误伤 list / step 容器
    expect(queryAllByTestId(/^model-pick-/)).toHaveLength(0);
    expect((getByTestId("render-start") as HTMLButtonElement).disabled).toBe(true);
  });

  it("展示输出总容量与可清理空间，预演清理调用 dry_run", async () => {
    const { controller, endpoints } = setup({
      storage: async () => ({
        ok: true as const,
        job_count: 7,
        pinned_count: 2,
        total_bytes: 3 * 1024 * 1024,
        max_total_bytes: 20 * 1024 ** 3,
        max_jobs: 30,
        max_age_days: 14,
        evictable_jobs: 3,
        reclaimable_bytes: 2 * 1024 * 1024,
        roots: ["out"],
      }),
    });
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    await settle();

    expect(getByTestId("storage-total").textContent).toContain("3");
    expect(getByTestId("storage-reclaimable").textContent).toContain("3");
    fireEvent.click(getByTestId("storage-cleanup-preview"));
    await settle();
    expect(endpoints.cleanupStorage).toHaveBeenCalledWith(true);
  });

  it("成功结果展示产物大小、保留状态与固定开关", async () => {
    const { controller, endpoints } = setup({
      createJob: async () =>
        renderJobFixture({ state: "success", is_terminal: true, step: 5, disk_bytes: 4096 }),
      getJob: async (jobId: string) =>
        renderJobFixture({ job_id: jobId, state: "success", is_terminal: true, step: 5, disk_bytes: 4096 }),
      artifacts: async (jobId: string) => ({
        ok: true as const,
        job_id: jobId,
        artifacts: [{ artifact_id: "art-result.png", name: "result.png", size: 4096 }],
      }),
    });
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(getByTestId("render-start"));
    await settle();
    await settleTimers(12);

    expect(getByTestId("render-disk-bytes").textContent).toContain("4.0 KB");
    expect(getByTestId("render-retention")).toBeTruthy();
    fireEvent.click(getByTestId("render-pin"));
    await settle();
    expect(endpoints.pinJob).toHaveBeenCalledWith(expect.any(String), true);
  });
});
