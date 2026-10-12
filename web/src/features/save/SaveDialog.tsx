import { useWorkspace } from "../../app/useWorkspace";
import type { Workspace } from "../../state/workspace";
import { saveBusy, saveFailureNotice, saveStatusRows, saveTargetProblem } from "../../state/save";

/**
 * 「应用到工程」面板。
 *
 * 三条不能妥协的规则，全在这一个组件里看得见：
 *
 * 1. **默认另存为**。不主动去动用户已有的 `.blend` —— 想覆盖必须显式切过去。
 * 2. **覆盖前必须把绝对路径与备份路径摊开**。服务端在没收到确认时**不会**签发令牌，
 *    而是回 `SAVE_CONFIRM_REQUIRED`；这里把它渲染成一块确认信息，不是一条红错误 ——
 *    用户还没做错任何事，只是还没看清要写到哪里。
 * 3. **失败时草稿一定还在**。所以失败文案里必须出现「草稿已保留」，
 *    而且**绝不**在这里调用任何清草稿的动作。
 */
export function SaveDialog({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const save = state.save;
  if (!save.open) {
    return null;
  }

  const busy = saveBusy(save);
  const isSaveAs = save.mode === "save_as";
  const targetProblem = isSaveAs ? saveTargetProblem(save.targetPath) : null;
  // 还没开始打字就先不说教：空输入框下方挂一条红字只会让人觉得「我还没做什么就错了」。
  // 按钮照旧是禁用的，把原因放进它的 `title` 里 —— 想知道为什么点不动，悬停就有答案。
  const showTargetProblem = Boolean(targetProblem) && save.targetPath.trim() !== "";
  const rows = saveStatusRows(save.status);
  const confirm = save.confirm;
  const structureBlocked = state.historyBlocked;

  return (
    <section class="save-dialog" data-testid="save-dialog" aria-label="保存到工程">
      <header class="save-head">
        <strong>保存到工程</strong>
        <span class="muted">
          草稿会整份写进 Blender（L0 取基线值 + 参数面草稿），回读校验通过后才写盘。
        </span>
        <button
          type="button"
          class="link"
          data-testid="save-close"
          onClick={() => workspace.actions.closeSaveDialog()}
        >
          收起
        </button>
      </header>

      <div class="save-modes" role="radiogroup" aria-label="保存模式">
        <label class="save-mode">
          <input
            type="radio"
            name="save-mode"
            data-testid="save-mode-save_as"
            checked={isSaveAs}
            disabled={busy}
            onChange={() => workspace.actions.setSaveMode("save_as")}
          />
          另存为（默认）
        </label>
        <label class="save-mode">
          <input
            type="radio"
            name="save-mode"
            data-testid="save-mode-overwrite"
            checked={!isSaveAs}
            disabled={busy}
            onChange={() => workspace.actions.setSaveMode("overwrite")}
          />
          覆盖当前工程
        </label>
      </div>

      {isSaveAs ? (
        <div class="save-field">
          <label for="save-target">目标路径（绝对路径，以 .blend 结尾）</label>
          <input
            id="save-target"
            type="text"
            data-testid="save-target"
            value={save.targetPath}
            disabled={busy}
            spellcheck={false}
            placeholder="D:\项目\角色-调参.blend"
            onInput={(event) =>
              workspace.actions.setSaveTarget((event.currentTarget as HTMLInputElement).value)
            }
          />
          {showTargetProblem ? (
            <div class="bad" data-testid="save-target-error">
              {targetProblem}
            </div>
          ) : null}
        </div>
      ) : (
        <div class="save-field muted">
          覆盖模式的目标由 Blender 当前工程决定，不接受指定；覆盖前一定先生成同目录备份。
        </div>
      )}

      {confirm ? (
        <div class="save-confirm" data-testid="save-confirm-box">
          <strong>这次会覆盖已有文件，请先看清这两条路径</strong>
          <dl class="save-paths">
            <dt>目标文件</dt>
            <dd class="mono-sm" data-testid="save-confirm-target">
              {confirm.targetPath}
            </dd>
            <dt>备份位置</dt>
            <dd class="mono-sm" data-testid="save-confirm-backup">
              {confirm.backupPath ?? "服务端未给出备份路径"}
            </dd>
          </dl>
          {confirm.warnings.length > 0 ? (
            <ul class="save-warnings" data-testid="save-confirm-warnings">
              {confirm.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
          <div class="save-confirm-actions">
            <button
              type="button"
              class="btn btn-primary"
              data-testid="save-confirm"
              disabled={busy}
              onClick={() => void workspace.actions.prepareSave(true)}
            >
              确认并覆盖
            </button>
            <span class="muted">按错也没关系：这一步之前磁盘上什么都没有改。</span>
          </div>
        </div>
      ) : null}

      {save.error ? (
        <div class="bad" data-testid="save-error">
          <strong>{save.error.code}</strong>：{save.error.message}
        </div>
      ) : null}

      {save.steps.length > 0 ? (
        <ol class="save-steps" data-testid="save-steps">
          {save.steps.map((step, index) => (
            <li key={`${index}-${step}`}>{step}</li>
          ))}
        </ol>
      ) : null}

      {rows.length > 0 ? (
        <table class="details-table" data-testid="save-status">
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} data-testid={`save-status-row-${row.key}`}>
                <th scope="row">{row.label}</th>
                <td class={`save-status-${row.tone}`} data-tone={row.tone}>
                  {row.value}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {save.outcome?.saved ? (
        <div class="ok" data-testid="save-ok">
          已保存到 {save.outcome.targetPath}
          {save.outcome.backupPath ? `（原文件备份在 ${save.outcome.backupPath}）` : ""}
          {save.outcome.baselineRefreshed ? "；基线已按新内容刷新。" : "；基线刷新未确认，建议手动刷新一次。"}
        </div>
      ) : null}

      {save.phase === "failed" ? (
        <div class="warn" data-testid="save-failure">
          {saveFailureNotice(save.status)}
        </div>
      ) : null}

      <footer class="save-actions">
        <button
          type="button"
          class="btn btn-primary"
          data-testid="save-prepare"
          disabled={busy || structureBlocked || Boolean(targetProblem) || Boolean(confirm)}
          title={
            structureBlocked
              ? "结构已变化，请先刷新基线"
              : targetProblem
                ? targetProblem
                : confirm
                  ? "请先确认上方的覆盖路径"
                  : "校验基线 / 草稿 / 目标路径，不写盘"
          }
          onClick={() => void workspace.actions.prepareSave(false)}
        >
          {save.phase === "preparing" ? "校验中…" : save.phase === "committing" ? "保存中…" : "准备保存"}
        </button>
        <span class="muted" data-testid="save-hint">
          {structureBlocked
            ? "结构已变化：草稿与已签发的确认令牌都已作废。"
            : confirm
              ? "等待你确认覆盖。"
              : "「准备保存」只做校验，不会写盘。"}
        </span>
      </footer>
    </section>
  );
}
