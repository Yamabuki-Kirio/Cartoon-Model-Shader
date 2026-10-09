import { useState } from "preact/hooks";
import { useWorkspace } from "../app/useWorkspace";
import type { Workspace } from "../state/workspace";

export type ZoomMode = "fit" | "one-to-one";

/** 给图片 URL 追加防缓存版本号；版本号取 URL 末段（就是 job_id）。 */
export function withCacheVersion(url: string): string {
  const token = url.split("/").filter(Boolean).pop() ?? "0";
  return `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(token)}`;
}

/**
 * 中央固定预览。
 *
 * 四条与正确性有关的规则：
 *
 * * 图片 URL 一定带 `v=<job_id>`（防缓存），否则浏览器会拿旧图 —— 调参时看起来「没反应」；
 * * **图片加载失败只显示错误，不清空 `lastSuccessfulPreview`**：失败画面不得替换最后一张成功预览；
 * * **基线图只在基线预览任务跑完之后才挂上去**：基线响应里就带 URL，但那时文件还没落盘；
 * * 基线图与最后成功预览图可切换，两者是两个不同的任务。
 */
export function PreviewCanvas({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const [zoom, setZoom] = useState<ZoomMode>("fit");
  const [source, setSource] = useState<"preview" | "baseline">("preview");
  const [imageError, setImageError] = useState<string | null>(null);

  const preview = state.lastSuccessfulPreview;
  const baselineUrl = state.baselinePreviewUrl
    ? withCacheVersion(state.baselinePreviewUrl)
    : null;
  const activeUrl = source === "baseline" && baselineUrl ? baselineUrl : preview?.url ?? null;

  // 终态任务不再占着 activeJob，所以「任务」一行要能回落到最近一次终态。
  const job = state.activeJob ?? state.lastJob;
  const jobLabel = state.activeJob
    ? `${state.activeJob.status}（${state.activeJob.steps.length} 步）`
    : state.lastJob
      ? `空闲（上次：${state.lastJob.status}）`
      : "空闲";

  return (
    <section class="preview-pane" data-testid="preview-pane">
      <div class="preview-toolbar">
        <div class="preview-sources">
          <button
            type="button"
            class={source === "preview" ? "chip chip-on" : "chip"}
            disabled={!preview}
            onClick={() => {
              setSource("preview");
              setImageError(null);
            }}
          >
            最后成功预览
          </button>
          <button
            type="button"
            class={source === "baseline" ? "chip chip-on" : "chip"}
            disabled={!baselineUrl}
            title={
              state.baselinePreviewStatus === "pending"
                ? "基线预览任务还没跑完，画面尚未落盘"
                : undefined
            }
            onClick={() => {
              setSource("baseline");
              setImageError(null);
            }}
          >
            基线图
            {state.baselinePreviewStatus === "pending" ? "（生成中）" : ""}
          </button>
        </div>
        <div class="preview-zoom">
          <button
            type="button"
            class={zoom === "fit" ? "chip chip-on" : "chip"}
            onClick={() => setZoom("fit")}
          >
            适应窗口
          </button>
          <button
            type="button"
            class={zoom === "one-to-one" ? "chip chip-on" : "chip"}
            onClick={() => setZoom("one-to-one")}
          >
            1:1
          </button>
        </div>
        <div class="preview-actions">
          <button type="button" class="chip" disabled title="A/B 对比将在后续提交开放">
            A/B
          </button>
          <button type="button" class="chip" disabled title="差异热力图将在后续提交开放">
            热力图
          </button>
        </div>
      </div>

      <div class="preview-stage" data-zoom={zoom}>
        {activeUrl ? (
          <img
            key={activeUrl}
            src={activeUrl}
            alt="预览"
            class={
              zoom === "fit" ? "preview-image preview-image-fit" : "preview-image preview-image-1x"
            }
            onLoad={() => setImageError(null)}
            onError={() => {
              // 只报错、不清 preview：最后一张成功画面必须留着。
              setImageError("图片加载失败（可能是任务刚结束、文件尚未落盘）。");
            }}
          />
        ) : state.baselinePreviewStatus === "pending" ? (
          <p class="muted pad" data-testid="preview-pending">
            正在生成基线预览…
          </p>
        ) : (
          <p class="muted pad">还没有预览图。点「刷新基线」建立基线并产出首张预览。</p>
        )}
      </div>

      <div class="preview-meta" data-testid="preview-meta">
        <span>
          取景：
          {String(
            (job?.framing_request as { mode_label?: string } | null)?.mode_label ??
              (preview?.framing as { mode_label?: string } | null)?.mode_label ??
              state.framingChoice.mode
          )}
        </span>
        <span>
          分辨率：
          {preview?.renderResolution ? preview.renderResolution.join(" × ") : "—"}
        </span>
        <span data-testid="preview-job-status">任务：{jobLabel}</span>
        {job?.steps?.length ? (
          <span class="muted" data-testid="preview-steps">
            {job.steps.join(" → ")}
          </span>
        ) : null}
      </div>

      {imageError ? (
        <p class="inline-error" data-testid="preview-image-error">
          {imageError}
        </p>
      ) : null}
      {state.baselinePreviewStatus === "failed" && !baselineUrl ? (
        <p class="inline-warn" data-testid="baseline-preview-failed">
          基线已建立，但首张基线画面未生成（基线预览任务失败或被取代）。可点「刷新基线」重试。
        </p>
      ) : null}
      {state.previewStale ? (
        <p class="inline-warn" data-testid="preview-stale">
          画面已过期：你在渲染期间又改过草稿。点「应用并预览」刷新画面。
        </p>
      ) : null}
    </section>
  );
}
