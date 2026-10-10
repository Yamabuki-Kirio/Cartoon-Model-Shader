/**
 * 草稿命令栈（撤销 / 重做）。
 *
 * 三条设计取舍：
 *
 * 1. **存完整草稿快照，不存逆操作**。草稿很小（几个色带），快照实现没有「逆操作写错」
 *    这类失败模式；合并规则也变成一句「保留第一个 before、替换 after」。
 * 2. **`mergeKey` 是合并的唯一依据**。同一控件连续输入共用一个 key；一次拖动共用一个
 *    key 并在 `pointerup` 时 `seal()`；**不同色标的 key 不同，因此永不合并**。
 * 3. **与服务端任务历史完全无关**。提交预览不清空撤销栈；只有刷新基线才清空 ——
 *    因为刷新后旧草稿引用的 id 可能已经不存在了。
 */

import type { SurfaceDraft } from "../schema/types";
import { deepCopy } from "./draft";

export type CommandKind =
  | "setValue"
  | "dragElement"
  | "setElementColor"
  | "resetGroup"
  | "resetAll"
  | "copyToGroup";

export interface DraftCommand {
  kind: CommandKind;
  /** 展示用文案（例如「Cel_Skin 第 2 个色标位置」）。 */
  label: string;
  groupId: string;
  /** 受影响的分组 id 集合（复位 / 复制会跨组）。 */
  groupIds: string[];
  before: SurfaceDraft;
  after: SurfaceDraft;
  /** 相同 `mergeKey` 的相邻命令会合并成一条；`null` = 永不合并。 */
  mergeKey: string | null;
  at: number;
}

export interface PushOptions {
  kind: CommandKind;
  label: string;
  groupId: string;
  groupIds?: string[];
  before: SurfaceDraft;
  after: SurfaceDraft;
  mergeKey?: string | null;
  now?: number;
}

/** 只有这几种命令允许按 `mergeKey` 合并。复位 / 复制是原子命令，永不合并。 */
const MERGEABLE: ReadonlySet<CommandKind> = new Set<CommandKind>([
  "setValue",
  "dragElement",
  "setElementColor",
]);

export class DraftHistory {
  private undo: DraftCommand[] = [];
  private redo: DraftCommand[] = [];

  get undoDepth(): number {
    return this.undo.length;
  }

  get redoDepth(): number {
    return this.redo.length;
  }

  clear(): void {
    this.undo = [];
    this.redo = [];
  }

  peekUndo(): DraftCommand | null {
    return this.undo.length > 0 ? this.undo[this.undo.length - 1] : null;
  }

  /**
   * 压入一条命令。
   *
   * 合并条件全部满足才合并：同类命令、`mergeKey` 非空且与栈顶相同、
   * 且**栈顶尚未被 seal**。合并时保留第一个 `before`、用新的 `after` 覆盖 ——
   * 于是「连续拖动 30 次」在撤销时一次退回拖前的状态。
   */
  push(options: PushOptions): DraftCommand {
    const command: DraftCommand = {
      kind: options.kind,
      label: options.label,
      groupId: options.groupId,
      groupIds: options.groupIds ?? [options.groupId],
      before: deepCopy(options.before),
      after: deepCopy(options.after),
      mergeKey: options.mergeKey ?? null,
      at: options.now ?? Date.now(),
    };

    const top = this.peekUndo();
    if (
      top &&
      command.mergeKey !== null &&
      top.mergeKey === command.mergeKey &&
      MERGEABLE.has(top.kind) &&
      MERGEABLE.has(command.kind)
    ) {
      top.after = command.after;
      top.label = command.label;
      top.at = command.at;
      // 合并后重做栈必然失效：历史被改写了。
      this.redo = [];
      return top;
    }

    this.undo.push(command);
    this.redo = [];
    return command;
  }

  /** 结束一次连续交互（拖动松开 / 输入框失焦）：此后不再合并进栈顶。 */
  seal(): void {
    const top = this.peekUndo();
    if (top) {
      top.mergeKey = null;
    }
  }

  /** 撤销：返回应该恢复的草稿；没有可撤销时返回 `null`。 */
  undoStep(): { draft: SurfaceDraft; command: DraftCommand } | null {
    const command = this.undo.pop();
    if (!command) {
      return null;
    }
    this.redo.push(command);
    return { draft: deepCopy(command.before), command };
  }

  /** 重做：返回应该恢复的草稿；没有可重做时返回 `null`。 */
  redoStep(): { draft: SurfaceDraft; command: DraftCommand } | null {
    const command = this.redo.pop();
    if (!command) {
      return null;
    }
    this.undo.push(command);
    return { draft: deepCopy(command.after), command };
  }

  /** 便捷合并键。 */
  static mergeKey(kind: CommandKind, paramId: string, elementIndex?: number): string {
    const suffix = elementIndex === undefined ? "" : `#${elementIndex}`;
    return `${kind}:${paramId}${suffix}`;
  }
}
