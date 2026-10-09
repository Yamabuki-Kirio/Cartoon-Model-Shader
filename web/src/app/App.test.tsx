import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { AppError } from "../api/types";
import { makeTestWorkspace, settle } from "../testing/endpoints";
import { jobPayload } from "../testing/fixtures";
import type { JobState } from "../api/types";

afterEach(cleanup);

async function setup(overrides: Record<string, unknown> = {}) {
  const { workspace, endpoints } = makeTestWorkspace(overrides);
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

describe("工作台整体", () => {
  it("渲染顶部状态栏 / 三栏 / 底部操作栏", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    expect(getByTestId("status-bar")).toBeTruthy();
    expect(getByTestId("nav-panel")).toBeTruthy();
    expect(getByTestId("preview-pane")).toBeTruthy();
    expect(getByTestId("inspector")).toBeTruthy();
    expect(getByTestId("action-bar")).toBeTruthy();
  });

  it("状态栏显示连接、基线、结构与草稿数量", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    expect(getByTestId("status-blender").textContent).toContain("已连接");
    expect(getByTestId("status-baseline").textContent).toContain("bl0000000001");
    expect(getByTestId("status-structure").textContent).toContain("ffffffff");
    expect(getByTestId("status-dirty").textContent).toContain("0 项已改");
  });

  it("导航只列 Cel 分组，不生成伪控件", async () => {
    const { workspace } = await setup();
    const { getByTestId, queryByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    expect(getByTestId("nav-cel.Cel_Skin")).toBeTruthy();
    expect(getByTestId("nav-cel.Cel_Hair")).toBeTruthy();
    expect(getByTestId("nav-cel.Sakura_Hair_Reference")).toBeTruthy();
    // 未接入的参数族不该出现
    expect(queryByTestId("nav-cel.照明")).toBeNull();
  });

  it("导航的「仅已修改」筛选生效", async () => {
    const { workspace } = await setup();
    const { getByText, getByTestId, queryByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    fireEvent.click(getByText("仅已修改"));
    expect(queryByTestId("nav-cel.Cel_Skin")).toBeNull();

    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await settle(2);
    expect(getByTestId("nav-cel.Cel_Skin")).toBeTruthy();
  });

  it("结构失效时禁用「应用并预览」并显示横幅", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    workspace.actions.applyStructureFatal({ code: "STRUCTURE_CHANGED", message: "结构变了", retryable: true });
    await settle(2);
    expect(getByTestId("banner-structure")).toBeTruthy();
    expect((getByTestId("action-apply-preview") as HTMLButtonElement).disabled).toBe(true);
    expect(getByTestId("status-structure").textContent).toContain("已失效");
  });

  it("保存 / 应用到工程 / 正式渲染按钮只占位不发请求", async () => {
    const { workspace, endpoints } = await setup();
    const { getByText } = render(<App workspace={workspace} autoBootstrap={false} />);
    const savePreset = getByText("保存完整预设") as HTMLButtonElement;
    const commit = getByText("应用到工程") as HTMLButtonElement;
    expect(savePreset.disabled).toBe(true);
    expect(commit.disabled).toBe(true);
    fireEvent.click(savePreset);
    fireEvent.click(commit);
    await settle(2);
    // 只点按钮不会产生任何请求
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
  });

  it("预览图 URL 带防缓存参数", async () => {
    const { workspace } = await setup();
    const { container } = render(<App workspace={workspace} autoBootstrap={false} />);
    await workspace.actions.applyAndPreview();
    await settle();
    const image = container.querySelector("img.preview-image") as HTMLImageElement;
    expect(image.src).toContain("v=job0000000000001");
  });

  it("图片加载失败显示错误但不清空画面（保留最后成功预览）", async () => {
    const { workspace } = await setup();
    const { container, getByTestId, queryByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    await workspace.actions.applyAndPreview();
    await settle();
    const image = container.querySelector("img.preview-image") as HTMLImageElement;
    expect(image).toBeTruthy();

    fireEvent.error(image);
    await settle(2);
    expect(getByTestId("preview-image-error")).toBeTruthy();
    // 画面仍在（同一张 URL 还挂着）
    expect((container.querySelector("img.preview-image") as HTMLImageElement).src).toContain(
      "v=job0000000000001"
    );
    expect(queryByTestId("preview-job-status")?.textContent).toContain("done");
  });

  it("失败任务：横幅显示错误，画面保留", async () => {
    const failing = jobPayload({
      status: "failed",
      result: null,
      error: {
        code: "SURFACE_APPLY_FAILED",
        message: "参数写入失败（本次写入已被回滚）。",
        retryable: true,
        hint: "确认对象仍然存在",
        details: { restore: { attempted: true, verified: true } },
      },
    }) as unknown as JobState;
    let mode: "ok" | "fail" = "ok";
    const { workspace } = await setup({
      job: vi.fn(async () => (mode === "ok" ? (jobPayload() as unknown as JobState) : failing)),
    });
    const { container, getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    await workspace.actions.applyAndPreview();
    await settle();
    expect(container.querySelector("img.preview-image")).toBeTruthy();

    mode = "fail";
    await workspace.actions.applyAndPreview();
    await settle();
    expect(getByTestId("banner-error").textContent).toContain("SURFACE_APPLY_FAILED");
    expect(getByTestId("status-rollback").textContent).toContain("已恢复");
    // 画面还在
    expect(container.querySelector("img.preview-image")).toBeTruthy();
  });

  it("外部改动横幅列出被改过的取值", async () => {
    const external = [{ id: "cel.Cel_Skin.ramp[0].position" }];
    const { workspace } = await setup({
      job: vi.fn(async () =>
        jobPayload({
          external_changes: external,
          result: { ...jobPayload().result, external_changes: external },
        }) as unknown as JobState
      ),
    });
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    await workspace.actions.applyAndPreview();
    await settle();
    expect(getByTestId("banner-external").textContent).toContain("cel.Cel_Skin.ramp[0].position");
  });

  it("底部提示区分「未预览 / 已预览 / 画面过期 / 一致」", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    const hint = () => getByTestId("pending-hint").textContent ?? "";
    expect(hint()).toContain("一致");

    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "EASE" });
    await settle(2);
    // 还没渲染过 ⇒ 是「有未预览的修改」，不是「画面过期」
    expect(hint()).toContain("1 项已改");
    expect(hint()).toContain("未预览");
    expect(getByTestId("status-dirty").textContent).not.toContain("画面已过期");

    await workspace.actions.applyAndPreview();
    await settle();
    expect(hint()).toContain("已预览");

    // 预览之后又改（换成**不同**的值才算改）⇒ 画面过期
    workspace.actions.setValue("cel.Cel_Skin.ramp", { elements: [], interpolation: "CONSTANT" });
    await settle(2);
    expect(hint()).toContain("有待预览的 L1 修改");
    expect(getByTestId("status-dirty").textContent).toContain("画面已过期");

    workspace.actions.resetAllDraft();
    await settle(2);
    expect(hint()).toContain("一致");
  });

  it("建立基线后可切到基线图", async () => {
    const { workspace } = await setup();
    const { getByText, container } = render(<App workspace={workspace} autoBootstrap={false} />);
    // 走真实流程：点「刷新基线」（POST 会带回基线首张预览的 job_id）
    await workspace.actions.refreshBaseline();
    await settle(2);
    fireEvent.click(getByText("基线图"));
    await settle(2);
    const image = container.querySelector("img.preview-image") as HTMLImageElement;
    expect(image.src).toContain("/api/preview/base0");
    expect(image.src).toContain("v=");
  });

  it("A/B 与热力图只预留入口（禁用）", async () => {
    const { workspace } = await setup();
    const { getByText } = render(<App workspace={workspace} autoBootstrap={false} />);
    expect((getByText("A/B") as HTMLButtonElement).disabled).toBe(true);
    expect((getByText("热力图") as HTMLButtonElement).disabled).toBe(true);
  });

  it("卸载时停止轮询（dispose 打断在途任务）", async () => {
    const { workspace } = await setup();
    const { unmount } = render(<App workspace={workspace} />);
    await settle(2);
    const generation = workspace.runner.currentGeneration;
    unmount();
    // App 的清理函数会 dispose：代次前移，在途结果被丢弃
    expect(workspace.runner.currentGeneration).toBeGreaterThan(generation);
  });

  it("未构建前端时的 503 会给出构建指引", async () => {
    const { workspace } = await setup();
    const { getByTestId } = render(<App workspace={workspace} autoBootstrap={false} />);
    workspace.actions.handleError(
      new AppError(
        {
          code: "FRONTEND_NOT_BUILT",
          message: "前端尚未构建，无法提供页面。",
          retryable: false,
          hint: "请在 web/ 目录执行 npm ci 与 npm run build 后重试。",
        },
        503
      )
    );
    await settle(2);
    const banner = getByTestId("banner-error");
    expect(banner.textContent).toContain("FRONTEND_NOT_BUILT");
    expect(banner.textContent).toContain("npm run build");
  });
});
