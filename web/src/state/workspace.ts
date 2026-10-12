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
  CommitBody,
  CommitPrepareBody,
  CommitPrepareResponse,
  CommitResponse,
  CommitStatus,
  ConnectionState,
  ErrorDetail,
  ExternalChange,
  FramingContext,
  FramingSubmitOptions,
  IsolationSummary,
  JobError,
  JobResult,
  JobState,
  PresetDetailResponse,
  PresetListResponse,
  PresetSaveBody,
  PresetSkip,
  PresetSummary,
  SaveMode,
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
import {
  confirmFromError,
  confirmFromPrepared,
  emptySaveState,
  saveBusy,
  saveFailureNotice,
  saveTargetProblem,
  splitSaveDraft,
  type SaveOutcome,
  type SaveState,
} from "./save";
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
  /** 隔离预览结果（没请求隔离时为 null）。 */
  isolation: IsolationSummary | null;
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
  /**
   * 自动预览开关（默认**开**）。
   *
   * 关掉之后只能手动点「应用并预览」—— 渲染很贵，用户可能就想一次改完再渲染。
   * 关掉时**不影响**已有的排期语义：`previewStale` 照旧提示「有待预览的修改」。
   */
  autoPreview: boolean;
  /** 自动预览已经排期、还没提交（界面据此显示「待预览」）。 */
  autoPreviewPending: boolean;
  /** 保存闭环（预设 + 应用到工程）。 */
  save: SaveState;
  /** 本地预设列表。 */
  presets: PresetState;
}

export interface PresetState {
  /** 预设面板是否打开。 */
  open: boolean;
  loading: boolean;
  /** 有写请求在途（保存 / 覆盖 / 套用），按钮据此禁用。 */
  busy: boolean;
  list: PresetSummary[];
  /** 读不动的预设文件：带着错误码列出来，绝不静默忽略。 */
  skipped: PresetSkip[];
  /** 可展示的存储位置（服务端给的展示写法，不含本机绝对路径）。 */
  storage: string;
  error: ErrorDetail | null;
  /** 最近一次预设操作的结果说明。 */
  notice: string | null;
}

function emptyPresetState(): PresetState {
  return {
    open: false,
    loading: false,
    busy: false,
    list: [],
    skipped: [],
    storage: "",
    error: null,
    notice: null,
  };
}

/**
 * 把一份预设的参数面草稿分成「当前工程认得」与「认不得」两堆。
 *
 * 预设里的 id 含**工程事实**（材质名、Cel 组名），所以跨工程套用必然有一部分对不上。
 * 对不上的必须**报数**：静默丢弃会让用户以为预设生效了，而画面毫无变化 ——
 * 那是最难排查的一类问题。
 *
 * 只读 / 不支持的节点也一并算作「认不得」：它们本来就不接受写入。
 */
export function presetDraftFor(
  schema: SurfaceSchema | null,
  surface: Record<string, unknown>
): { applied: SurfaceDraft; skipped: string[] } {
  const index = flatten(schema?.groups ?? []);
  const applied: SurfaceDraft = {};
  const skipped: string[] = [];
  for (const [paramId, value] of Object.entries(surface)) {
    const node = index.get(paramId);
    if (!node || node.kind === "group" || node.kind === "unknown" || !node.supported || !node.editable) {
      skipped.push(paramId);
      continue;
    }
    applied[paramId] = deepCopy(value);
  }
  return { applied, skipped };
}

export interface FramingChoice {
  mode: string;
  margin: number;
  /**
   * 隔离预览的材质名。`null` = 不隔离（显示全部材质）。
   *
   * 它和「当前选中的材质」是**两件事**：选中材质只决定编辑哪个分组，
   * 隔离则真的改变渲染结果。因此用户可以选中 A 材质却仍然看整体画面。
   */
  isolateMaterial: string | null;
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
    body?: { framing?: FramingSubmitOptions },
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
  listPresets(options?: { signal?: AbortSignal }): Promise<PresetListResponse>;
  readPreset(presetId: string, options?: { signal?: AbortSignal }): Promise<PresetDetailResponse>;
  createPreset(body: PresetSaveBody, options?: { signal?: AbortSignal }): Promise<PresetDetailResponse>;
  updatePreset(
    presetId: string,
    body: PresetSaveBody,
    options?: { signal?: AbortSignal }
  ): Promise<PresetDetailResponse>;
  deletePreset(
    presetId: string,
    options?: { signal?: AbortSignal }
  ): Promise<{ ok: true; deleted: PresetSummary }>;
  commitPrepare(
    body: CommitPrepareBody,
    options?: { signal?: AbortSignal }
  ): Promise<CommitPrepareResponse>;
  commitApply(body: CommitBody, options?: { signal?: AbortSignal }): Promise<CommitResponse>;
  parseSchema(raw: unknown): SurfaceSchema;
}

export interface WorkspaceOptions {
  endpoints: WorkspaceEndpoints;
  history?: DraftHistory;
  pollIntervalMs?: number;
  jobTimeoutMs?: number;
  now?: () => number;
  /**
   * 自动预览的**初始**开关，默认 `true`。
   *
   * 做成可注入的，是为了让测试能关掉它 —— 否则每个用例都会在 300ms 后
   * 冒出一个真实计时器，用例之间互相干扰。生产默认值就是 `true`。
   */
  autoPreview?: boolean;
}

const IDLE_CONNECTION: ConnectionState = {
  status: "unknown",
  target: "",
  latencyMs: null,
  checkedAt: null,
  message: null,
};

const DEFAULT_FRAMING: FramingChoice = {
  mode: "current_camera",
  margin: 0.15,
  isolateMaterial: null,
};

// -- 自动预览 ------------------------------

/**
 * 连续输入（数字框打字、色标拖动）**停手**多久之后自动预览。
 *
 * 300ms 是「手还在动」与「这一下改完了」之间的分界：再短会把一次拖动拆成好几张图，
 * 再长会让人觉得界面没反应。
 */
export const AUTO_PREVIEW_INPUT_MS = 300;

/**
 * 一次操作**结束**（输入框失焦 / 松开色标 / 颜色确认）之后多久自动预览。
 *
 * 比连续输入短得多：动作已经结束了，没有理由再等 —— 但仍留一小段，
 * 好让「失焦后紧接着改下一个控件」这类连续操作合并成一次渲染。
 */
export const AUTO_PREVIEW_COMMIT_MS = 120;

/**
 * 自动预览的完整交互规则（就这两条）：
 *
 * | 触发 | 等待 | 语义 |
 * |---|---|---|
 * | 任何草稿改动 | {@link AUTO_PREVIEW_INPUT_MS} | 连续输入：停手之后才算一次 |
 * | `sealHistory()`（失焦 / 松开拖动 / 颜色确认） | {@link AUTO_PREVIEW_COMMIT_MS} | 操作结束：立刻预览 |
 *
 * **快速连续修改只执行最后一次**：每次改动都重启计时器，因此排期天然收敛到最后一次。
 * 计时器到点时若已有任务在跑，**不提交**（否则一次拖动会打断好几轮渲染），
 * 改由任务终态回收时再排一次。
 */

/**
 * 把界面上的取景选择翻成请求载荷。
 *
 * 不隔离时**不带** `isolate_material` 字段（而不是带一个 `null`）——
 * 服务端的 framing 契约是 `extra="forbid"`，少发一个字段永远比发一个语义待定的
 * 字段安全；`null` 与空串服务端虽然也接受，但没必要依赖那条归一化。
 */
export function framingPayload(choice: FramingChoice): FramingSubmitOptions {
  const payload: FramingSubmitOptions = { mode: choice.mode, margin: choice.margin };
  if (choice.isolateMaterial) {
    payload.isolate_material = choice.isolateMaterial;
  }
  return payload;
}

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
  /** 自动预览的排期计时器；`null` = 没有排期。 */
  let autoPreviewTimer: ReturnType<typeof setTimeout> | null = null;

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
    autoPreview: options.autoPreview ?? true,
    autoPreviewPending: false,
    save: emptySaveState(),
    presets: emptyPresetState(),
  });

  function syncHistoryDepths(): void {
    store.setState({
      undoDepth: history.undoDepth,
      redoDepth: history.redoDepth,
      canUndo: history.undoDepth > 0,
      canRedo: history.redoDepth > 0,
    });
  }

  /** 取消自动预览排期（结构失效 / 刷新基线 / 手动提交 / 卸载时都要走这里）。 */
  function cancelAutoPreview(): void {
    if (autoPreviewTimer !== null) {
      clearTimeout(autoPreviewTimer);
      autoPreviewTimer = null;
    }
    if (store.getState().autoPreviewPending) {
      store.setState({ autoPreviewPending: false });
    }
  }

  /**
   * 自动预览是否**确实有活要干** —— 也就是「画面与当前草稿不一致」。
   *
   * 三种「没活」的情形必须分开判，否则会凭空渲染：
   *
   * * 已经有画面、且画面就是当前草稿（`previewStale === false`）；
   * * **还没有任何画面，草稿也还是空的** —— 首张图由基线流程负责，
   *   不该由自动预览代劳（否则一次单纯的失焦就会渲染一张空草稿的图）；
   * * 结构失效（由 `scheduleAutoPreview` 统一挡掉，这里不重复判）。
   */
  function autoPreviewHasWork(): boolean {
    const state = store.getState();
    if (state.previewStale) {
      return true;
    }
    if (state.lastSuccessfulPreview) {
      return false;
    }
    return state.dirtyIds.size > 0;
  }

  /**
   * 排一次自动预览。**每次调用都重启计时器** —— 这就是「快速连续修改只执行最后一次」。
   *
   * 关掉开关 / 结构失效 / 还没有 schema / 没有活要干时直接返回：不排期、也不留下
   * `pending`，免得界面显示一个永远不会兑现的「待预览」。
   */
  function scheduleAutoPreview(delayMs: number): void {
    const state = store.getState();
    if (!state.autoPreview || state.historyBlocked || !state.schema) {
      return;
    }
    if (!autoPreviewHasWork()) {
      return;
    }
    if (autoPreviewTimer !== null) {
      clearTimeout(autoPreviewTimer);
    }
    store.setState({ autoPreviewPending: true });
    autoPreviewTimer = setTimeout(() => {
      autoPreviewTimer = null;
      void runAutoPreview();
    }, delayMs);
  }

  /**
   * 排期到点：该渲染就渲染。
   *
   * 三条提前返回，各有理由：
   *
   * * 开关被关掉 / 结构失效 / 没有活要干 —— 不渲染；
   * * **已有任务在跑** —— 不渲染。这里刻意不「取代」，否则一次拖动会把在途渲染
   *   打断好几轮；改由终态回收时（`handleTerminal`）再排一次，最多多一张图。
   */
  async function runAutoPreview(): Promise<void> {
    const state = store.getState();
    if (!state.autoPreview || state.historyBlocked) {
      store.setState({ autoPreviewPending: false });
      return;
    }
    store.setState({ autoPreviewPending: false });
    if (!autoPreviewHasWork()) {
      return;
    }
    if (state.activeJob !== null || state.loading) {
      return;
    }
    await actions.applyAndPreview();
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
    // 连续输入：停手 300ms 才算一次改动（`sealHistory` 会把等待缩短到 120ms）。
    scheduleAutoPreview(AUTO_PREVIEW_INPUT_MS);
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
      isolation: (result.isolation as IsolationSummary | undefined) ?? null,
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

    // 自动预览的回收点：排期到点时若已有任务在跑，那次改动就没被渲染。
    // **只在成功时续排** —— 失败时续排会对同一份必然失败的草稿反复重试，
    // 把错误日志刷满，也让用户以为界面卡住了；失败就交给用户决定下一步。
    const state = store.getState();
    if (outcome === "succeeded" && state.autoPreview && !state.historyBlocked && state.previewStale) {
      scheduleAutoPreview(AUTO_PREVIEW_COMMIT_MS);
    }
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

  async function refreshBaseline(choice?: Partial<FramingChoice>): Promise<boolean> {
    const framing: FramingChoice = { ...store.getState().framingChoice, ...(choice ?? {}) };
    // 上一次的基线预览若还挂着，这次刷新同样会取代它 —— 先如实改判，别让它停在 pending。
    baselineJobId = null;
    // 刷新基线会清空草稿：任何在途的自动预览排期都已无意义，必须取消，
    // 否则它会在新基线上渲染一份**已经不存在**的草稿。
    cancelAutoPreview();
    store.setState({
      loading: true,
      error: null,
      notice: null,
      framingChoice: framing,
      baselinePreviewStatus: "pending",
      baselinePreviewUrl: null,
      // 刷新基线会让服务端手里的确认令牌全部失效（`BASELINE_STALE`）。
      // 面板上那条「即将覆盖 …」的确认信息指向的就是那个令牌，必须一起撤掉 ——
      // 留着它，用户点确认时才发现要重来一次。
      save: {
        ...store.getState().save,
        phase: "idle",
        confirm: null,
        error: null,
        steps: [],
        status: null,
      },
    });
    let created: Awaited<ReturnType<WorkspaceEndpoints["createBaseline"]>>;
    try {
      // 基线的首张预览同样尊重当前的隔离选择：否则「开关显示隔离中、图却是全体」
      // 这种自相矛盾的状态会让用户以为隔离没生效。
      created = await endpoints.createBaseline({ framing: framingPayload(framing) });
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
    // 隔离的材质在工程里找不到：**当场关掉隔离**，否则用户点几次「应用并预览」都会
    // 撞同一堵墙。关掉这件事必须说出来 —— 静默改掉用户的开关比报错更糟。
    if (detail.code === "ISOLATE_MATERIAL_MISSING") {
      store.setState({
        ...patch,
        error: detail,
        loading: false,
        framingChoice: { ...store.getState().framingChoice, isolateMaterial: null },
        notice: "隔离预览已关闭（该材质在当前工程里找不到），下次预览将显示全部材质。",
      });
      return;
    }
    store.setState({ ...patch, error: detail, loading: false });
  }

  // -- 保存闭环：预设 + 应用到工程 ---------------------------------------

  function patchSave(patch: Partial<SaveState>): void {
    store.setState({ save: { ...store.getState().save, ...patch } });
  }

  function patchPresets(patch: Partial<PresetState>): void {
    store.setState({ presets: { ...store.getState().presets, ...patch } });
  }

  /** 保存的当前载荷：草稿拆成 L0 / 参数面两半，带上客户端持有的结构指纹。 */
  function savePayload(mode: SaveMode, targetPath: string | null): {
    mode: SaveMode;
    targetPath: string | null;
    l0: Record<string, unknown>;
    surface: Record<string, unknown>;
    structureHash: string | null;
  } {
    const state = store.getState();
    const { l0, surface } = splitSaveDraft(state.schema, state.draft);
    return { mode, targetPath, l0, surface, structureHash: state.structureHash };
  }

  /**
   * 保存成功后把基线**读回来**并与草稿对齐。
   *
   * 为什么不直接调 `refreshBaseline()`：服务端在 `commit` 内部已经重新采集过基线
   * （`baseline_refreshed`），再走一次 `createBaseline` 会白渲染一张与刚才那张
   * 一模一样的图 —— 而这次渲染的唯一作用只是让前端知道 `baseline_id`。
   * 读一次只读接口就够了。
   *
   * 草稿必须清空：磁盘上的内容现在**就是**那份草稿，留着它会让下一次保存
   * 把同一批值再写一遍，也会让「N 项已改」的提示变成假话。
   */
  async function syncBaselineAfterSave(): Promise<void> {
    await readBaseline();
    history.clear();
    syncHistoryDepths();
    draftVersion = 0;
    previewedVersion = 0;
    previewedVersionForSubmit = 0;
    cancelAutoPreview();
    store.setState({
      draft: emptyDraft(),
      dirtyIds: new Set<string>(),
      effective: {},
      previewStale: false,
      historyBlocked: false,
      notice: "已保存到工程文件：基线已按新内容刷新，草稿已清空。",
    });
  }

  /** 执行保存（消费令牌）。失败时**只**写保存状态，绝不碰草稿与最后一张成功预览。 */
  async function runCommit(prepared: CommitPrepareResponse): Promise<boolean> {
    const payload = savePayload(
      prepared.mode === "overwrite" ? "overwrite" : "save_as",
      // 目标路径一律以**票据**为准：传 null 表示「用你确认过的那条」，
      // 服务端会自己回填。前端再传一次只会多一处可以不一致的地方。
      null
    );
    const body: CommitBody = {
      token: prepared.token,
      mode: payload.mode,
      draft: payload.l0,
      surface_draft: payload.surface,
      structure_hash: payload.structureHash,
    };
    patchSave({ phase: "committing", error: null, confirm: null, steps: ["已签发确认令牌"] });
    try {
      const result = await endpoints.commitApply(body);
      const outcome: SaveOutcome = {
        saved: Boolean(result.saved),
        mode: result.mode,
        targetPath: result.target_path,
        backupPath: result.backup_path ?? null,
        steps: result.steps ?? [],
        warnings: result.warnings ?? [],
        savedAt: result.saved_at ?? null,
        status: result.status ?? null,
        baselineRefreshed: Boolean(result.baseline_refreshed),
      };
      patchSave({ phase: result.saved ? "saved" : "failed", outcome, status: result.status ?? null, steps: outcome.steps });
      if (result.saved) {
        await syncBaselineAfterSave();
      } else {
        // `saved: false` 但没有抛错：服务端明确说没落盘。草稿一律保留。
        store.setState({ notice: saveFailureNotice(result.status ?? null) });
      }
      return Boolean(result.saved);
    } catch (error) {
      const detail = toDetail(error);
      const status = commitStatusOf(detail);
      // 走统一错误路由：结构失效（STRUCTURE_CHANGED / IDENTITY_MISSING）必须在这里
      // 就作废草稿与已签发的令牌，否则用户能拿一份作废的草稿继续保存。
      routeErrorDetail(detail);
      patchSave({ phase: "failed", error: detail, status, steps: commitStepsOf(detail) });
      return false;
    }
  }

  /**
   * 准备保存。
   *
   * `SAVE_CONFIRM_REQUIRED` **不是失败**：它表示这次会覆盖已有文件、而用户还没看过
   * 绝对路径与备份位置。这条路径把两条路径摊开、停在 `confirming`，等用户点确认。
   * 把它当错误渲染会让用户以为出了故障，而其实只是流程要求他看一眼。
   */
  async function prepareSave(confirm: boolean): Promise<boolean> {
    const state = store.getState();
    if (!state.schema) {
      store.setState({ error: { code: "NO_SCHEMA", message: "尚未加载 schema。", retryable: true } });
      return false;
    }
    if (state.historyBlocked) {
      store.setState({
        error: {
          code: "STRUCTURE_CHANGED",
          message: "结构已变化，草稿已作废。请先刷新基线再保存。",
          retryable: true,
        },
      });
      return false;
    }
    if (saveBusy(state.save)) {
      return false;
    }
    const mode = state.save.mode;
    let targetPath: string | null = null;
    if (mode === "save_as") {
      const problem = saveTargetProblem(state.save.targetPath);
      if (problem) {
        patchSave({
          phase: "idle",
          error: { code: "SAVE_TARGET_INVALID", message: problem, retryable: false },
        });
        return false;
      }
      targetPath = state.save.targetPath.trim();
    }

    const payload = savePayload(mode, targetPath);
    // 第二次确认必须带上第一次拿到的确认单号：服务端据此沿用**已展示给用户**的
    // 备份路径签发令牌（两步之间跨过整秒边界，展示路径也不会与实际生成的对不上），
    // 并复核基线 / 草稿 / 模式 / 目标都没变。`state` 是本函数开头取的快照，
    // 下面 patchSave 清空 save.confirm 不影响这里读到的值。
    const confirmationId = confirm ? state.save.confirm?.confirmationId || null : null;
    patchSave({
      phase: "preparing",
      error: null,
      confirm: null,
      outcome: null,
      status: null,
      steps: ["校验基线 / 草稿 / 保存模式 / 目标路径"],
    });
    try {
      const prepared = await endpoints.commitPrepare({
        mode: payload.mode,
        draft: payload.l0,
        target_path: payload.targetPath,
        confirm,
        confirmation_id: confirmationId,
        surface_draft: payload.surface,
        structure_hash: payload.structureHash,
      });
      patchSave({ confirm: confirmFromPrepared(prepared) });
      return await runCommit(prepared);
    } catch (error) {
      const detail = toDetail(error);
      if (detail.code === "SAVE_CONFIRM_REQUIRED") {
        const confirmInfo = confirmFromError(detail);
        if (confirmInfo) {
          patchSave({
            phase: "confirming",
            confirm: confirmInfo,
            error: null,
            steps: ["等待确认：这次会覆盖已有文件"],
          });
          return false;
        }
        // details 形状对不上：**不编路径**，如实说拿不到待确认的信息。
        patchSave({
          phase: "failed",
          confirm: null,
          error: {
            ...detail,
            message: "服务端要求确认覆盖，但没有给出目标路径与备份路径；请重新准备。",
          },
        });
        return false;
      }
      routeErrorDetail(detail);
      patchSave({ phase: "failed", error: detail, steps: commitStepsOf(detail) });
      return false;
    }
  }

  /** 保存预设的请求体：L0 留空、参数面草稿整份带上，另附当前的取景设置。 */
  function presetPayload(name: string): PresetSaveBody {
    const state = store.getState();
    const { surface } = splitSaveDraft(state.schema, state.draft);
    return {
      name,
      framing_mode: state.framingChoice.mode,
      framing_margin: state.framingChoice.margin,
      // L0 参数没有搬进 v4 界面，因此这一半一定是空的 —— 不假装有。
      parameters: {},
      surface,
    };
  }

  async function refreshPresets(): Promise<void> {
    patchPresets({ loading: true, error: null });
    try {
      const payload = await endpoints.listPresets();
      patchPresets({
        loading: false,
        list: payload.presets ?? [],
        skipped: payload.skipped ?? [],
        storage: payload.storage ?? "",
        error: null,
      });
    } catch (error) {
      patchPresets({ loading: false, error: toDetail(error) });
    }
  }

  /**
   * 保存当前草稿为预设（`presetId` 给定时为覆盖保存）。
   *
   * 同名冲突（`PRESET_NAME_CONFLICT`）走保存状态里的 error，**不**动全局错误横幅：
   * 那是一个可以在面板里当场改名字解决的问题，不该同时弹两条提示。
   */
  async function savePreset(name: string, presetId?: string): Promise<boolean> {
    const state = store.getState();
    if (!state.schema) {
      patchPresets({ error: { code: "NO_SCHEMA", message: "尚未加载 schema。", retryable: true } });
      return false;
    }
    const trimmed = name.trim();
    if (!trimmed) {
      patchPresets({
        error: { code: "PRESET_INVALID", message: "请填写预设名称。", retryable: false },
      });
      return false;
    }
    patchPresets({ busy: true, error: null, notice: null });
    try {
      const body = presetPayload(trimmed);
      const result = presetId
        ? await endpoints.updatePreset(presetId, body)
        : await endpoints.createPreset(body);
      const count = Object.keys(result.preset.surface ?? {}).length;
      patchPresets({
        busy: false,
        notice: presetId
          ? `已覆盖预设「${result.preset.name}」（参数面 ${count} 项）。`
          : `已保存预设「${result.preset.name}」（参数面 ${count} 项）。`,
      });
      await refreshPresets();
      return true;
    } catch (error) {
      patchPresets({ busy: false, error: toDetail(error) });
      return false;
    }
  }

  /**
   * 套用预设到草稿。
   *
   * 三件事必须做到：
   *
   * * 走 `commitCommand` —— 套用预设要能**撤销**，否则用户按错一下就得手动改回去；
   * * 只写当前工程认得的 id，并把跳过的**数量**说出来（跨工程套用必然对不上）；
   * * 结构失效期间拒绝：草稿已经作废，往里面写东西毫无意义。
   */
  async function applyPreset(presetId: string): Promise<boolean> {
    const state = store.getState();
    if (!state.schema) {
      patchPresets({ error: { code: "NO_SCHEMA", message: "尚未加载 schema。", retryable: true } });
      return false;
    }
    if (state.historyBlocked) {
      patchPresets({
        error: {
          code: "STRUCTURE_CHANGED",
          message: "结构已变化，草稿已作废。请先刷新基线再套用预设。",
          retryable: true,
        },
      });
      return false;
    }
    patchPresets({ busy: true, error: null, notice: null });
    try {
      const { preset } = await endpoints.readPreset(presetId);
      const { applied, skipped } = presetDraftFor(state.schema, preset.surface ?? {});
      const next: SurfaceDraft = { ...store.getState().draft };
      for (const [paramId, value] of Object.entries(applied)) {
        next[paramId] = value;
      }
      const count = Object.keys(applied).length;
      if (count > 0) {
        commitCommand({
          kind: "applyPreset",
          label: `套用预设 ${preset.name}`,
          groupId: "",
          groupIds: topGroups(state.schema).map((group) => group.id),
          next,
          mergeKey: null,
        });
      }
      patchPresets({
        busy: false,
        notice:
          skipped.length > 0
            ? `已套用「${preset.name}」${count} 项；另有 ${skipped.length} 项在当前工程里找不到（可能来自别的工程或已被重命名），已跳过。`
            : `已套用「${preset.name}」${count} 项。`,
      });
      return true;
    } catch (error) {
      patchPresets({ busy: false, error: toDetail(error) });
      return false;
    }
  }

  async function deletePreset(presetId: string): Promise<boolean> {
    patchPresets({ busy: true, error: null, notice: null });
    try {
      const result = await endpoints.deletePreset(presetId);
      patchPresets({ busy: false, notice: `已删除预设「${result.deleted.name}」。` });
      await refreshPresets();
      return true;
    } catch (error) {
      patchPresets({ busy: false, error: toDetail(error) });
      return false;
    }
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
    setFramingChoice(choice: Partial<FramingChoice>): void {
      store.setState({ framingChoice: { ...store.getState().framingChoice, ...choice } });
    },
    /**
     * 切换隔离预览的材质并立即重新渲染。
     *
     * 传 `null` = 回到「显示全部材质」。隔离**必须重新渲染**才生效 ——
     * 它改的是渲染时的可见性，不是已落盘的那张图；所以这里不只是一个状态切换。
     * 结构失效（`historyBlocked`）时交给 `applyAndPreview` 统一拒绝。
     */
    isolateMaterial(name: string | null): Promise<boolean> {
      return actions.applyAndPreview({ isolateMaterial: name });
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
      // 操作结束（输入框失焦 / 松开色标 / 颜色确认）：把自动预览的等待从 300ms 缩到 120ms。
      // 每次调用都重启计时器，因此「失焦后紧接着改下一个控件」仍会合并成一次渲染。
      scheduleAutoPreview(AUTO_PREVIEW_COMMIT_MS);
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

    async applyAndPreview(choice?: Partial<FramingChoice>): Promise<boolean> {
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
      const framing: FramingChoice = { ...state.framingChoice, ...(choice ?? {}) };
      const draft = sanitizeDraft(state.schema, state.draft);
      previewedVersionForSubmit = draftVersion;
      // 手动提交即「立刻渲染」：把排期撤掉，免得刚渲染完又立刻被自动预览再渲染一次。
      cancelAutoPreview();
      store.setState({ loading: true, error: null, notice: null, framingChoice: framing });
      // 新任务会取代在途的基线预览 —— 那次基线图不可能再被我们跟踪到，如实改判。
      abandonPendingBaselinePreview();
      const payload: JobSubmitPayload = {
        draft,
        framing: framingPayload(framing),
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
      // 结构失效期间绝不自动渲染：草稿已经作废，排期必须一起撤掉。
      cancelAutoPreview();
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
        // 令牌真的失效了（服务端会以 `BASELINE_STALE` / `STRUCTURE_CHANGED` 拒绝），
        // 面板上那条确认信息也必须撤掉 —— 否则用户点「确认覆盖」时才发现白点了一下。
        save: {
          ...store.getState().save,
          phase: "idle",
          confirm: null,
          error: null,
          steps: [],
          status: null,
        },
      });
    },

    handleError(error: unknown): void {
      routeErrorDetail(toDetail(error));
    },

    /**
     * 开关自动预览。
     *
     * 打开时如果**当前草稿还没被渲染**，立刻排一次（用「操作结束」的短等待）——
     * 否则用户打开了开关却什么都没发生，会以为开关坏了。
     */
    setAutoPreview(enabled: boolean): void {
      store.setState({ autoPreview: enabled });
      if (!enabled) {
        cancelAutoPreview();
        return;
      }
      // 打开时如果确实有未渲染的改动，立刻排一次（`scheduleAutoPreview` 自己会判有没有活）；
      // 否则用户打开了开关却什么都没发生，会以为开关坏了。
      scheduleAutoPreview(AUTO_PREVIEW_COMMIT_MS);
    },

    cancelAutoPreview(): void {
      cancelAutoPreview();
    },

    // -- 保存闭环 ------------------------

    /**
     * 打开保存面板（默认「另存为」，不动用户已有的工程文件）。
     *
     * 清掉的是**这一次尝试**的工作状态（待确认 / 错误 / 步骤 / 四态表），
     * 保留 `outcome` —— 那是一条已经发生的事实（「上次保存到了哪个文件」），
     * 用户下次打开还该看得见。失败提示反而会随新的一次尝试消失，因为
     * 那描述的是上一次尝试，重开面板时再摆一遍只会让人以为又失败了一次。
     */
    openSaveDialog(mode: SaveMode = "save_as"): void {
      patchSave({
        open: true,
        mode,
        phase: "idle",
        error: null,
        status: null,
        steps: [],
        confirm: null,
      });
    },

    closeSaveDialog(): void {
      // 关闭只收起面板：**不清** outcome / confirm —— 用户下次打开还能看到
      // 刚才写到了哪里、备份在哪。真正清空的是「重新准备」那条路径。
      patchSave({ open: false });
    },

    /**
     * 切换保存模式。
     *
     * 切换会丢掉**这一次尝试**的东西：确认信息、四态表、步骤、错误。
     * 确认过的是「写到这个文件的这条路径」，模式一换，那个确认就不再指向同一件事，
     * 留着它等于让用户确认一件已经变了的事。
     * `outcome` 不动 —— 它是上一次保存的记录，不因为切换模式而失真。
     */
    setSaveMode(mode: SaveMode): void {
      patchSave({ mode, phase: "idle", confirm: null, status: null, steps: [], error: null });
    },

    setSaveTarget(path: string): void {
      patchSave({ targetPath: path });
    },

    /** 准备保存。`confirm=true` 表示用户已经在面板上看过绝对路径与备份位置。 */
    prepareSave(confirm = false): Promise<boolean> {
      return prepareSave(confirm);
    },

    refreshPresets,
    savePreset,
    applyPreset,
    deletePreset,

    /**
     * 打开预设面板并**顺带拉一次列表**。
     *
     * 不做缓存：预设目录是用户（或别的工具）可以直接动的，缓存一份过期列表
     * 会让「我明明删了它还在」。列表很便宜，每次都拉。
     */
    openPresetPanel(): void {
      patchPresets({ open: true });
      void refreshPresets();
    },

    closePresetPanel(): void {
      patchPresets({ open: false });
    },

    clearPresetError(): void {
      patchPresets({ error: null });
    },

    clearPresetNotice(): void {
      patchPresets({ notice: null });
    },

    saveBusy(): boolean {
      return saveBusy(store.getState().save);
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
      // 卸载后计时器绝不能还活着：它会在一个已卸载的工作台上提交任务。
      cancelAutoPreview();
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

/**
 * 失败响应里的四态状态块（`details.status`）。
 *
 * 取不到就返回 `null` —— **不报假绿**：编一个「全部 false」的状态出来，
 * 用户会以为服务端明确说了「什么都没写」，而其实我们只是没拿到证据。
 */
export function commitStatusOf(detail: ErrorDetail): CommitStatus | null {
  const raw = (detail.details ?? {}) as Record<string, unknown>;
  const status = raw.status;
  if (status === null || typeof status !== "object" || Array.isArray(status)) {
    return null;
  }
  return status as CommitStatus;
}

/** 失败响应里的步骤轨迹（走到哪一步断的）。取不到就空数组。 */
export function commitStepsOf(detail: ErrorDetail): string[] {
  const raw = (detail.details ?? {}) as Record<string, unknown>;
  return Array.isArray(raw.steps)
    ? raw.steps.filter((item): item is string => typeof item === "string")
    : [];
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
