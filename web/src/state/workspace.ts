/**
 * v4 工作台的唯一状态机。
 *
 * 所有会改状态的路径都收在这里（组件只调用 actions），原因有三：
 *
 * * 「结构失效即禁用预览」这条规则要在**一个地方**判定，散落在组件里必然漏；
 * * 撤销栈与服务端任务历史必须互不干扰，混在一起就会出现「撤销一步把渲染也回滚了」；
 * * 刷新基线要原子地清掉草稿 + 命令栈，任一遗漏都会让界面停在不可能的状态。
 *
 * 不写浏览器持久存储：草稿引用的 id 与结构指纹都是**会话内**的东西，
 * 加载一份过期草稿比没有草稿更危险。
 *
 * 约定：`groupId` 一律是**分组节点的 id**（形如 `cel.Cel_Skin`），
 * 不是显示名，也不是 `node.group`（后者是 `"cel"` 这个族名）。
 */

import { previewImageUrl } from "../api/client";
import type {
  BaselineResponse,
  ConnectionState,
  ErrorDetail,
  ExternalChange,
  FramingContext,
  JobError,
  JobResult,
  JobState,
  SurfaceBaselinePublic,
} from "../api/types";
import { AppError } from "../api/types";
import type { SurfaceDraft, SurfaceSchema } from "../schema/types";
import { flatten, topGroups } from "../schema/parse";
import { canCopyInto, copyRampValue, draftValueFromNode } from "../schema/ramp";
import { DraftHistory, type CommandKind } from "./commands";
import {
  computeDirtyIds,
  deepCopy,
  effectiveValue,
  emptyDraft,
  nodeValue,
  resetAll,
  resetGroup,
  sanitizeDraft,
  setDraftValue,
  valuesEqual,
} from "./draft";
import { JobRunner, type JobOutcome, type JobSubmitPayload } from "./jobs";
import { createStore, type Store } from "./store";

export interface PreviewState {
  jobId: string;
  /** 带 `v=<job_id>` 的防缓存 URL。 */
  url: string;
  rawUrl: string;
  appliedSurface: Record<string, unknown>;
  restoreVerified: boolean;
  externalChanges: ExternalChange[];
  framing: Record<string, unknown> | null;
  renderResolution: number[] | null;
  at: number;
}

/**
 * 基线首张预览的可用状态。
 *
 * 基线本身在 `POST /api/session/baseline` 返回时就已建立，但**画面**要等它顺带创建的
 * 预览任务跑完才有文件。两者必须分开表达，否则界面只能二选一：
 * 要么在文件落盘前挂上一张 404 的图，要么谎称「基线还没有画面」。
 */
export type BaselinePreviewStatus = "none" | "pending" | "ready" | "failed";

/**
 * 工程脏标记提示文案（O3）。
 *
 * 只承诺**我们能保证**的事：参数值 / 节点 / 相机 / 输出设置会恢复原值；本工具不写盘。
 * **不承诺**「Blender 看起来是干净的」—— 脏标记由 Blender 维护且不能由我们清除。
 */
export const PROJECT_DIRTY_NOTICE =
  "本次预览只改内存：参数值、节点、相机与输出设置都已恢复原值。" +
  "本工具不会保存或覆盖你的 .blend 文件；" +
  "Blender 仍可能显示「有未保存的修改」，那是本次写入留下的标记，不代表工程内容有差异。";

export interface WorkspaceState {
  schema: SurfaceSchema | null;
  baseline: SurfaceBaselinePublic | null;
  baselineId: string | null;
  baselineCapturedAt: string | null;
  baselinePreviewUrl: string | null;
  /** 基线首张预览：任务跑完之前不允许展示图。 */
  baselinePreviewStatus: BaselinePreviewStatus;
  draft: SurfaceDraft;
  effective: Record<string, unknown>;
  structureHash: string | null;
  externalChanges: ExternalChange[];
  dirtyIds: Set<string>;
  /** **只在跑**的任务。终态任务由 `lastJob` 承载，绝不留在活跃位上。 */
  activeJob: JobState | null;
  /** 最近一个**终态**任务的快照（成功 / 失败 / 被取代），用于显示「上次」与步骤轨迹。 */
  lastJob: JobState | null;
  lastSuccessfulPreview: PreviewState | null;
  /** 画面基于的草稿比当前草稿旧（提交后又编辑过）。 */
  previewStale: boolean;
  undoDepth: number;
  redoDepth: number;
  canUndo: boolean;
  canRedo: boolean;
  connection: ConnectionState;
  framing: FramingContext | null;
  activeGroupId: string | null;
  error: ErrorDetail | null;
  notice: string | null;
  /**
   * 工程脏标记提示（O3）。
   *
   * Blender 的 `bpy.data.is_dirty` 是**粘性**的：把参数写回原值也不会清除它，
   * 而本工具只在用户显式走保存流程时才写盘。于是「基线时干净、预览后变脏」
   * 几乎必然发生 —— 它不是缺陷，也不代表工程内容真的被改了。为空表示无需提示。
   */
  projectNotice: string | null;
  loading: boolean;
  /** 结构失效：草稿已作废，预览被禁用，直到刷新基线。 */
  historyBlocked: boolean;
  framingChoice: FramingChoice;
}

export interface FramingChoice {
  mode: string;
  margin: number;
}

export interface WorkspaceEndpoints {
  surfaceSchema(options?: { signal?: AbortSignal }): Promise<{
    schema_version: string;
    groups: unknown[];
    highest_cost: string;
    surface_baseline_id?: string;
    structure_hash?: string;
    compositor_group?: string;
    degraded?: string[];
  }>;
  surfaceBaseline(options?: { signal?: AbortSignal }): Promise<SurfaceBaselinePublic & { ok: true }>;
  createBaseline(
    body?: { framing?: FramingChoice },
    options?: { signal?: AbortSignal }
  ): Promise<BaselineResponse & { ok: true }>;
  submitPreview(
    payload: JobSubmitPayload,
    options?: { signal?: AbortSignal }
  ): Promise<{ job_id: string; seq: number; status: string }>;
  job(jobId: string, options?: { signal?: AbortSignal }): Promise<JobState>;
  blenderStatus(): Promise<{
    ok: boolean;
    status: string;
    target: string;
    checked_at: string;
    latency_ms?: number | null;
    error?: ErrorDetail | null;
  }>;
  framingContext(): Promise<FramingContext & { ok: true }>;
  parseSchema(raw: unknown): SurfaceSchema;
}

export interface WorkspaceOptions {
  endpoints: WorkspaceEndpoints;
  history?: DraftHistory;
  pollIntervalMs?: number;
  jobTimeoutMs?: number;
  now?: () => number;
}

const IDLE_CONNECTION: ConnectionState = {
  status: "unknown",
  target: "",
  latencyMs: null,
  checkedAt: null,
  message: null,
};

const DEFAULT_FRAMING: FramingChoice = { mode: "current_camera", margin: 0.15 };

export function createWorkspace(options: WorkspaceOptions) {
  const { endpoints } = options;
  const history = options.history ?? new DraftHistory();
  const now = options.now ?? (() => Date.now());

  let draftVersion = 0;
  let previewedVersion = 0;
  /** 提交那一刻的草稿版本；用来判断「画面是否正好对应提交时的草稿」。 */
  let previewedVersionForSubmit = 0;
  /**
   * 正在跟踪的**基线首张预览**任务 id。
   *
   * 基线响应里就带着 `job_id`，但那时图片文件还不存在 —— 必须轮询到 `done`
   * 才能把 URL 交给 `img`。这个变量就是「当前那一次」的凭据（每次只跟踪一个）。
   */
  let baselineJobId: string | null = null;

  const store: Store<WorkspaceState> = createStore<WorkspaceState>({
    schema: null,
    baseline: null,
    baselineId: null,
    baselineCapturedAt: null,
    baselinePreviewUrl: null,
    baselinePreviewStatus: "none",
    draft: emptyDraft(),
    effective: {},
    structureHash: null,
    externalChanges: [],
    dirtyIds: new Set<string>(),
    activeJob: null,
    lastJob: null,
    lastSuccessfulPreview: null,
    previewStale: false,
    undoDepth: 0,
    redoDepth: 0,
    canUndo: false,
    canRedo: false,
    connection: IDLE_CONNECTION,
    framing: null,
    activeGroupId: null,
    error: null,
    notice: null,
    projectNotice: null,
    loading: false,
    historyBlocked: false,
    framingChoice: DEFAULT_FRAMING,
  });

  function syncHistoryDepths(): void {
    store.setState({
      undoDepth: history.undoDepth,
      redoDepth: history.redoDepth,
      canUndo: history.undoDepth > 0,
      canRedo: history.redoDepth > 0,
    });
  }

  function applyDraft(next: SurfaceDraft): void {
    draftVersion += 1;
    const state = store.getState();
    store.setState({
      draft: next,
      dirtyIds: computeDirtyIds(state.schema, next),
      // 「画面过期」的前提是**有一张画面**。还没渲染过就没有「过期」可言 ——
      // 那种情况由「N 项已改（未预览）」表达，两件事不能混成一句提示。
      previewStale: state.lastSuccessfulPreview !== null && draftVersion !== previewedVersion,
    });
  }

  function commitCommand(params: {
    kind: CommandKind;
    label: string;
    groupId: string;
    groupIds?: string[];
    next: SurfaceDraft;
    mergeKey?: string | null;
  }): void {
    const before = store.getState().draft;
    if (valuesEqual(before, params.next)) {
      return;
    }
    if (store.getState().historyBlocked) {
      // 结构失效期间不记账：撤销回一个已经作废的草稿只会更糟。
      applyDraft(params.next);
      return;
    }
    history.push({
      kind: params.kind,
      label: params.label,
      groupId: params.groupId,
      groupIds: params.groupIds,
      before,
      after: params.next,
      mergeKey: params.mergeKey ?? null,
      now: now(),
    });
    applyDraft(params.next);
    syncHistoryDepths();
  }

  function handleSuccess(job: JobState, result: JobResult): void {
    const applied = (result.applied_surface ?? {}) as Record<string, unknown>;
    // 结果里没有就回落到任务级字段：服务端两处都会带，但前端不该因为只读了一处就丢提示。
    const external = result.external_changes ?? job.external_changes ?? [];
    const rawUrl = result.preview_url ?? `/api/preview/${job.job_id}`;
    const stale = draftVersion !== previewedVersionForSubmit;
    const preview: PreviewState = {
      jobId: job.job_id,
      url: previewImageUrl(rawUrl, job.job_id),
      rawUrl,
      appliedSurface: applied,
      restoreVerified: Boolean(result.surface_restore_verified ?? result.restore_verified),
      externalChanges: external,
      framing: (result.framing as Record<string, unknown>) ?? null,
      renderResolution: (result.render_resolution as number[]) ?? null,
      at: now(),
    };
    if (!stale) {
      // 只有「画面正是当前草稿」时才承认它是已预览版本。
      previewedVersion = previewedVersionForSubmit;
    }
    // 基线首张预览：**任务跑完**（文件已落盘）才允许展示基线图。
    // 这里的判定与 `stale` 无关 —— 基线图是不是「当前草稿的画面」不影响它已经生成。
    const isBaselineJob = baselineJobId !== null && job.job_id === baselineJobId;
    if (isBaselineJob) {
      baselineJobId = null;
    }
    store.setState({
      lastSuccessfulPreview: preview,
      ...(isBaselineJob ? { baselinePreviewStatus: "ready" as const, baselinePreviewUrl: rawUrl } : {}),
      // 新草稿产生时旧任务结果**不写进 effective**，并标记画面对应的草稿已过期。
      ...(stale ? {} : { effective: { ...store.getState().effective, ...applied } }),
      previewStale: draftVersion !== previewedVersion,
      externalChanges: external,
      error: null,
      loading: false,
      notice: stale
        ? "本次画面基于提交时的草稿；你在渲染期间又改过草稿，已标记为过期。"
        : null,
      // O3：工程本来是干净的，预览后 Blender 却显示「有未保存的修改」——
      // 那是写入留下的粘性标记，不是内容差异。不改承诺、不尝试清标记，只如实说明。
      projectNotice: result.project?.dirty_flagged
        ? PROJECT_DIRTY_NOTICE
        : store.getState().projectNotice,
    });
  }

  /**
   * **统一终态清理**（与 `JobRunner.onTerminal` 配对）。
   *
   * `onSucceeded` / `onFailed` 负责「结果」，这里负责「不再活动」：
   *
   * * 终态任务一律离开 `activeJob` —— 否则界面会一直显示「渲染中」，
   *   而 `activeJob` 也就失去了「活动」的含义；
   * * 终态快照记进 `lastJob`，让状态栏还能说出「上次」是什么结果；
   * * 基线首张预览若以失败 / 被取代收场，要**单独**改判（基线本身已经建立，
   *   不成立的只是那张画面），否则界面会永远停在「正在生成基线预览…」。
   */
  function handleTerminal(job: JobState, outcome: JobOutcome): void {
    const patch: Partial<WorkspaceState> = {
      activeJob: null,
      lastJob: job,
      loading: false,
    };
    if (baselineJobId !== null && job.job_id === baselineJobId) {
      baselineJobId = null;
      if (outcome !== "succeeded") {
        patch.baselinePreviewStatus = "failed";
        patch.baselinePreviewUrl = null;
        patch.notice = baselinePreviewFailedNotice(outcome);
      }
    }
    store.setState(patch);
  }

  /**
   * 提交新任务会**取代**在途的基线预览。这时它不可能再被我们跟踪到终态，
   * 必须当场改判 —— 否则界面永远停在「正在生成基线预览…」。
   *
   * 之所以按「失败」而不是「继续等」处理：`AbortController` 只能取消**前端**的轮询，
   * 服务端那个任务仍会跑完；我们既无法确认文件何时落盘，也无法确认它是否成功。
   * 与其挂一张可能 404 的图，不如如实说「没确认生成」，让用户点一次「刷新基线」。
   */
  function abandonPendingBaselinePreview(): void {
    if (store.getState().baselinePreviewStatus !== "pending") {
      return;
    }
    baselineJobId = null;
    store.setState({
      baselinePreviewStatus: "failed",
      baselinePreviewUrl: null,
      notice: baselinePreviewFailedNotice("superseded"),
    });
  }

  const runner = new JobRunner({
    endpoints,
    ...(options.pollIntervalMs === undefined ? {} : { pollIntervalMs: options.pollIntervalMs }),
    ...(options.jobTimeoutMs === undefined ? {} : { timeoutMs: options.jobTimeoutMs }),
    ...(options.now === undefined ? {} : { now: options.now }),
    hooks: {
      onUpdate: (job) => {
        store.setState({
          activeJob: job,
          externalChanges: job.external_changes ?? store.getState().externalChanges,
        });
      },
      onSucceeded: (job, result) => handleSuccess(job, result),
      // 失败任务的 `job` 不必在这里写状态：紧接着的 `onTerminal` 会用同一份快照
      // 记进 `lastJob`（并清掉 activeJob），写两次只会多一次渲染。
      onFailed: (_job, error) => {
        // ⚠ **不动** lastSuccessfulPreview —— 失败画面不得替换最后一张成功预览。
        //
        // 但错误必须走**同一条**路由：服务端的预览任务同样会以
        // `STRUCTURE_CHANGED` / `IDENTITY_MISSING` 终结（结构在提交后被改、
        // 对象被重命名或删除）。若这里只写一句 error，草稿不会作废、
        // `historyBlocked` 仍为 false，用户还能接着提交 —— 结构失效保护等于没生效。
        routeErrorDetail(jobErrorToDetail(error));
      },
      onTerminal: (job, outcome) => handleTerminal(job, outcome),
      onCancelled: () => {
        // 本地取消（新任务取代旧的 / 用户主动取消）同样是终态：活跃位必须清掉。
        // 注意这里**不**碰 lastJob —— 被取消的任务没有一个可展示的终态快照。
        store.setState({ activeJob: null });
      },
    },
  });

  async function loadSchema(signal?: AbortSignal): Promise<SurfaceSchema | null> {
    try {
      const raw = await endpoints.surfaceSchema(signal ? { signal } : {});
      const schema = endpoints.parseSchema(raw);
      store.setState({
        schema,
        structureHash: raw.structure_hash ?? store.getState().structureHash,
      });
      return schema;
    } catch (error) {
      store.setState({ error: toDetail(error) });
      return null;
    }
  }

  function baselinePreviewFailedNotice(outcome: JobOutcome | "superseded"): string {
    return outcome === "superseded"
      ? "基线预览任务已被更新的任务取代，基线本身已建立；如需首张画面，请再点「刷新基线」。"
      : "基线已建立，但首张预览未能生成；可点「刷新基线」重试。";
  }

  async function refreshBaseline(choice?: FramingChoice): Promise<boolean> {
    const framing = choice ?? store.getState().framingChoice;
    // 上一次的基线预览若还挂着，这次刷新同样会取代它 —— 先如实改判，别让它停在 pending。
    baselineJobId = null;
    store.setState({
      loading: true,
      error: null,
      notice: null,
      framingChoice: framing,
      baselinePreviewStatus: "pending",
      baselinePreviewUrl: null,
    });
    let created: Awaited<ReturnType<WorkspaceEndpoints["createBaseline"]>>;
    try {
      created = await endpoints.createBaseline({ framing });
    } catch (error) {
      store.setState({
        error: toDetail(error),
        loading: false,
        baselinePreviewStatus: "failed",
      });
      return false;
    }

    history.clear();
    syncHistoryDepths();
    const schema = await loadSchema();
    const surface = created.surface ?? null;
    draftVersion = 0;
    previewedVersion = 0;
    previewedVersionForSubmit = 0;
    const previewUrlFromResponse =
      typeof created.preview_url === "string" ? created.preview_url : null;
    const jobId = typeof created.job_id === "string" && created.job_id ? created.job_id : null;
    store.setState({
      baselineId: created.baseline_id ?? null,
      baselineCapturedAt: created.captured_at ?? null,
      baseline: surface,
      // 基线图**先不给 URL**：轮询到任务完成再给，否则 img 会指向一个还没落盘的文件。
      baselinePreviewUrl: jobId ? null : previewUrlFromResponse,
      baselinePreviewStatus: jobId ? "pending" : previewUrlFromResponse ? "ready" : "none",
      draft: emptyDraft(),
      dirtyIds: new Set<string>(),
      effective: {},
      structureHash: surface?.structure_hash ?? null,
      externalChanges: [],
      previewStale: false,
      historyBlocked: false,
      activeJob: null,
      lastJob: null,
      loading: jobId !== null,
      notice:
        surface && surface.available === false
          ? "v4 拓扑探针未成功，Cel 参数暂不可用（L0 与保存流程不受影响）。"
          : null,
      activeGroupId: store.getState().activeGroupId ?? firstGroupId(schema),
    });

    if (!jobId) {
      return true;
    }

    // 轮询基线任务：完成（成功）后才展示基线图；失败 / 被取代由终态清理改判。
    // 返回 `false` 只代表该预览没成 —— **基线本身已经建立**，所以这里仍返回 `true`。
    baselineJobId = jobId;
    await runner.adopt(jobId);
    return true;
  }

  async function readBaseline(): Promise<void> {
    store.setState({ loading: true });
    try {
      const baseline = await endpoints.surfaceBaseline();
      const schema = await loadSchema();
      store.setState({
        baseline,
        baselineId: baseline.baseline_id ?? null,
        baselineCapturedAt: baseline.captured_at ?? null,
        // 只读基线的响应里没有 preview_url（那是建立基线时才知道的 job）。
        // 所以这里只在服务端真的给了它时才填，否则「基线图」保持不可用 ——
        // 宁可少一张图，也不要指向一个不存在的文件。
        ...(typeof baseline.preview_url === "string"
          ? { baselinePreviewUrl: baseline.preview_url, baselinePreviewStatus: "ready" as const }
          : {}),
        structureHash: baseline.structure_hash ?? null,
        loading: false,
        activeGroupId: store.getState().activeGroupId ?? firstGroupId(schema),
      });
    } catch (error) {
      store.setState({ error: toDetail(error), loading: false });
    }
  }

  async function bootstrap(): Promise<void> {
    store.setState({ loading: true, error: null });
    await Promise.allSettled([refreshConnection(), refreshFraming()]);
    await loadSchema();
    if (!store.getState().baseline) {
      await readBaseline();
    }
    store.setState({
      activeGroupId: store.getState().activeGroupId ?? firstGroupId(store.getState().schema),
      loading: false,
    });
  }

  async function refreshConnection(): Promise<void> {
    try {
      const status = await endpoints.blenderStatus();
      store.setState({
        connection: {
          status: status.status,
          target: status.target,
          latencyMs: status.latency_ms ?? null,
          checkedAt: status.checked_at,
          message: status.error?.message ?? null,
        },
      });
    } catch (error) {
      store.setState({
        connection: {
          ...IDLE_CONNECTION,
          status: "unreachable",
          message: toDetail(error).message,
        },
      });
    }
  }

  async function refreshFraming(): Promise<void> {
    try {
      const framing = await endpoints.framingContext();
      store.setState({ framing });
    } catch {
      // 取景诊断失败不影响调参：它只是状态栏信息。
    }
  }

  function rampDraftOf(
    elements: Array<{ position: number; color: number[] }>,
    interpolation: string
  ) {
    return { elements: deepCopy(elements), interpolation };
  }

  /**
   * 错误路由的**唯一入口**。
   *
   * 两条来源都必须经过这里：同步异常（`handleError`）与预览任务终态（`onFailed`）。
   * 结构失效保护只有一份，漏掉任何一条来源，用户都能继续提交一份已经作废的草稿。
   *
   * `patch` 只用来顺带带上任务上下文（如 `activeJob`），**不得**用它覆盖
   * `error` / `historyBlocked` / `draft` 这些由下面统一决定的字段。
   */
  function routeErrorDetail(detail: ErrorDetail, patch: Partial<WorkspaceState> = {}): void {
    if (isStructureFatalCode(detail.code)) {
      actions.applyStructureFatal(detail, patch);
      return;
    }
    store.setState({ ...patch, error: detail, loading: false });
  }

  const actions = {
    bootstrap,
    refreshBaseline,
    readBaseline,
    refreshConnection,
    refreshFraming,

    setActiveGroup(groupId: string | null): void {
      store.setState({ activeGroupId: groupId });
    },
    setFramingChoice(choice: FramingChoice): void {
      store.setState({ framingChoice: choice });
    },
    clearNotice(): void {
      store.setState({ notice: null });
    },
    clearProjectNotice(): void {
      store.setState({ projectNotice: null });
    },
    clearError(): void {
      // **只**清提示。`historyBlocked` 是安全锁，只能由「成功刷新基线」解除
      // （见 `refreshBaseline`）—— 否则用户点一下「关闭」就能绕过
      // 「必须刷新基线」这条限制，锁形同虚设。
      store.setState({ error: null });
    },

    /** 标量 / 枚举：`seal` 表示结束一次连续输入（失焦 / 回车）。 */
    setValue(paramId: string, value: unknown, options2: { seal?: boolean } = {}): void {
      const state = store.getState();
      const node = state.schema ? flatten(state.schema.groups).get(paramId) : undefined;
      commitCommand({
        kind: "setValue",
        label: `修改 ${node?.label ?? paramId}`,
        groupId: ownerGroupId(paramId),
        next: setDraftValue(state.draft, paramId, value),
        mergeKey: DraftHistory.mergeKey("setValue", paramId),
      });
      if (options2.seal) {
        history.seal();
      }
    },

    sealHistory(): void {
      history.seal();
    },

    dragElement(
      groupId: string,
      elementIndex: number,
      elements: Array<{ position: number; color: number[] }>,
      interpolation: string
    ): void {
      const state = store.getState();
      const rampId = `${groupId}.ramp`;
      commitCommand({
        kind: "dragElement",
        label: `${groupId} 第 ${elementIndex + 1} 个色标`,
        groupId,
        next: setDraftValue(state.draft, rampId, rampDraftOf(elements, interpolation)),
        mergeKey: DraftHistory.mergeKey("dragElement", rampId, elementIndex),
      });
    },

    setElementColor(
      groupId: string,
      elementIndex: number,
      elements: Array<{ position: number; color: number[] }>,
      interpolation: string
    ): void {
      const state = store.getState();
      const rampId = `${groupId}.ramp`;
      commitCommand({
        kind: "setElementColor",
        label: `${groupId} 第 ${elementIndex + 1} 个色标颜色`,
        groupId,
        next: setDraftValue(state.draft, rampId, rampDraftOf(elements, interpolation)),
        mergeKey: null,
      });
    },

    setInterpolation(
      groupId: string,
      value: string,
      elements: Array<{ position: number; color: number[] }>
    ): void {
      const state = store.getState();
      const rampId = `${groupId}.ramp`;
      commitCommand({
        kind: "setValue",
        label: `${groupId} 插值方式`,
        groupId,
        next: setDraftValue(state.draft, rampId, rampDraftOf(elements, value)),
        mergeKey: null,
      });
    },

    resetGroupDraft(groupId: string): void {
      const state = store.getState();
      commitCommand({
        kind: "resetGroup",
        label: `复位 ${groupId}`,
        groupId,
        next: resetGroup(state.draft, state.schema, groupId),
        mergeKey: null,
      });
    },

    resetAllDraft(): void {
      const state = store.getState();
      commitCommand({
        kind: "resetAll",
        label: "复位全部 Cel 草稿",
        groupId: "",
        groupIds: topGroups(state.schema).map((group) => group.id),
        next: resetAll(state.draft, state.schema),
        mergeKey: null,
      });
    },

    /** 复制色带到另一个兼容组；不兼容时返回原因（不写草稿）。 */
    copyRampToGroup(sourceGroupId: string, targetGroupId: string): string | null {
      const state = store.getState();
      if (!state.schema) {
        return "尚未加载 schema。";
      }
      const index = flatten(state.schema.groups);
      const sourceNode = index.get(`${sourceGroupId}.ramp`);
      const targetNode = index.get(`${targetGroupId}.ramp`);
      if (!sourceNode || sourceNode.kind !== "ramp") {
        return "源组没有可复制的色带。";
      }
      if (!targetNode || targetNode.kind !== "ramp") {
        return "目标组没有可编辑的色带。";
      }
      const reason = canCopyInto(sourceNode, targetNode);
      if (reason) {
        return reason;
      }
      const sourceValue = (nodeValue(state.draft, sourceNode) ??
        draftValueFromNode(sourceNode)) as {
        elements: Array<{ position: number; color: number[] }>;
        interpolation: string;
      };
      const copied = copyRampValue(sourceValue, targetNode.elements.length);
      if (!copied) {
        return "色标数量不同，无法复制。";
      }
      commitCommand({
        kind: "copyToGroup",
        label: `${sourceGroupId} → ${targetGroupId}`,
        groupId: targetGroupId,
        groupIds: [sourceGroupId, targetGroupId],
        next: setDraftValue(state.draft, `${targetGroupId}.ramp`, copied),
        mergeKey: null,
      });
      return null;
    },

    undo(): void {
      const step = history.undoStep();
      if (!step) {
        return;
      }
      applyDraft(step.draft);
      syncHistoryDepths();
      store.setState({ notice: `已撤销：${step.command.label}` });
    },

    redo(): void {
      const step = history.redoStep();
      if (!step) {
        return;
      }
      applyDraft(step.draft);
      syncHistoryDepths();
      store.setState({ notice: `已重做：${step.command.label}` });
    },

    async applyAndPreview(choice?: FramingChoice): Promise<boolean> {
      const state = store.getState();
      if (!state.schema) {
        store.setState({
          error: { code: "NO_SCHEMA", message: "尚未加载 schema。", retryable: true },
        });
        return false;
      }
      if (state.historyBlocked) {
        store.setState({
          error: {
            code: "STRUCTURE_CHANGED",
            message: "结构已变化，草稿已作废。请先刷新基线再预览。",
            retryable: true,
          },
        });
        return false;
      }
      const framing = choice ?? state.framingChoice;
      const draft = sanitizeDraft(state.schema, state.draft);
      previewedVersionForSubmit = draftVersion;
      store.setState({ loading: true, error: null, notice: null, framingChoice: framing });
      // 新任务会取代在途的基线预览 —— 那次基线图不可能再被我们跟踪到，如实改判。
      abandonPendingBaselinePreview();
      const payload: JobSubmitPayload = {
        draft,
        framing,
        ...(state.structureHash ? { expected_structure_hash: state.structureHash } : {}),
      };
      const ok = await runner.start(payload);
      if (!ok) {
        store.setState({ loading: false });
      }
      return ok;
    },

    cancelActiveJob(): void {
      runner.cancel("用户取消");
      store.setState({ loading: false });
    },

    /** 结构失效：草稿作废、命令栈清空、预览禁用，但**保留**最后一张成功预览。 */
    applyStructureFatal(detail: ErrorDetail, patch: Partial<WorkspaceState> = {}): void {
      // 在途的基线预览同样被掐断（`dispose` 不会再走到终态清理），必须当场改判，
      // 否则基线图会永远停在「正在生成…」。
      const pendingBaseline = store.getState().baselinePreviewStatus === "pending";
      baselineJobId = null;
      runner.dispose();
      history.clear();
      syncHistoryDepths();
      draftVersion += 1;
      store.setState({
        ...patch,
        ...(pendingBaseline
          ? { baselinePreviewStatus: "failed" as const, baselinePreviewUrl: null }
          : {}),
        draft: emptyDraft(),
        dirtyIds: new Set<string>(),
        historyBlocked: true,
        error: detail,
        notice: "工程结构已变化：草稿与已签发的保存确认令牌均已作废，请刷新基线。",
        loading: false,
      });
    },

    handleError(error: unknown): void {
      routeErrorDetail(toDetail(error));
    },

    effectiveFor(nodeId: string): unknown {
      const state = store.getState();
      if (nodeId in state.effective) {
        return state.effective[nodeId];
      }
      const node = state.schema ? flatten(state.schema.groups).get(nodeId) : undefined;
      return node ? effectiveValue(node) : null;
    },

    dispose(): void {
      runner.dispose();
    },
  };

  return { store, actions, history, runner };
}

export type Workspace = ReturnType<typeof createWorkspace>;

/**
 * 会让**整份草稿与已签发确认令牌一起作废**的错误码。
 *
 * 判定只认稳定错误码、不认文案 —— 文案会改，错误码不会。
 * 两个来源（同步异常、预览任务终态）共用这一个判据，避免两处判据漂移。
 */
export function isStructureFatalCode(code: string): boolean {
  return code === "STRUCTURE_CHANGED" || code === "IDENTITY_MISSING";
}

/** 从参数 id 反推所属分组节点 id（`cel.Cel_Skin.ramp` → `cel.Cel_Skin`）。 */
export function ownerGroupId(paramId: string): string {
  const parts = paramId.split(".");
  return parts.length >= 2 ? `${parts[0]}.${parts[1]}` : paramId;
}

function firstGroupId(schema: SurfaceSchema | null): string | null {
  const groups = topGroups(schema);
  const editable = groups.find((group) => group.supported && group.editable);
  return (editable ?? groups[0])?.id ?? null;
}

function jobErrorToDetail(error: JobError): ErrorDetail {
  return {
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    hint: error.hint ?? null,
    details: error.details ?? null,
  };
}

function toDetail(error: unknown): ErrorDetail {
  if (error instanceof AppError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      hint: error.hint,
      details: error.details,
    };
  }
  return {
    code: "INTERNAL_ERROR",
    message: error instanceof Error ? error.message : String(error),
    retryable: true,
  };
}
