import { useWorkspace } from "../../app/useWorkspace";
import type { SharedConfirmRequest } from "../../api/types";
import type { GraphMaterial } from "../../schema/graph";
import { sharedGroupOf } from "../../schema/graph";
import type { Workspace } from "../../state/workspace";

export interface SharedConfirmPanelProps {
  workspace: Workspace;
  request: SharedConfirmRequest;
  /** 当前选中的材质（可能不是请求里提到的那个）。 */
  material: GraphMaterial | null;
}

/**
 * 共享节点组的影响范围确认。
 *
 * 为什么要单独一块面板、而不是一个「确认」按钮：
 *
 * * 修改共享组会**同时改到别的材质**，用户此刻必须能看到「会影响到谁」；
 * * 所以面板列出组名、受影响材质清单、以及草稿里属于该组的全部参数；
 * * 确认按钮的文字把后果写出来，而不是一个中性的「确定」。
 *
 * 这份信息来自服务端（`SHARED_PARAMETER_CONFIRM_REQUIRED` 的 details，或 schema 的
 * `affected_materials`）。前端**不猜**会影响谁 —— 猜错一次就是在替用户做决定。
 */
export function SharedConfirmPanel({ workspace, request, material }: SharedConfirmPanelProps) {
  const state = useWorkspace(workspace);
  const affected = request.affectedMaterials;
  // 草稿里属于这一组的参数：让用户看到「具体改了哪几项」。
  const touched =
    material === null
      ? []
      : material.params.filter(
          (param) =>
            param.node.id in state.draft && sharedGroupOf(param, material) === request.sharedGroup
        );

  return (
    <div class="shared-confirm" data-testid="shared-confirm-panel">
      <h4 class="shared-confirm-title">共享节点组影响范围确认</h4>
      <p data-testid="shared-confirm-group">
        这一组是：<span class="mono-sm">{request.sharedGroup}</span>
      </p>
      <p data-testid="shared-confirm-affected">
        改动会同时影响 {affected.length} 个材质：
        <strong>{affected.length > 0 ? affected.join("、") : "服务端未列出受影响材质"}</strong>
      </p>
      <div data-testid="shared-confirm-params">
        本次要写进去的参数：
        {touched.length > 0 ? (
          <ul class="shared-confirm-list">
            {touched.map((param) => (
              <li key={param.node.id}>
                <span class="mono-sm">{param.node.label}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span class="muted">（当前材质下没有属于该组的待写入参数）</span>
        )}
      </div>
      <p class="muted" data-testid="shared-confirm-hint">
        确认后这一组参数才会写进 Blender；写入失败会整体回滚并回读确认。
        刷新基线后需要重新确认（影响范围可能已经变了）。
      </p>
      <div class="shared-confirm-actions">
        <button
          type="button"
          class="btn btn-primary"
          data-testid="shared-confirm-accept"
          disabled={state.historyBlocked}
          onClick={() => workspace.actions.confirmSharedGroup(request.sharedGroup)}
        >
          我已了解影响范围，确认修改「{request.sharedGroup}」
        </button>
        <button
          type="button"
          class="btn"
          data-testid="shared-confirm-cancel"
          onClick={() => workspace.actions.cancelSharedConfirm()}
        >
          取消并撤销该组改动
        </button>
      </div>
    </div>
  );
}

/**
 * 已确认的共享组回执。
 *
 * 确认过什么、会影响谁 —— 这些必须留在界面上。只给一个「已确认」的勾，
 * 用户过一会儿就说不清自己批准了什么。
 */
export function SharedConfirmedList({
  workspace,
  material,
}: {
  workspace: Workspace;
  material: GraphMaterial;
}) {
  const state = useWorkspace(workspace);
  if (state.graph.confirmedSharedGroups.length === 0) {
    return null;
  }
  return (
    <div class="shared-confirmed" data-testid="shared-confirmed-list">
      <span class="muted">已确认影响范围的共享节点组：</span>
      <ul class="shared-confirm-list">
        {state.graph.confirmedSharedGroups.map((group) => {
          const users = material.sharedGroups[group] ?? [];
          return (
            <li key={group} data-testid={`shared-confirmed-${group}`}>
              <span class="mono-sm">{group}</span>
              <span class="muted">
                {users.length > 0 ? ` → ${users.join("、")}` : " → 影响材质未知"}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
