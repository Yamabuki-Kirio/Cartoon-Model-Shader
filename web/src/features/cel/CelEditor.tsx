import { useMemo, useState } from "preact/hooks";
import { useWorkspace } from "../../app/useWorkspace";
import {
  canCopyInto,
  draftValueFromNode,
  elementsFromNode,
  validateDraftValue,
} from "../../schema/ramp";
import { flatten } from "../../schema/parse";
import type { RampDraftValue, SurfaceNode } from "../../schema/types";
import type { Workspace } from "../../state/workspace";
import { RampStrip } from "./RampStrip";
import { Chip, reasonLabel } from "../../components/chips";

/**
 * Cel 色阶编辑器。
 *
 * L1 语义：**编辑不触发渲染**。所有改动只进草稿，底部「应用并预览」才提交。
 * 结构失效（`historyBlocked`）时提交被禁用 —— 撤回到一份已作废的草稿只会更糟。
 *
 * `Sakura_Hair_Reference` 之类参考组在 schema 里就是 `editable: false`，
 * 这里**不提供**任何「强制编辑」或「回退写操作」的入口（本提交不开放自动重连）。
 */
export function CelEditor({ workspace, groupId }: { workspace: Workspace; groupId: string }) {
  const state = useWorkspace(workspace);
  const [copyError, setCopyError] = useState<string | null>(null);

  const index = state.schema ? flatten(state.schema.groups) : null;
  const node = index?.get(`${groupId}.ramp`);
  const emission = index?.get(`${groupId}.emission_strength`) ?? null;

  const copyTargets = useMemo(() => {
    if (!state.schema || !index) {
      return [];
    }
    const source = node && node.kind === "ramp" ? node : null;
    return state.schema.groups
      .filter((group) => group.id !== groupId && group.kind === "group")
      .map((group) => {
        const target = index.get(`${group.id}.ramp`);
        const reason =
          target && target.kind === "ramp" ? canCopyInto(source, target) : "目标组没有可编辑的色带。";
        return { id: group.id, label: group.label, reason };
      });
  }, [state.schema, index, node, groupId]);

  if (!state.schema) {
    return <p class="muted pad">尚未加载 schema。</p>;
  }
  if (!node || node.kind !== "ramp") {
    return (
      <p class="muted pad" data-testid="cel-no-ramp">
        该分组没有色带参数。
      </p>
    );
  }

  const draftValue = (state.draft[node.id] as RampDraftValue | undefined) ?? draftValueFromNode(node);
  const invalid = validateDraftValue(draftValue);
  const elements = elementsFromNode(node).map((element, elementIndex) => ({
    position: draftValue.elements[elementIndex]?.position ?? element.position.value,
    color: draftValue.elements[elementIndex]?.color ?? element.color.value,
  }));

  return (
    <section class="cel-editor" data-testid={`cel-editor-${groupId}`}>
      <div class="cel-head">
        <h3>{node.label}</h3>
        <div class="status-chips">
          <Chip label={node.supported ? "已探测" : "探测不到"} tone={node.supported ? "ok" : "bad"} />
          <Chip label={node.editable ? "可编辑" : "只读"} tone={node.editable ? "ok" : "warn"} />
          <Chip label="L1" tone="info" />
        </div>
      </div>

      {!node.editable ? (
        <p class="inline-warn" data-testid="cel-readonly-reason">
          只读：{reasonLabel(node.readonly_reason)}
          {node.reason ? ` · ${node.reason}` : ""}
        </p>
      ) : null}

      <RampStrip
        workspace={workspace}
        groupId={groupId}
        node={node}
        elements={elements}
        interpolation={draftValue.interpolation}
        editable={node.editable && node.supported}
      />

      <div class="cel-interpolation">
        <label>
          插值方式
          <select
            data-testid={`cel-interpolation-${groupId}`}
            disabled={!node.editable || !node.supported || node.interpolation.options.length === 0}
            value={draftValue.interpolation}
            onChange={(event) =>
              workspace.actions.setInterpolation(
                groupId,
                (event.currentTarget as HTMLSelectElement).value,
                elements
              )
            }
          >
            {(node.interpolation.options.length > 0
              ? node.interpolation.options
              : [{ value: draftValue.interpolation, label: draftValue.interpolation }]
            ).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <span class="muted" data-testid={`cel-count-${groupId}`}>
          色标数量 {node.element_count.value}（结构信息，只读；不提供增删）
        </span>
      </div>

      {invalid ? (
        <p class="inline-error" data-testid={`cel-invalid-${groupId}`}>
          {invalid}
        </p>
      ) : null}

      <EmissionRow workspace={workspace} node={emission} />

      <div class="cel-actions">
        <button
          type="button"
          class="btn"
          data-testid={`cel-reset-${groupId}`}
          disabled={state.historyBlocked}
          onClick={() => workspace.actions.resetGroupDraft(groupId)}
        >
          组内复位
        </button>
        <label class="copy-row">
          复制到
          <select
            data-testid={`cel-copy-target-${groupId}`}
            value=""
            disabled={state.historyBlocked}
            onChange={(event) => {
              const select = event.currentTarget as HTMLSelectElement;
              const targetId = select.value;
              if (!targetId) {
                return;
              }
              const reason = workspace.actions.copyRampToGroup(groupId, targetId);
              setCopyError(reason);
            }}
          >
            <option value="">选择目标组…</option>
            {copyTargets.map((target) => (
              <option key={target.id} value={target.id} disabled={Boolean(target.reason)}>
                {target.label}
                {target.reason ? `（不兼容：${target.reason}）` : ""}
              </option>
            ))}
          </select>
        </label>
      </div>

      {copyError ? (
        <p class="inline-error" data-testid={`cel-copy-error-${groupId}`}>
          无法复制：{copyError}
        </p>
      ) : null}

      <ul class="copy-notes" data-testid={`cel-copy-notes-${groupId}`}>
        {copyTargets.map((target) => (
          <li key={target.id}>
            <span class="mono-sm">{target.label}</span>
            {target.reason ? (
              <span class="muted"> 不兼容：{target.reason}</span>
            ) : (
              <span class="ok"> 可复制</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * Emission 强度：**只有 schema 确认 `editable: true` 时才渲染控件**。
 *
 * 真机拓扑确认后（2026-10-11，受管 Cel 组全部命中「``EMISSION`` 节点 + ``Strength``
 * 插座且未连线」），后端给的是 `NODE_SOCKET.default_value`（object_id =
 * 「组名/节点名/插座名」，三段都由探测结果拼出），因此这里渲染数字输入框。
 *
 * 仍会拿到 `editable: false` 的两种情形：插座已被上游连线（写 default_value
 * 不生效）、或该组是参考组（回退策略）—— 那时只显示只读值。
 */
export function EmissionRow({ workspace, node }: { workspace: Workspace; node: SurfaceNode | null }) {
  const state = useWorkspace(workspace);
  if (!node) {
    return (
      <p class="muted" data-testid="cel-emission-absent">
        Emission 强度：本期未探测到（只读降级）。
      </p>
    );
  }
  const value = state.draft[node.id] ?? (node as { value?: unknown }).value ?? null;
  const editable = node.editable && node.supported;

  return (
    <div class="cel-emission" data-testid={`cel-emission-${node.id}`}>
      <label>
        Emission 强度
        {editable ? (
          <input
            type="number"
            step={String((node as { step?: number }).step ?? 0.05)}
            min={String((node as { minimum?: number }).minimum ?? 0)}
            max={String((node as { maximum?: number }).maximum ?? 100)}
            value={value === null || value === undefined ? "" : String(value)}
            onInput={(event) =>
              workspace.actions.setValue(
                node.id,
                Number((event.currentTarget as HTMLInputElement).value)
              )
            }
            onBlur={() => workspace.actions.sealHistory()}
          />
        ) : (
          <output data-testid={`cel-emission-readonly-${node.id}`}>
            {value === null || value === undefined ? "—" : String(value)}
          </output>
        )}
      </label>
      <span class="muted">
        {editable
          ? "探到且已确认，可编辑。"
          : `只读：${reasonLabel(node.readonly_reason)}`}
      </span>
    </div>
  );
}
