import { useWorkspace } from "../app/useWorkspace";
import type { Workspace } from "../state/workspace";

/**
 * 底部操作栏。
 *
 * 启用的都是**当前真的能工作**的动作；三个尚未接通的（保存预设、应用到工程、正式渲染）
 * 一律 `disabled` 并注明「后续提交开放」—— 按钮可以占位，但**不能发不完整的请求**：
 * 一个会返回 422 的按钮比没有按钮更让人困惑。
 */
export function ActionBar({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const previewDisabled = !state.schema || state.historyBlocked;

  return (
    <footer class="action-bar" data-testid="action-bar">
      <div class="action-group">
        <button
          type="button"
          class="btn"
          data-testid="action-undo"
          disabled={!state.canUndo}
          onClick={() => workspace.actions.undo()}
          title={state.canUndo ? `撤销（栈深 ${state.undoDepth}）` : "没有可撤销的操作"}
        >
          撤销
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-redo"
          disabled={!state.canRedo}
          onClick={() => workspace.actions.redo()}
          title={state.canRedo ? `重做（栈深 ${state.redoDepth}）` : "没有可重做的操作"}
        >
          重做
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-reset-group"
          disabled={state.historyBlocked || !state.activeGroupId}
          onClick={() => {
            if (state.activeGroupId) {
              workspace.actions.resetGroupDraft(state.activeGroupId);
            }
          }}
        >
          复位当前组
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-reset-all"
          disabled={state.historyBlocked || state.dirtyIds.size === 0}
          onClick={() => workspace.actions.resetAllDraft()}
        >
          复位全部 Cel
        </button>
      </div>

      <div class="action-group action-primary">
        <span class="pending-hint" data-testid="pending-hint">
          {pendingHint(state)}
        </span>
        <button
          type="button"
          class="btn btn-primary"
          data-testid="action-apply-preview"
          disabled={previewDisabled || state.loading}
          title={state.historyBlocked ? "结构已变化，请先刷新基线" : "提交完整 Cel 草稿并渲染一张预览"}
          onClick={() => workspace.actions.applyAndPreview()}
        >
          {state.loading ? "处理中…" : "应用并预览"}
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-refresh-baseline"
          disabled={state.loading}
          onClick={() => workspace.actions.refreshBaseline()}
        >
          刷新基线
        </button>
      </div>

      <div class="action-group action-pending">
        <button type="button" class="btn" disabled title="提交 5 开放">
          保存完整预设
        </button>
        <button type="button" class="btn" disabled title="提交 5 开放">
          应用到工程
        </button>
        <button type="button" class="btn" disabled title="后续提交开放">
          正式渲染
        </button>
        <span class="muted">（保存闭环在提交 5 开放，当前按钮不发送请求）</span>
      </div>
    </footer>
  );
}

/**
 * 底部提示要区分三种状态，混成一句就会骗人：
 *
 * | 有画面 | 草稿比画面新 | 提示 |
 * |---|---|---|
 * | 否 | — | N 项已改（未预览） |
 * | 是 | 否 | N 项已改（已预览） |
 * | 是 | 是 | 有待预览的 L1 修改 |
 */
export function pendingHint(state: {
  previewStale: boolean;
  dirtyIds: Set<string>;
  lastSuccessfulPreview: unknown;
}): string {
  const count = state.dirtyIds.size;
  if (count === 0) {
    return "草稿与基线一致";
  }
  if (state.previewStale) {
    return "有待预览的 L1 修改";
  }
  return state.lastSuccessfulPreview ? `${count} 项已改（已预览）` : `${count} 项已改（未预览）`;
}
