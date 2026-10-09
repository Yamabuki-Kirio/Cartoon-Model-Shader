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
import { JobRunner, type JobSubmitPayload } from "./jobs";
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

export interface WorkspaceState {
  schema: SurfaceSchema | null;
  baseline: SurfaceBaselinePublic | null;
  baselineId: string | null;
  baselineCapturedAt: string | null;
  baselinePreviewUrl: string | null;
  draft: SurfaceDraft;
  effective: Record<string, unknown>;
  structureHash: string | null;
  externalChanges: ExternalChange[];
  dirtyIds: Set<string>;
  activeJob: JobState | null;
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

  const store: Store<WorkspaceState> = createStore<WorkspaceState>({
    schema: null,
    baseline: null,
    baselineId: null,
    baselineCapturedAt: null,
    baselinePreviewUrl: null,
    draft: emptyDraft(),
    effective: {},
    structureHash: null,
    externalChanges: [],
    dirtyIds: new Set<string>(),
    activeJob: null,
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
    store.setState({
      activeJob: job,
      lastSuccessfulPreview: preview,
      // 新草稿产生时旧任务结果**不写进 effective**，并标记画面对应的草稿已过期。
      ...(stale ? {} : { effective: { ...store.getState().effective, ...applied } }),
      previewStale: draftVersion !== previewedVersion,
      externalChanges: external,
      error: null,
      loading: false,
      notice: stale
        ? "本次画面基于提交时的草稿；你在渲染期间又改过草稿，已标记为过期。"
        : null,
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
      onFailed: (job, error) => {
        // ⚠ 只写 error，**不动** lastSuccessfulPreview ——
        //   失败画面不得替换最后一张成功预览。
        store.setState({ activeJob: job, error: jobErrorToDetail(error), loading: false });
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

  async function refreshBaseline(choice?: FramingChoice): Promise<boolean> {
    const framing = choice ?? store.getState().framingChoice;
    store.setState({ loading: true, error: null, notice: null, framingChoice: framing });
    try {
      const created = await endpoints.createBaseline({ framing });
      runner.dispose();
      history.clear();
      syncHistoryDepths();
      const schema = await loadSchema();
      const surface = created.surface ?? null;
      draftVersion = 0;
      previewedVersion = 0;
      previewedVersionForSubmit = 0;
      store.setState({
        baselineId: created.baseline_id ?? null,
        baselineCapturedAt: created.captured_at ?? null,
        baseline: surface,
        baselinePreviewUrl: created.preview_url ?? null,
        draft: emptyDraft(),
        dirtyIds: new Set<string>(),
        effective: {},
        structureHash: surface?.structure_hash ?? null,
        externalChanges: [],
        previewStale: false,
        historyBlocked: false,
        activeJob: null,
        loading: false,
        notice:
          surface && surface.available === false
            ? "v4 拓扑探针未成功，Cel 参数暂不可用（L0 与保存流程不受影响）。"
            : null,
        activeGroupId: store.getState().activeGroupId ?? firstGroupId(schema),
      });
      return true;
    } catch (error) {
      store.setState({ error: toDetail(error), loading: false });
      return false;
    }
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
        // 所以这里只在服务端真的给了它时才填，否则「基线图」按钮保持禁用 ——
        // 宁可少一个按钮，也不要指向一张不存在的图。
        ...(typeof baseline.preview_url === "string"
          ? { baselinePreviewUrl: baseline.preview_url }
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
    clearError(): void {
      store.setState({ error: null, historyBlocked: false });
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
    applyStructureFatal(detail: ErrorDetail): void {
      runner.dispose();
      history.clear();
      syncHistoryDepths();
      draftVersion += 1;
      store.setState({
        draft: emptyDraft(),
        dirtyIds: new Set<string>(),
        historyBlocked: true,
        error: detail,
        notice: "工程结构已变化：草稿与已签发的保存确认令牌均已作废，请刷新基线。",
        loading: false,
      });
    },

    handleError(error: unknown): void {
      const detail = toDetail(error);
      if (
        error instanceof AppError &&
        (error.code === "STRUCTURE_CHANGED" || error.code === "IDENTITY_MISSING")
      ) {
        actions.applyStructureFatal(detail);
        return;
      }
      store.setState({ error: detail, loading: false });
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
