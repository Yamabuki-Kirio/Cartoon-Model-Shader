import { useEffect } from "preact/hooks";
import { useStore } from "../../state/useStore";
import { Chip } from "../../components/chips";
import type { RenderStore } from "../../state/render-store";
import { RENDER_STEPS, isTerminalState, type RenderJob } from "../../api/render-types";

/**
 * 一键渲染向导：选择模型 → 材质分析 → 材质确认 → 渲染设置 → 执行渲染 → 查看结果。
 *
 * 材质确认由**固定脚本的本地确认页**完成（裁决 2=A）：这里只展示「还需确认 N 项」
 * 与确认页入口，确认后的续跑由服务端唯一持有，不在前端重复实现。
 */
export function RenderWizard({ controller }: { controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);

  useEffect(() => {
    controller.actions.bootstrap();
    return () => controller.actions.dispose();
  }, [controller]);

  const job = state.job;
  const step = controller.actions.stepOf(job);

  return (
    <section class="render-wizard" data-testid="render-wizard">
      <header class="wizard-head">
        <h2>一键渲染</h2>
        <button
          type="button"
          class="chip"
          data-testid="render-refresh-models"
          onClick={() => void controller.actions.refreshModels()}
        >
          刷新模型列表
        </button>
      </header>

      <Steps current={step} />

      {state.jobError ? (
        <div class="banner banner-error" data-testid="render-error">
          <b>错误：{state.jobError.code}</b>
          <div>{state.jobError.message}</div>
          {state.jobError.hint ? <div class="muted">{state.jobError.hint}</div> : null}
          <button type="button" class="chip" onClick={() => controller.actions.clearError()}>
            关闭
          </button>
        </div>
      ) : null}

      {state.notice ? (
        <div class="banner banner-info" data-testid="render-notice">
          {state.notice}
          <button type="button" class="chip" onClick={() => controller.actions.clearNotice()}>
            关闭
          </button>
        </div>
      ) : null}

      {!job ? <ModelStep controller={controller} /> : null}

      {job ? <ProgressStep job={job} controller={controller} /> : null}
    </section>
  );
}
function Steps({ current }: { current: number }) {
  return (
    <ol class="steps" data-testid="render-steps">
      {RENDER_STEPS.map((label, index) => (
        <li
          key={label}
          class={[
            "step",
            index === current ? "step-on" : "",
            index < current ? "step-done" : "",
          ]
            .filter(Boolean)
            .join(" ")}
          data-testid={`render-step-${index}`}
        >
          <span class="step-index">{index + 1}</span>
          <span class="step-label">{label}</span>
        </li>
      ))}
    </ol>
  );
}
function ModelStep({ controller }: { controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);
  return (
    <div class="wizard-body" data-testid="render-model-step">
      {state.modelsLoading ? <p class="muted">正在加载模型…</p> : null}
      {state.modelsError ? (
        <p class="bad">模型列表加载失败：{state.modelsError.message}</p>
      ) : null}
      {!state.modelsLoading && state.models.length === 0 ? (
        <p class="muted">受信任模型目录里还没有模型。请把 .pmx / .blend 放进模型根目录后点「刷新模型列表」。</p>
      ) : null}

      <ul class="model-list" data-testid="render-model-list">
        {state.models.map((model) => (
          <li key={model.model_id}>
            <button
              type="button"
              class={state.selectedModelId === model.model_id ? "model-item model-item-on" : "model-item"}
              data-testid={`model-pick-${model.model_id}`}
              onClick={() => controller.actions.selectModel(model.model_id)}
            >
              <span class="model-name">{model.name}</span>
              <span class="muted mono-sm">{model.fingerprint}</span>
            </button>
          </li>
        ))}
      </ul>

      <div class="settings-row" data-testid="render-settings">
        <label>
          <span>渲染模式</span>
          <select
            value={state.mode}
            data-testid="render-mode"
            onChange={(event) =>
              controller.actions.setMode(
                (event.currentTarget as HTMLSelectElement).value as "faithful" | "enhanced"
              )
            }
          >
            <option value="faithful">忠实（faithful）</option>
            <option value="enhanced">增强（enhanced）</option>
          </select>
        </label>
        <label class="checkbox">
          <input
            type="checkbox"
            checked={state.autoFrame}
            data-testid="render-auto-frame"
            onChange={(event) =>
              controller.actions.setAutoFrame((event.currentTarget as HTMLInputElement).checked)
            }
          />
          <span>自动构图</span>
        </label>
      </div>

      <div class="wizard-actions">
        <button
          type="button"
          class="primary"
          data-testid="render-start"
          disabled={!state.selectedModelId || state.busy}
          onClick={() => void controller.actions.startJob()}
        >
          {state.busy ? "正在创建任务…" : "开始渲染"}
        </button>
      </div>
    </div>
  );
}

const STATE_LABELS: Record<string, string> = {
  queued: "排队中",
  preflight: "预检中",
  analyzing_materials: "材质分析中",
  waiting_confirmation: "等待材质确认",
  preparing: "准备渲染",
  rendering: "渲染中",
  verifying: "校验产物",
  success: "渲染成功",
  failed: "渲染失败",
  rejected: "已被拒绝",
  diagnostic: "仅诊断",
  cancelled: "已取消",
};

function ProgressStep({ job, controller }: { job: RenderJob; controller: RenderStore }) {
  const terminal = isTerminalState(job.state);
  const waiting = job.state === "waiting_confirmation";

  return (
    <div class="wizard-body" data-testid="render-progress">
      <div class="progress-head" data-testid="render-state">
        <Chip
          tone={job.state === "success" ? "ok" : terminal ? "bad" : "info"}
          label={STATE_LABELS[job.state] ?? job.state}
        />
        <span class="muted">
          {job.model_name} · {job.fingerprint}
        </span>
      </div>

      {waiting ? (
        <div class="banner banner-warn" data-testid="render-confirm-required">
          <b>有 {job.required_count} 个材质需要人工确认</b>
          <div class="muted">
            确认由固定脚本的本地确认页完成；全部确认后服务端会自动继续渲染，无需在此重复操作。
          </div>
          {job.confirm_url ? (
            <a
              class="chip"
              href={job.confirm_url}
              target="_blank"
              rel="noreferrer"
              data-testid="render-confirm-link"
            >
              打开确认页
            </a>
          ) : null}
        </div>
      ) : null}

      {job.materials.length > 0 ? (
        <table class="material-table" data-testid="render-materials">
          <thead>
            <tr>
              <th>材质</th>
              <th>建议分类</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {job.materials.map((item) => (
              <tr key={item.material}>
                <td class="mono-sm">{item.material}</td>
                <td>{item.proposed_class ?? "—"}</td>
                <td>{item.confirmed ? "已确认" : item.requires_user ? "待确认" : "自动"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {job.logs && job.logs.length > 0 ? (
        <pre class="render-logs" data-testid="render-logs">
          {job.logs.join("\n")}
        </pre>
      ) : null}

      {terminal ? <ResultStep job={job} controller={controller} /> : null}

      <div class="wizard-actions">
        {!terminal ? (
          <button
            type="button"
            class="chip"
            data-testid="render-cancel"
            onClick={() => void controller.actions.cancelJob()}
          >
            取消任务
          </button>
        ) : (
          <button
            type="button"
            class="chip"
            data-testid="render-retry"
            onClick={() => void controller.actions.retryJob()}
          >
            重新渲染
          </button>
        )}
      </div>
    </div>
  );
}

function ResultStep({ job, controller }: { job: RenderJob; controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);
  if (job.state !== "success") {
    return (
      <div class="banner banner-error" data-testid="render-result-failed">
        本次未产出可交付结果（{STATE_LABELS[job.state] ?? job.state}）。日志与输出目录已保留，可修正后重试。
      </div>
    );
  }
  return (
    <div class="render-result" data-testid="render-result">
      <h3>渲染结果</h3>
      {state.artifacts.length === 0 ? (
        <p class="muted">尚未发现产物文件。</p>
      ) : (
        <ul class="artifact-list">
          {state.artifacts.map((artifact) => (
            <li key={artifact.artifact_id}>
              <a
                href={controller.actions.artifactUrl(job.job_id, artifact.artifact_id)}
                target="_blank"
                rel="noreferrer"
                data-testid={`render-artifact-${artifact.artifact_id}`}
              >
                {artifact.name}
              </a>
              <span class="muted"> {Math.round(artifact.size / 1024)} KB</span>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        class="chip"
        data-testid="render-open-output"
        onClick={() => void controller.actions.openOutput()}
      >
        打开输出目录
      </button>
    </div>
  );
}
