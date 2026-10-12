import { useWorkspace } from "../app/useWorkspace";
import type { Workspace } from "../state/workspace";
import { saveBusy } from "../state/save";

/**
 * 底部操作栏。
 *
 * 启用的都是**当前真的能工作**的动作。保存闭环（保存完整预设 / 应用到工程）
 * 已经接通；「正式渲染」仍未开放，因此保持 `disabled` 并注明原因 ——
 * 按钮可以占位，但**不能发不完整的请求**：一个会返回 422 的按钮比没有按钮更让人困惑。
 *
 * 保存与预览共用同一台 Blender，服务端也是串行的（同一把锁）。因此两边互相禁用：
 * 保存进行中不提交预览，预览进行中也不签发保存令牌。
 */
export function ActionBar({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const saving = saveBusy(state.save);
  const previewDisabled = !state.schema || state.historyBlocked;
  const saveDisabled = !state.schema || state.historyBlocked || saving || state.loading;

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
        <label class="auto-preview" title="连续输入停手约 300ms、或一次操作结束（失焦 / 松开拖动 / 颜色确认）后自动渲染">
          <input
            type="checkbox"
            data-testid="action-auto-preview"
            checked={state.autoPreview}
            disabled={state.historyBlocked}
            onChange={(event) =>
              workspace.actions.setAutoPreview((event.currentTarget as HTMLInputElement).checked)
            }
          />
          自动预览
        </label>
        <span class="pending-hint" data-testid="pending-hint">
          {pendingHint(state)}
        </span>
        <button
          type="button"
          class="btn btn-primary"
          data-testid="action-apply-preview"
          disabled={previewDisabled || state.loading || saving}
          title={
            state.historyBlocked
              ? "结构已变化，请先刷新基线"
              : saving
                ? "保存进行中，等它结束再预览"
                : "提交完整 Cel 草稿并渲染一张预览"
          }
          onClick={() => workspace.actions.applyAndPreview()}
        >
          {state.loading ? "处理中…" : "应用并预览"}
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-refresh-baseline"
          disabled={state.loading || saving}
          onClick={() => workspace.actions.refreshBaseline()}
        >
          刷新基线
        </button>
      </div>

      <div class="action-group action-pending">
        <button
          type="button"
          class="btn"
          data-testid="action-open-presets"
          disabled={state.historyBlocked}
          title={state.historyBlocked ? "结构已变化，请先刷新基线" : "本地预设：保存 / 套用 / 覆盖 / 删除"}
          onClick={() => workspace.actions.openPresetPanel()}
        >
          保存完整预设
        </button>
        <button
          type="button"
          class="btn"
          data-testid="action-open-save"
          disabled={saveDisabled}
          title={
            state.historyBlocked
              ? "结构已变化，请先刷新基线"
              : saving
                ? "保存进行中"
                : state.loading
                  ? "渲染进行中，等它结束再保存"
                  : "保存到工程：默认另存为，覆盖前必先生成备份"
          }
          onClick={() => workspace.actions.openSaveDialog("save_as")}
        >
          应用到工程
        </button>
        <button type="button" class="btn" data-testid="action-final-render" disabled title="后续提交开放">
          正式渲染
        </button>
        <span class="muted">（正式渲染仍未开放；预设与保存已可用）</span>
      </div>
    </footer>
  );
}

/**
 * 底部提示要区分四种状态，混成一句就会骗人：
 *
 * | 有画面 | 草稿比画面新 | 自动预览 | 提示 |
 * |---|---|---|---|
 * | 否 | — | — | N 项已改（未预览） |
 * | 是 | 否 | — | N 项已改（已预览） |
 * | 是 | 是 | 已排期 | 待预览：N 项已改（自动预览已排期） |
 * | 是 | 是 | 关 / 渲染中 | 有待预览的 L1 修改 |
 */
export function pendingHint(state: {
  previewStale: boolean;
  dirtyIds: Set<string>;
  lastSuccessfulPreview: unknown;
  autoPreview: boolean;
  autoPreviewPending: boolean;
}): string {
  const count = state.dirtyIds.size;
  if (count === 0) {
    return "草稿与基线一致";
  }
  if (state.previewStale) {
    if (!state.autoPreview) {
      return "有待预览的 L1 修改（自动预览已关闭）";
    }
    // 排期还在 = 手刚停下，马上会渲染；排期没了但画面仍旧 = 正在渲染（或刚被取代）。
    return state.autoPreviewPending
      ? `待预览：${count} 项已改（自动预览已排期）`
      : "有待预览的 L1 修改";
  }
  return state.lastSuccessfulPreview ? `${count} 项已改（已预览）` : `${count} 项已改（未预览）`;
}
