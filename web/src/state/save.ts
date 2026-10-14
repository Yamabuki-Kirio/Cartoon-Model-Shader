/**
 * 保存闭环的**纯逻辑**：状态形状、状态派生、载荷拼装、失败文案。
 *
 * 这里不放任何状态变更 —— 变更一律收在 `workspace.ts` 里（见那里的模块注释）。
 * 分开的理由很实际：这一层可以只靠输入输出测，不需要造一个工作台、
 * 不需要假计时器，用例短得多，也更容易看出「哪一条规则」被改坏了。
 *
 * 三条贯穿全篇的准则：
 *
 * 1. **失败绝不改草稿、绝不改最后一张成功预览。** 用户花时间调出来的东西，
 *    不能因为一次保存失败而消失。服务端也承诺 `draft_preserved: true`。
 * 2. **覆盖必须二次确认，且确认时要把绝对路径与备份路径摊开。**
 *    这不是流程装饰：`save_as` 落到一个已存在的文件同样是覆盖。
 * 3. **不报假绿。** 拿不到证据的状态一律 `null`（未知），不写成 `false` 也不写成 `true`。
 */

import type { CommitStatus, ErrorDetail, SaveMode } from "../api/types";
import type { SurfaceDraft, SurfaceSchema } from "../schema/types";
import { graphNodeIndex, splitGraphDraft, type MaterialGraphSchema } from "../schema/graph";
import { sanitizeDraft } from "./draft";

export type SavePhase =
  /** 没有进行中的保存（面板可以开着，也可以是关的）。 */
  | "idle"
  /** 正在 prepare：只校验，**不写盘**。 */
  | "preparing"
  /** 服务端要求二次确认（这次会覆盖已有文件）。 */
  | "confirming"
  /** 正在 commit：应用 → 回读 → 备份 → 保存。 */
  | "committing"
  | "saved"
  | "failed";

/**
 * 二次确认要展示的东西。
 *
 * 三个字段都来自服务端 `SAVE_CONFIRM_REQUIRED` 的 `details`（或 prepare 成功的响应），
 * **不是**前端自己拼的 —— 用户要确认的是「服务端真的会写到这里」，
 * 前端猜一条路径出来就等于让用户确认一件没发生的事。
 */
export interface SaveConfirmInfo {
  mode: SaveMode;
  /** 绝对路径。保存链路刻意保留真实路径，这是既有的二次确认约定。 */
  targetPath: string;
  /** 预计的备份路径；`null` = 这次不会生成备份（目标文件不存在）。 */
  backupPath: string | null;
  warnings: string[];
  /**
   * 第一次 prepare 返回的确认单号。点「确认并覆盖」时随第二次 prepare 回传，
   * 服务端据此**沿用这里展示的备份路径**签发令牌 —— 否则用户阅读路径超过 1 秒，
   * 确认框里的路径与实际生成的备份路径就会对不上。
   *
   * 空字符串 = 服务端没给（契约对不上）；此时按旧行为确认，并在成功后以
   * save-ok / 状态表里的真实路径为准。
   */
  confirmationId: string;
}

/** 一次保存的**终态**快照（成功或失败都可能有）。 */
export interface SaveOutcome {
  saved: boolean;
  mode: string;
  targetPath: string;
  backupPath: string | null;
  steps: string[];
  warnings: string[];
  savedAt: string | null;
  status: CommitStatus | null;
  baselineRefreshed: boolean;
}

export interface SaveState {
  /** 保存面板是否打开。 */
  open: boolean;
  phase: SavePhase;
  /** 默认**另存为**：不动用户已有的工程文件是最安全的默认。 */
  mode: SaveMode;
  /** 用户输入的另存为路径（`overwrite` 时不用它）。 */
  targetPath: string;
  confirm: SaveConfirmInfo | null;
  outcome: SaveOutcome | null;
  /** 真正的失败（`SAVE_CONFIRM_REQUIRED` **不算** —— 那是待确认）。 */
  error: ErrorDetail | null;
  /** 失败时也能说出「走到哪一步了」。 */
  steps: string[];
  status: CommitStatus | null;
}

export function emptySaveState(): SaveState {
  return {
    open: false,
    phase: "idle",
    mode: "save_as",
    targetPath: "",
    confirm: null,
    outcome: null,
    error: null,
    steps: [],
    status: null,
  };
}

/** 保存流程是否正忙（按钮据此禁用，避免重复签发令牌）。 */
export function saveBusy(save: SaveState): boolean {
  return save.phase === "preparing" || save.phase === "committing";
}

// -- 载荷 ------------------------

/**
 * 把工作台的草稿拆成保存接口要的两半。
 *
 * v4 工作台的草稿**只能**由 v4 schema 的 id 构成（见 `draft.sanitizeDraft`），
 * 所以它整份都属于参数面草稿，L0 那一半是空的。这不是偷懒：
 *
 * * L0 参数并没有被搬进递归 schema，界面上也就改不到它们；
 * * 服务端在应用时会自己把「基线 + 草稿」合成**完整** L0 取值
 *   （`validate_draft` 的既有不变量），所以 L0 那半留空 = 不改动 L0，正是我们要的。
 *
 * 将来 L0 参数真的接进 schema 之后，拆分点就在这一个函数里 ——
 * 别处不需要知道这件事。
 */
export function splitSaveDraft(
  schema: SurfaceSchema | null,
  draft: SurfaceDraft
): { l0: Record<string, unknown>; surface: Record<string, unknown> } {
  return { l0: {}, surface: sanitizeDraft(schema, draft) };
}

/** 保存载荷的三半：L0 / v4 参数面 / 材质图（PR #7）。 */
export interface SaveDraftSplit {
  l0: Record<string, unknown>;
  surface: Record<string, unknown>;
  graph: Record<string, unknown>;
}

/**
 * 把工作台的草稿拆成保存接口要的三半。
 *
 * 与 {@link splitSaveDraft} 的差别只有材质图：材质图参数 id 走 `material_draft`，
 * 混进 `surface_draft` 只会被服务端当未知参数拒掉。拆分点收在这里 ——
 * 别处不需要知道哪些 id 属于哪一边。
 */
export function splitSaveDraftFull(
  schema: SurfaceSchema | null,
  graphSchema: MaterialGraphSchema | null,
  draft: SurfaceDraft
): SaveDraftSplit {
  const { surface, graph } = splitGraphDraft(graphSchema, draft);
  return {
    // L0 那一半留空 = 不改动 L0（服务端用「基线 + 草稿」合成完整取值）。
    l0: {},
    surface: sanitizeDraft(schema, surface),
    graph: sanitizeDraft(null, graph, graphNodeIndex(graphSchema)),
  };
}

/**
 * 另存为路径的**本地**预检。
 *
 * 只拦「一定不合法」的两种（相对路径 / 后缀不对），目的是别让用户按下一个
 * 必然 422 的按钮。真正的合法性（能不能写、盘上有没有、权限）只有服务端知道，
 * 这里**不猜**。
 */
export function saveTargetProblem(path: string): string | null {
  const text = path.trim();
  if (!text) {
    return "请填写要保存到的绝对路径（以 .blend 结尾）。";
  }
  const absolute = /^[A-Za-z]:[\\/]/.test(text) || text.startsWith("/") || text.startsWith("\\\\");
  if (!absolute) {
    return "需要绝对路径（例如 D:\\项目\\角色.blend）。";
  }
  if (!/\.blend$/i.test(text)) {
    return "路径必须以 .blend 结尾。";
  }
  return null;
}

/**
 * 从 `SAVE_CONFIRM_REQUIRED` 的 `details` 里取出确认信息。
 *
 * `details` 形状不对时返回 `null` —— 那说明我们与服务端的契约对不上了，
 * 这时候**不能**编一条路径出来让用户确认，只能如实说「拿不到待确认的路径」。
 */
export function confirmFromError(detail: ErrorDetail): SaveConfirmInfo | null {
  if (detail.code !== "SAVE_CONFIRM_REQUIRED") {
    return null;
  }
  const raw = (detail.details ?? {}) as Record<string, unknown>;
  const targetPath = typeof raw.target_path === "string" ? raw.target_path : "";
  if (!targetPath) {
    return null;
  }
  const mode = raw.mode === "overwrite" ? "overwrite" : "save_as";
  const backupPath = typeof raw.backup_path === "string" ? raw.backup_path : null;
  const warnings = Array.isArray(raw.warnings)
    ? raw.warnings.filter((item): item is string => typeof item === "string")
    : [];
  const confirmationId = typeof raw.confirmation_id === "string" ? raw.confirmation_id : "";
  return { mode, targetPath, backupPath, warnings, confirmationId };
}

/** 从 prepare 成功的响应里取出同形的确认信息（覆盖路径已带备份）。 */
export function confirmFromPrepared(prepared: {
  mode: string;
  target_path: string;
  backup_path: string | null;
  warnings: string[];
  confirmation_id?: string;
}): SaveConfirmInfo {
  return {
    mode: prepared.mode === "overwrite" ? "overwrite" : "save_as",
    targetPath: prepared.target_path,
    backupPath: prepared.backup_path,
    warnings: [...prepared.warnings],
    confirmationId: prepared.confirmation_id ?? "",
  };
}

// -- 四态展示 ------------------------

export type SaveStepTone = "ok" | "bad" | "unknown" | "skip";

export interface SaveStepRow {
  key: string;
  label: string;
  value: string;
  tone: SaveStepTone;
}

function triState(value: unknown, okText: string, badText: string): { value: string; tone: SaveStepTone } {
  if (value === true) {
    return { value: okText, tone: "ok" };
  }
  if (value === false) {
    return { value: badText, tone: "bad" };
  }
  return { value: "未知", tone: "unknown" };
}

/**
 * 把 `status` 摊成「应用 / 回读 / 备份 / 保存 / 回滚」五行。
 *
 * 五个状态**各自独立**，绝不合并成一句「保存失败」：用户最需要知道的是
 * 「草稿到底写进去没有」「原文件还在不在」。合并之后这两件事就都看不出来了。
 *
 * 应用与回读都分 L0 / 参数面两半，因为它们是两批独立的写入，
 * 一边成功一边失败是真实存在的情形。
 */
export function saveStatusRows(status: CommitStatus | null): SaveStepRow[] {
  if (!status) {
    return [];
  }
  const surfaceTouched =
    status.surface_readback_verified !== undefined ||
    Object.keys(status.applied_surface ?? {}).length > 0;
  const applied: SaveStepRow = { key: "applied", label: "应用（L0）", ...triState(status.applied, "已写入", "未写入") };
  const surfaceApplied: SaveStepRow | null = surfaceTouched
    ? {
        key: "applied-surface",
        label: "应用（Cel / 材质）",
        ...triState(
          Object.keys(status.applied_surface ?? {}).length > 0 || status.surface_readback_verified,
          `${Object.keys(status.applied_surface ?? {}).length} 项已写入`,
          "未写入"
        ),
      }
    : null;
  const readback: SaveStepRow = {
    key: "readback",
    label: "回读（L0）",
    ...triState(status.readback_verified, "一致", "不一致"),
  };
  const surfaceReadback: SaveStepRow | null = surfaceTouched
    ? {
        key: "readback-surface",
        label: "回读（Cel / 材质）",
        ...triState(status.surface_readback_verified, "一致", "不一致"),
      }
    : null;

  // 材质图（PR #7）单独一档：它与 Cel 是两批独立的写入，一边成功一边失败是真实存在的
  // 情形；合并成一行会让「材质图到底写进去没有」看不出来。
  const graphTouched =
    status.material_graph_readback_verified !== undefined ||
    Object.keys(status.applied_material_graph ?? {}).length > 0;
  const graphApplied: SaveStepRow | null = graphTouched
    ? {
        key: "applied-material-graph",
        label: "应用（材质图）",
        ...triState(
          Object.keys(status.applied_material_graph ?? {}).length > 0 ||
            status.material_graph_readback_verified,
          `${Object.keys(status.applied_material_graph ?? {}).length} 项已写入`,
          "未写入"
        ),
      }
    : null;
  const graphReadback: SaveStepRow | null = graphTouched
    ? {
        key: "readback-material-graph",
        label: "回读（材质图）",
        ...triState(status.material_graph_readback_verified, "一致", "不一致"),
      }
    : null;

  const backup = status.backup ?? { required: false, created: false, path: null, path_confirmed: null };
  const backupRow: SaveStepRow = backup.required
    ? {
        key: "backup",
        label: "备份",
        ...(backup.created
          ? { value: `已生成：${backup.path ?? backup.path_confirmed ?? "（路径未知）"}`, tone: "ok" as const }
          : { value: "未生成", tone: "bad" as const }),
      }
    : { key: "backup", label: "备份", value: "本次不需要（目标文件不存在）", tone: "skip" };

  const savedRow: SaveStepRow = {
    key: "saved",
    label: "保存",
    ...triState(status.saved, status.target_path ? `已写入 ${status.target_path}` : "已写入", "未写入"),
  };
  if (status.saved && status.save) {
    const parts: string[] = [];
    if (status.save.path_matches === false) {
      parts.push("回读路径与目标不一致");
    }
    if (status.save.file_exists === false) {
      parts.push("回读时文件不存在");
    }
    if (parts.length > 0) {
      savedRow.value = `已写入 ${status.target_path}（但${parts.join("、")}）`;
      savedRow.tone = "bad";
    }
  }

  const rollback = status.rollback ?? {
    attempted: false,
    verified: false,
    code: null,
    error: null,
    reason: null,
  };
  const rollbackRow: SaveStepRow = rollback.attempted
    ? {
        key: "rollback",
        label: "回滚",
        ...triState(rollback.verified, "已恢复到基线", "回滚未验证"),
      }
    : {
        key: "rollback",
        label: "回滚",
        value: rollback.reason ?? "未触发",
        tone: "skip",
      };

  const rows: SaveStepRow[] = [applied];
  if (surfaceApplied) {
    rows.push(surfaceApplied);
  }
  if (graphApplied) {
    rows.push(graphApplied);
  }
  rows.push(readback);
  if (surfaceReadback) {
    rows.push(surfaceReadback);
  }
  if (graphReadback) {
    rows.push(graphReadback);
  }
  rows.push(backupRow, savedRow, rollbackRow);
  return rows;
}

/**
 * 失败时的一句话说明。
 *
 * **一定要**说清两件事：磁盘上的文件没有被写（或者没写全），以及界面上的草稿还在。
 * 少了后半句，用户会以为白改了。
 */
export function saveFailureNotice(status: CommitStatus | null): string {
  const kept = "草稿与最后一张成功预览都已保留，可以改完再存。";
  if (!status) {
    return `保存未完成。${kept}`;
  }
  if (status.backup?.created) {
    return `保存未完成：原文件已备份到 ${status.backup.path ?? status.backup.path_confirmed ?? "备份路径未知"}，目标文件未被改动。${kept}`;
  }
  if (status.applied && !status.saved) {
    return `保存未完成：草稿已写进 Blender 但没能落盘${status.rollback?.attempted ? "，已尝试回滚到基线" : ""}。${kept}`;
  }
  return `保存未完成，磁盘上的工程文件未被写入。${kept}`;
}

/** 二次确认提示：把两条绝对路径与告警摆出来，让用户看清再点。 */
export function confirmNotice(confirm: SaveConfirmInfo): string {
  const lines = [`即将覆盖已有文件：${confirm.targetPath}`];
  lines.push(
    confirm.backupPath
      ? `覆盖前会先生成同目录备份：${confirm.backupPath}`
      : "服务端未给出备份路径，请确认后再继续。"
  );
  return lines.join("\n");
}
