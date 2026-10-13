import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RenderWizard } from "./RenderWizard";
import { createRenderStore } from "../../state/render-store";
import { makeTestRenderEndpoints, renderJobFixture } from "../../testing/render-fixtures";
import type { RenderEndpoints } from "../../api/render-endpoints";
import type { RenderJob } from "../../api/render-types";

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

/**
 * 运行终态契约（规格 §3）。
 *
 * 关键不变量：四种终态的**文案与按钮必须互不相同**，且只有 success 显示成品区。
 * 这里逐态钉死，防止回归成「除 success 外一律渲染失败」。
 */
describe("一键渲染：四种运行终态", () => {
  async function runToTerminal(
    state: "success" | "rejected" | "diagnostic" | "failed",
    extra: Partial<RenderJob> = {},
  ) {
    const job = () =>
      renderJobFixture({
        state,
        is_terminal: true,
        step: 5,
        stage: "查看结果",
        ...extra,
      });
    const { controller } = setup({
      createJob: async () => job(),
      getJob: async () => job(),
      artifacts: async (jobId: string) => ({
        ok: true as const,
        job_id: jobId,
        artifacts: [{ artifact_id: "art-result.png", name: "result.png", size: 1024 }],
      }),
    });
    const view = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(view.getByTestId("render-start"));
    await settle();
    await settleTimers(8);
    return view;
  }

  it("SUCCESS：显示成品区、可交付标记与打开输出目录", async () => {
    const { getByTestId, queryByTestId } = await runToTerminal("success", {
      outcome: "success",
      shippable: true,
    });
    expect(getByTestId("render-result")).toBeTruthy();
    expect(getByTestId("render-shippable").textContent).toContain("可交付");
    expect(getByTestId("render-open-output")).toBeTruthy();
    // 成功态不显示「降级判定」横幅
    expect(queryByTestId("render-result-degraded")).toBeNull();
  });

  it("REJECTED：显示「需要材质确认」，绝不显示「渲染失败」", async () => {
    const { getByTestId, queryByTestId } = await runToTerminal("rejected", {
      outcome: "rejected",
      shippable: false,
      error_code: "RENDER_REJECTED",
      confirm_url: "http://127.0.0.1:8770/",
    });
    expect(getByTestId("render-result-rejected")).toBeTruthy();
    expect(getByTestId("render-result-rejected").textContent).toContain("需要材质确认");
    // 状态徽标必须是「需要材质确认」，不能是「渲染失败」
    expect(getByTestId("render-state").textContent).toContain("需要材质确认");
    expect(getByTestId("render-state").textContent).not.toContain("渲染失败");
    // 且不得出现失败结果区
    expect(queryByTestId("render-result-failed")).toBeNull();
    // 非成功态：不得出现成品区 / 可交付标记 / 打开输出目录
    expect(queryByTestId("render-result")).toBeNull();
    expect(queryByTestId("render-shippable")).toBeNull();
    expect(queryByTestId("render-open-output")).toBeNull();
    // 有确认入口与重试
    expect(getByTestId("render-result-confirm-link")).toBeTruthy();
    expect(getByTestId("render-retry")).toBeTruthy();
  });

  it("DIAGNOSTIC：明确「诊断图（不可交付）」，图被标为非成品", async () => {
    const { getByTestId, queryByTestId } = await runToTerminal("diagnostic", {
      outcome: "diagnostic",
      shippable: false,
      error_code: "RENDER_DIAGNOSTIC",
    });
    expect(getByTestId("render-result-diagnostic")).toBeTruthy();
    expect(getByTestId("render-result-diagnostic").textContent).toContain("不可交付");
    expect(getByTestId("render-state").textContent).toContain("诊断图");
    // 诊断图确实被列出，但每个都带「非成品」标注
    expect(getByTestId("render-diagnostic-artifacts").textContent).toContain("诊断图，非成品");
    expect(queryByTestId("render-result")).toBeNull();
    expect(queryByTestId("render-shippable")).toBeNull();
  });

  it("FAILED：显示错误码与重试，不展示裸 traceback", async () => {
    const { getByTestId, queryByTestId } = await runToTerminal("failed", {
      outcome: "failed",
      shippable: false,
      error_code: "RENDER_SUBPROCESS_FAILED",
    });
    expect(getByTestId("render-result-failed")).toBeTruthy();
    expect(getByTestId("render-result-error-code").textContent).toContain(
      "RENDER_SUBPROCESS_FAILED",
    );
    expect(getByTestId("render-retry")).toBeTruthy();
    expect(queryByTestId("render-result")).toBeNull();
    expect(queryByTestId("render-open-output")).toBeNull();
  });

  it("降级成功：额外提示这是兼容降级判定", async () => {
    const { getByTestId } = await runToTerminal("success", {
      outcome: "success",
      shippable: true,
      degraded: true,
    });
    expect(getByTestId("render-result")).toBeTruthy();
    expect(getByTestId("render-result-degraded")).toBeTruthy();
    expect(getByTestId("render-result-degraded").textContent).toContain("降级");
  });
});

/**
 * 取消策略（规格 §7）。
 *
 * 正式渲染阶段**不可取消**：按钮必须消失并换成说明文案，而不是发一个注定被拒的请求。
 */
describe("一键渲染：取消策略", () => {
  async function runTo(state: RenderJob["state"], extra: Partial<RenderJob> = {}) {
    const job = () => renderJobFixture({ state, step: 4, ...extra });
    const { controller, endpoints } = setup({
      createJob: async () => job(),
      getJob: async () => job(),
    });
    const view = render(<RenderWizard controller={controller} />);
    await settle();
    fireEvent.click(view.getByTestId("render-start"));
    await settle();
    await settleTimers(6);
    return { view, endpoints };
  }

  it("可取消阶段显示「取消任务」", async () => {
    const { view, endpoints } = await runTo("waiting_confirmation");
    expect(view.getByTestId("render-cancel")).toBeTruthy();
    fireEvent.click(view.getByTestId("render-cancel"));
    await settle();
    expect(endpoints.cancelJob).toHaveBeenCalledTimes(1);
  });

  it("正式渲染中：按钮禁用并说明原因，且不发起取消请求", async () => {
    const { view, endpoints } = await runTo("rendering");
    expect(view.queryByTestId("render-cancel")).toBeNull();
    const locked = view.getByTestId("render-cancel-locked");
    expect(locked.textContent).toContain("暂不支持取消");
    expect(locked.textContent).toContain("损坏工程");
    expect(endpoints.cancelJob).not.toHaveBeenCalled();
  });

  it("verifying 阶段同样不可取消", async () => {
    const { view } = await runTo("verifying");
    expect(view.queryByTestId("render-cancel")).toBeNull();
    expect(view.getByTestId("render-cancel-locked")).toBeTruthy();
  });
});

/**
 * 首次引导与环境诊断（规格 §5）。
 *
 * 关键不变量：环境未就绪时「开始渲染」必须**禁用并说明原因**，
 * 教程默认展开且能随时重开。
 */
describe("一键渲染：首次引导与环境诊断", () => {
  const blockedChecks = [
    {
      id: "blender_mcp",
      status: "error" as const,
      message: "Blender MCP 未连接",
      hint: "请在 Blender 里启动 MCP 服务，确认端口为 9876。",
    },
    {
      id: "source_blend",
      status: "error" as const,
      message: "尚未配置源工程 .blend",
      hint: "点「选择源工程」指定用于重建场景的 .blend。",
    },
  ];

  it("首次进入默认展示三步教程", async () => {
    const { controller } = setup();
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    const tour = getByTestId("render-tour");
    expect(tour.textContent).toContain("打开 Blender 测试工程");
    expect(tour.textContent).toContain("9876");
    expect(tour.textContent).toContain("检查环境并刷新模型列表");
  });

  it("教程可折叠，并可从右上角重开", async () => {
    const { controller } = setup();
    const { getByTestId, queryByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    fireEvent.click(getByTestId("render-tour-toggle"));
    await settle();
    expect(queryByTestId("render-tour")).toBeNull();

    fireEvent.click(getByTestId("render-tour-toggle"));
    await settle();
    expect(getByTestId("render-tour")).toBeTruthy();
  });

  it("环境未就绪：禁用开始渲染并逐项给出原因", async () => {
    const { controller } = setup({
      environment: async () => ({
        ok: true as const,
        ready: false,
        blockers: ["blender_mcp", "source_blend"],
        checks: blockedChecks,
      }),
    });
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    expect((getByTestId("render-start") as HTMLButtonElement).disabled).toBe(true);
    expect(getByTestId("render-start").textContent).toContain("暂不能开始");
    const blockers = getByTestId("render-environment-blockers");
    expect(blockers.textContent).toContain("Blender MCP 未连接");
    expect(blockers.textContent).toContain("9876");
    expect(blockers.textContent).toContain("尚未配置源工程");
    // 必须告诉用户下一步做什么，而不是只报错
    expect(getByTestId("render-start-disabled-reason")).toBeTruthy();
  });

  it("环境就绪：开始按钮可用，且不显示阻塞清单", async () => {
    const { controller } = setup({
      environment: async () => ({
        ok: true as const,
        ready: true,
        blockers: [],
        checks: [{ id: "blender_mcp", status: "ok" as const, message: "Blender MCP 已连接" }],
      }),
    });
    const { getByTestId, queryByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    expect((getByTestId("render-start") as HTMLButtonElement).disabled).toBe(false);
    expect(getByTestId("render-environment-summary").textContent).toContain("环境就绪");
    expect(queryByTestId("render-environment-blockers")).toBeNull();
  });

  it("warning 级检查不阻断渲染，但必须可见", async () => {
    const { controller } = setup({
      environment: async () => ({
        ok: true as const,
        ready: true,
        blockers: [],
        checks: [
          { id: "blender_mcp", status: "ok" as const, message: "Blender MCP 已连接" },
          {
            id: "active_jobs",
            status: "warning" as const,
            message: "当前有 1 个进行中的渲染任务",
            hint: "新任务会排队等待。",
          },
        ],
      }),
    });
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();

    expect((getByTestId("render-start") as HTMLButtonElement).disabled).toBe(false);
    expect(getByTestId("render-environment-warnings").textContent).toContain("排队等待");
  });

  it("重新检测环境会重新拉取诊断", async () => {
    const { controller, endpoints } = setup();
    const { getByTestId } = render(<RenderWizard controller={controller} />);
    await settle();
    await settle();
    const before = (endpoints.environment as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(getByTestId("render-environment-recheck"));
    await settle();
    expect((endpoints.environment as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
      before
    );
  });
});
