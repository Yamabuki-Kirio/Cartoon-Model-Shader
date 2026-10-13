import { useEffect } from "preact/hooks";
import { useStore } from "../../state/useStore";
import { Chip } from "../../components/chips";
import { formatBytes, type RenderStore } from "../../state/render-store";
import {
  RENDER_CANCEL_LOCKED_MESSAGE,
  RENDER_ONBOARDING_STEPS,
  RENDER_STATE_LABELS,
  RENDER_STEPS,
  RENDER_TERMINAL_LABELS,
  canCancelJob,
  isShippableJob,
  isTerminalState,
  type RenderJob,
} from "../../api/render-types";

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
        <button
          type="button"
          class="chip"
          data-testid="render-tour-toggle"
          onClick={() => controller.actions.setTourOpen(!state.tourOpen)}
        >
          {state.tourOpen ? "收起教程" : "第一次使用？"}
        </button>
      </header>

      <Onboarding controller={controller} />

      <EnvironmentPanel controller={controller} />

      <Steps current={step} />

      <StoragePanel controller={controller} />

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
/**
 * 首次使用教程（规格 §5.2）。
 *
 * 三步固定文案；首次进入默认展开，可折叠，也能从右上角随时重开 ——
 * 新用户不该被要求先去读命令行文档。
 */
function Onboarding({ controller }: { controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);
  if (!state.tourOpen) {
    return null;
  }
  return (
    <div class="banner banner-info" data-testid="render-tour">
      <b>第一次使用</b>
      <ol class="tour-steps">
        {RENDER_ONBOARDING_STEPS.map((label, index) => (
          <li key={label}>
            <span class="step-index">{index + 1}</span>
            <span>{label}</span>
          </li>
        ))}
      </ol>
      <div class="muted">
        三步完成后，「开始渲染」即可点击。卡住时点「重新检测环境」刷新这一页的诊断结果。
      </div>
    </div>
  );
}

/**
 * 环境诊断面板（规格 §5.3 / §5.4）。
 *
 * 红黄绿分级；error 级逐项给出**下一步做什么**。未就绪时上层会禁用「开始渲染」，
 * 因此这里不是装饰，而是用户唯一能看懂「为什么还不能开始」的地方。
 */
function EnvironmentPanel({ controller }: { controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);
  const env = state.environment;

  if (state.environmentLoading && !env) {
    return (
      <div class="banner banner-info" data-testid="render-environment">
        正在检查环境…
      </div>
    );
  }
  if (!env) {
    return (
      <div class="banner banner-warn" data-testid="render-environment">
        环境检查结果暂不可用。
        <button
          type="button"
          class="chip"
          data-testid="render-environment-recheck"
          onClick={() => void controller.actions.loadEnvironment()}
        >
          重新检测环境
        </button>
      </div>
    );
  }

  const errors_ = env.checks.filter((c) => c.status === "error");
  const warnings = env.checks.filter((c) => c.status === "warning");
  const okCount = env.checks.length - errors_.length - warnings.length;

  return (
    <div
      class={env.ready ? "banner banner-ok" : "banner banner-error"}
      data-testid="render-environment"
      data-ready={env.ready ? "true" : "false"}
    >
      <b data-testid="render-environment-summary">
        {env.ready
          ? `环境就绪：${okCount} 项正常${warnings.length ? `，${warnings.length} 项提醒` : ""}`
          : `环境未就绪：${errors_.length} 项需要处理`}
      </b>
      <button
        type="button"
        class="chip"
        data-testid="render-environment-recheck"
        onClick={() => void controller.actions.loadEnvironment()}
      >
        重新检测环境
      </button>

      {errors_.length > 0 ? (
        <ul class="env-list" data-testid="render-environment-blockers">
          {errors_.map((check) => (
            <li key={check.id} class="env-error">
              <b>{check.message}</b>
              {check.hint ? <div class="muted">{check.hint}</div> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {warnings.length > 0 ? (
        <ul class="env-list" data-testid="render-environment-warnings">
          {warnings.map((check) => (
            <li key={check.id} class="env-warn">
              <span>{check.message}</span>
              {check.hint ? <div class="muted">{check.hint}</div> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function StoragePanel({ controller }: { controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);
  const usage = state.storage;

  return (
    <div class="storage-panel" data-testid="render-storage">
      {usage ? (
        <>
          <span data-testid="storage-total">
            输出总容量：<b>{formatBytes(usage.total_bytes)}</b> / 上限 {formatBytes(usage.max_total_bytes)}
          </span>
          <span class="muted">
            任务 {usage.job_count}（固定 {usage.pinned_count}）· 保留 {usage.max_age_days} 天 · 最多 {usage.max_jobs} 个
          </span>
          <span class="muted" data-testid="storage-reclaimable">
            可清理 {usage.evictable_jobs} 个 · 约 {formatBytes(usage.reclaimable_bytes)}
          </span>
        </>
      ) : (
        <span class="muted">正在读取存储用量…</span>
      )}
      <div class="storage-actions">
        <button
          type="button"
          class="chip"
          data-testid="storage-cleanup-preview"
          onClick={() => void controller.actions.cleanupStorage(true)}
        >
          预演清理
        </button>
        <button
          type="button"
          class="chip"
          data-testid="storage-cleanup-run"
          onClick={() => {
            // 清理前必须明确确认：列出预计释放容量。
            const reclaim = usage ? formatBytes(usage.reclaimable_bytes) : "未知";
            const count = usage ? usage.evictable_jobs : 0;
            if (globalThis.confirm(`将清理最多 ${count} 个未固定任务，预计释放 ${reclaim}。正在运行或被固定的任务不会被动。继续？`)) {
              void controller.actions.cleanupStorage(false);
            }
          }}
        >
          清理历史任务
        </button>
      </div>
    </div>
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
  // 环境未就绪时禁用「开始渲染」：诊断还没读到时按未就绪处理（保守方向），
  // 避免在信息不足时放行一个必然失败的任务。
  const environmentReady = state.environment !== null && state.environment.ready === true;
  const numOrNull = (raw: string): number | null => {
    const t = raw.trim();
    if (!t) {
      return null;
    }
    const v = Number(t);
    return Number.isFinite(v) ? v : null;
  };
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
        <label>
          <span>灯光配置</span>
          <select
            value={state.lighting}
            data-testid="render-lighting"
            onChange={(event) =>
              controller.actions.setLighting(
                (event.currentTarget as HTMLSelectElement).value as "volume_fog" | "legacy"
              )
            }
          >
            <option value="volume_fog">体积雾灯光组（默认）</option>
            <option value="legacy">旧版复刻灯组</option>
          </select>
        </label>
        {state.lighting === "volume_fog" ? (
          <label>
            <span>雾密度（0–0.1）</span>
            <input
              type="number"
              min="0"
              max="0.1"
              step="0.001"
              value={state.fogDensity}
              data-testid="render-fog-density"
              onInput={(event) =>
                controller.actions.setFogDensity(
                  Number((event.currentTarget as HTMLInputElement).value)
                )
              }
            />
          </label>
        ) : null}
      </div>

      {state.mode === "enhanced" ? (
        <div class="settings-row" data-testid="render-glow-settings">
          <span class="muted">
            辉光微调（留空 = 用默认修复值：阈值 0.55 / 强度 0.85 / 尺寸 0.62 / 平滑度 0.35 / High）
          </span>
          <label>
            <span>阈值</span>
            <input
              type="number"
              min="0"
              max="10"
              step="0.01"
              value={state.glowThreshold ?? ""}
              data-testid="render-glow-threshold"
              onInput={(event) =>
                controller.actions.setGlow(
                  "threshold",
                  numOrNull((event.currentTarget as HTMLInputElement).value)
                )
              }
            />
          </label>
          <label>
            <span>强度</span>
            <input
              type="number"
              min="0"
              max="10"
              step="0.01"
              value={state.glowStrength ?? ""}
              data-testid="render-glow-strength"
              onInput={(event) =>
                controller.actions.setGlow(
                  "strength",
                  numOrNull((event.currentTarget as HTMLInputElement).value)
                )
              }
            />
          </label>
          <label>
            <span>尺寸</span>
            <input
              type="number"
              min="0"
              max="10"
              step="0.01"
              value={state.glowSize ?? ""}
              data-testid="render-glow-size"
              onInput={(event) =>
                controller.actions.setGlow(
                  "size",
                  numOrNull((event.currentTarget as HTMLInputElement).value)
                )
              }
            />
          </label>
          <label>
            <span>平滑度</span>
            <input
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={state.glowSmoothness ?? ""}
              data-testid="render-glow-smoothness"
              onInput={(event) =>
                controller.actions.setGlow(
                  "smoothness",
                  numOrNull((event.currentTarget as HTMLInputElement).value)
                )
              }
            />
          </label>
          <label>
            <span>质量</span>
            <select
              value={state.glowQuality ?? ""}
              data-testid="render-glow-quality"
              onChange={(event) => {
                const v = (event.currentTarget as HTMLSelectElement).value;
                controller.actions.setGlowQuality(
                  v === "" ? null : (v as "High" | "Medium" | "Low")
                );
              }}
            >
              <option value="">默认（High）</option>
              <option value="High">High</option>
              <option value="Medium">Medium</option>
              <option value="Low">Low</option>
            </select>
          </label>
        </div>
      ) : null}

      <div class="wizard-actions">
        <button
          type="button"
          class="primary"
          data-testid="render-start"
          disabled={!state.selectedModelId || state.busy || !environmentReady}
          onClick={() => void controller.actions.startJob()}
        >
          {state.busy
            ? "正在创建任务…"
            : environmentReady
              ? "开始渲染"
              : "环境检查未通过，暂不能开始"}
        </button>
        {!environmentReady ? (
          <div class="muted" data-testid="render-start-disabled-reason">
            请先按上方「环境未就绪」里的逐项提示处理，然后点「重新检测环境」。
          </div>
        ) : null}
      </div>
    </div>
  );
}

const STATE_LABELS: Record<string, string> = { ...RENDER_STATE_LABELS, ...RENDER_TERMINAL_LABELS };

/** 终态 Chip 的色调：只有 success 用 ok；diagnostic 用 warn（不是普通失败）。 */
function terminalTone(state: string): "ok" | "warn" | "bad" {
  if (state === "success") return "ok";
  if (state === "diagnostic") return "warn";
  return "bad";
}

function ProgressStep({ job, controller }: { job: RenderJob; controller: RenderStore }) {
  const terminal = isTerminalState(job.state);
  const waiting = job.state === "waiting_confirmation";
  const cancelable = canCancelJob(job);

  return (
    <div class="wizard-body" data-testid="render-progress">
      <div class="progress-head" data-testid="render-state">
        <Chip
          tone={terminal ? terminalTone(job.state) : "info"}
          label={STATE_LABELS[job.state] ?? job.state}
        />
        <span class="muted">
          {job.model_name} · {job.fingerprint}
        </span>
        {job.shippable ? (
          <span class="chip chip-ok" data-testid="render-shippable">
            可交付
          </span>
        ) : null}
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
          cancelable ? (
            <button
              type="button"
              class="chip"
              data-testid="render-cancel"
              onClick={() => void controller.actions.cancelJob()}
            >
              取消任务
            </button>
          ) : (
            // 规格 §7.3：正式渲染阶段不可取消。**先禁用并说明原因**，
            // 而不是发一个注定被服务端拒绝的请求。
            <div class="banner banner-info" data-testid="render-cancel-locked">
              {RENDER_CANCEL_LOCKED_MESSAGE}
            </div>
          )
        ) : isTerminalRetryable(job) ? (
          <button
            type="button"
            class="chip"
            data-testid="render-retry"
            onClick={() => void controller.actions.retryJob()}
          >
            重新渲染
          </button>
        ) : null}
      </div>
    </div>
  );
}

/** 可重试的终态（与服务端 RETRYABLE_STATES 一致；取消后也允许重跑）。 */
function isTerminalRetryable(job: RenderJob): boolean {
  return (
    job.state === "failed" ||
    job.state === "rejected" ||
    job.state === "diagnostic" ||
    job.state === "cancelled"
  );
}

function ResultStep({ job, controller }: { job: RenderJob; controller: RenderStore }) {
  const state = useStore(controller.store, (snapshot) => snapshot);

  // ---- 可交付：只有 success 且服务端说 shippable -----------------------
  if (isShippableJob(job)) {
    return (
      <div class="render-result" data-testid="render-result">
        <h3>渲染结果</h3>
        {job.degraded ? (
          <div class="banner banner-warn" data-testid="render-result-degraded">
            本次未收到固定脚本的终态标记，按兼容规则判定成功（退出码 0 且确有成品图）。
            这是<b>降级判定</b>，建议更新渲染脚本以获得完整终态信息。
          </div>
        ) : null}
        <div class="result-meta">
          <span class="muted" data-testid="render-disk-bytes">
            产物占用：{formatBytes(job.disk_bytes)}
          </span>
          <span class="muted" data-testid="render-retention">
            保留状态：{job.retention_state}
          </span>
          <label class="checkbox" title="固定后不会被自动清理">
            <input
              type="checkbox"
              checked={job.pinned}
              data-testid="render-pin"
              onChange={(event) =>
                void controller.actions.pinJob((event.currentTarget as HTMLInputElement).checked)
              }
            />
            <span>保留此任务</span>
          </label>
        </div>
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
                <span class="muted"> {formatBytes(artifact.size)}</span>
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

  // ---- 需要材质确认：绝不是「渲染失败」 --------------------------------
  if (job.state === "rejected") {
    return (
      <div class="banner banner-warn" data-testid="render-result-rejected">
        <b>需要材质确认</b>
        <div class="muted">
          渲染前的材质准入检查未通过，尚未开始正式渲染，因此没有产出可交付成品。
          这不是渲染失败。请按下面的提示完成材质确认后重新渲染。
        </div>
        {job.error_code ? <div class="muted">错误码：{job.error_code}</div> : null}
        {job.confirm_url ? (
          <a
            class="chip"
            href={job.confirm_url}
            target="_blank"
            rel="noreferrer"
            data-testid="render-result-confirm-link"
          >
            打开确认页
          </a>
        ) : null}
      </div>
    );
  }

  // ---- 诊断图：明确不可交付 ----
  if (job.state === "diagnostic") {
    return (
      <div class="banner banner-warn" data-testid="render-result-diagnostic">
        <b>诊断图（不可交付）</b>
        <div class="muted">
          本次产出的是诊断图：未识别材质以洋红标识，图上带「诊断预览」水印。
          它不是正式成品，不能作为交付结果使用。
        </div>
        {job.error_code ? <div class="muted">错误码：{job.error_code}</div> : null}
        {state.artifacts.length > 0 ? (
          <ul class="artifact-list" data-testid="render-diagnostic-artifacts">
            {state.artifacts.map((artifact) => (
              <li key={artifact.artifact_id}>
                <a
                  href={controller.actions.artifactUrl(job.job_id, artifact.artifact_id)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {artifact.name}
                </a>
                <span class="muted"> {formatBytes(artifact.size)}（诊断图，非成品）</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  }

  if (job.state === "cancelled") {
    return (
      <div class="banner banner-info" data-testid="render-result-cancelled">
        <b>已取消</b>
        <div class="muted">
          {job.cancel_reason ?? "任务已被取消"}。半成品不会标记为正式结果，可修正后重新渲染。
        </div>
      </div>
    );
  }

  // ---- 失败：错误码 + 可操作提示，绝不展示裸 traceback -------------------
  return (
    <div class="banner banner-error" data-testid="render-result-failed">
      <b>渲染失败</b>
      <div class="muted">
        本次未产出可交付结果。日志与输出目录已保留，可修正后重试。
      </div>
      {job.error_code ? (
        <div class="muted" data-testid="render-result-error-code">
          错误码：{job.error_code}
        </div>
      ) : null}
    </div>
  );
}
