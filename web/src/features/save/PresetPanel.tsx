import { useState } from "preact/hooks";
import { useWorkspace } from "../../app/useWorkspace";
import type { Workspace } from "../../state/workspace";

/**
 * 本地预设面板。
 *
 * 几个刻意的取舍：
 *
 * * **列表每次都重新拉**。预设目录是用户（或别的工具）能直接动的，
 *   缓存一份过期列表会让「我明明删了它怎么还在」。
 * * **套用是可撤销的一步**。套用走工作台的命令栈，按错一下撤销就回去了。
 * * **对不上的项要报数**。预设里的 id 含工程事实（材质名、Cel 组名），
 *   跨工程套用必然有一部分对不上；静默丢弃会让用户以为生效了、画面却没变。
 * * **面板自己不说「保存成功」**。落盘与否由 `save.outcome` 决定，
 *   预设只是参数集合，跟工程文件无关 —— 这两件事混在一起最误导人。
 */
export function PresetPanel({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const presets = state.presets;
  const [name, setName] = useState("");
  const [overwriteId, setOverwriteId] = useState<string | null>(null);

  if (!presets.open) {
    return null;
  }

  const busy = presets.busy || state.save.phase === "preparing" || state.save.phase === "committing";
  const dirty = state.dirtyIds.size;
  const current = presets.list.find((item) => item.preset_id === overwriteId) ?? null;

  return (
    <section class="preset-panel" data-testid="preset-panel" aria-label="本地预设">
      <header class="save-head">
        <strong>本地预设</strong>
        <span class="muted" data-testid="preset-storage">
          存放位置：{presets.storage || "（未知）"}
        </span>
        <button
          type="button"
          class="link"
          data-testid="preset-refresh"
          disabled={presets.loading}
          onClick={() => void workspace.actions.refreshPresets()}
        >
          {presets.loading ? "读取中…" : "重新读取"}
        </button>
        <button
          type="button"
          class="link"
          data-testid="preset-close"
          onClick={() => workspace.actions.closePresetPanel()}
        >
          收起
        </button>
      </header>

      <div class="preset-save-row">
        <label for="preset-name">预设名称</label>
        <input
          id="preset-name"
          type="text"
          data-testid="preset-name"
          value={name}
          disabled={busy}
          placeholder="例如：柔和肤色 v2"
          onInput={(event) => setName((event.currentTarget as HTMLInputElement).value)}
        />
        <button
          type="button"
          class="btn btn-primary"
          data-testid="preset-save"
          disabled={busy || state.historyBlocked || !name.trim()}
          title={state.historyBlocked ? "结构已变化，请先刷新基线" : "把当前完整草稿存成一份新预设"}
          onClick={() => {
            void workspace.actions.savePreset(name).then((ok) => {
              if (ok) {
                setName("");
              }
            });
          }}
        >
          保存完整预设
        </button>
        <span class="muted" data-testid="preset-draft-hint">
          {dirty === 0 ? "当前草稿与基线一致，存下来会是一份空预设。" : `将保存 ${dirty} 项改动。`}
        </span>
      </div>

      {presets.error ? (
        <div class="bad" data-testid="preset-error">
          <strong>{presets.error.code}</strong>：{presets.error.message}
          <button
            type="button"
            class="link"
            data-testid="preset-error-dismiss"
            onClick={() => workspace.actions.clearPresetError()}
          >
            知道了
          </button>
        </div>
      ) : null}

      {presets.notice ? (
        <div class="ok" data-testid="preset-notice">
          {presets.notice}
          <button
            type="button"
            class="link"
            data-testid="preset-notice-dismiss"
            onClick={() => workspace.actions.clearPresetNotice()}
          >
            知道了
          </button>
        </div>
      ) : null}

      {current ? (
        <div class="save-confirm" data-testid="preset-overwrite-box">
          <strong>覆盖预设「{current.name}」？</strong>
          <span class="muted">
            身份与创建时间保留，只把参数换成当前草稿。想留一份原来的，请改用「另存为新预设」。
          </span>
          <div class="save-confirm-actions">
            <button
              type="button"
              class="btn btn-primary"
              data-testid="preset-overwrite-confirm"
              disabled={busy}
              onClick={() => {
                void workspace.actions.savePreset(current.name, current.preset_id).then(() => {
                  setOverwriteId(null);
                });
              }}
            >
              确认覆盖
            </button>
            <button
              type="button"
              class="btn"
              data-testid="preset-overwrite-cancel"
              onClick={() => setOverwriteId(null)}
            >
              取消
            </button>
          </div>
        </div>
      ) : null}

      {presets.list.length === 0 ? (
        <div class="muted" data-testid="preset-empty">
          {presets.loading ? "正在读取…" : "还没有预设。"}
        </div>
      ) : (
        <ul class="preset-list" data-testid="preset-list">
          {presets.list.map((preset) => (
            <li class="preset-item" data-testid={`preset-item-${preset.preset_id}`} key={preset.preset_id}>
              <div class="preset-item-main">
                <strong>{preset.name}</strong>
                <span class="muted preset-meta" data-testid={`preset-meta-${preset.preset_id}`}>
                  {preset.surface_count > 0
                    ? `完整预设：参数面 ${preset.surface_count} 项`
                    : "L0 预设：不含 Cel / 材质草稿"}
                  {preset.parameter_count > 0 ? ` · L0 ${preset.parameter_count} 项` : ""}
                  {` · ${preset.updated_at}`}
                </span>
              </div>
              <div class="preset-item-actions">
                <button
                  type="button"
                  class="btn"
                  data-testid={`preset-apply-${preset.preset_id}`}
                  disabled={busy || state.historyBlocked}
                  title="套用到当前草稿（可撤销）"
                  onClick={() => void workspace.actions.applyPreset(preset.preset_id)}
                >
                  套用
                </button>
                <button
                  type="button"
                  class="btn"
                  data-testid={`preset-overwrite-${preset.preset_id}`}
                  disabled={busy || state.historyBlocked}
                  onClick={() => setOverwriteId(preset.preset_id)}
                >
                  覆盖
                </button>
                <button
                  type="button"
                  class="btn"
                  data-testid={`preset-delete-${preset.preset_id}`}
                  disabled={busy}
                  onClick={() => void workspace.actions.deletePreset(preset.preset_id)}
                >
                  删除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {presets.skipped.length > 0 ? (
        <div class="warn" data-testid="preset-skipped">
          有 {presets.skipped.length} 个预设文件读不动（不会被静默忽略）：
          <ul>
            {presets.skipped.map((item) => (
              <li key={item.file}>
                <span class="mono-sm">{item.file}</span> —— {item.code}：{item.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
