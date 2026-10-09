import { useWorkspace } from "../app/useWorkspace";
import { baselineValue, nodeValue } from "../state/draft";
import { flatten } from "../schema/parse";
import { rgbaToCss } from "../schema/ramp";
import type { SurfaceNode } from "../schema/types";
import type { Workspace } from "../state/workspace";
import { CelEditor } from "../features/cel/CelEditor";
import { Chip, reasonLabel } from "./chips";

export function Inspector({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const groupId = state.activeGroupId;
  const group = state.schema && groupId ? flatten(state.schema.groups).get(groupId) : undefined;

  return (
    <aside class="inspector" data-testid="inspector">
      <header class="inspector-head">
        <h2>{group?.label ?? "未选择分组"}</h2>
        {group ? <GroupStatus node={group} /> : null}
      </header>

      {!state.schema ? (
        <p class="muted pad">尚未加载 schema。</p>
      ) : !group ? (
        <p class="muted pad">请在左侧选择一个分组。</p>
      ) : group.kind === "group" ? (
        <div class="inspector-body">
          <CelEditor workspace={workspace} groupId={group.id} />
          <NodeDetails workspace={workspace} groupId={group.id} />
        </div>
      ) : null}
    </aside>
  );
}

function GroupStatus({ node }: { node: SurfaceNode }) {
  return (
    <div class="status-chips" data-testid="group-status">
      <Chip label={node.supported ? "已探测" : "探测不到"} tone={node.supported ? "ok" : "bad"} />
      <Chip label={node.editable ? "可编辑" : "只读"} tone={node.editable ? "ok" : "warn"} />
      <Chip label={node.active ? "参与输出" : "不参与输出"} tone={node.active ? "ok" : "warn"} />
      <Chip label={`层级 ${node.cost}`} tone="info" />
      {node.readonly_reason ? (
        <Chip label={reasonLabel(node.readonly_reason)} tone="warn" />
      ) : null}
    </div>
  );
}

/** 基线值 / 草稿值 / 生效值三列 + 状态说明。 */
function NodeDetails({ workspace, groupId }: { workspace: Workspace; groupId: string }) {
  const state = useWorkspace(workspace);
  const group = state.schema ? flatten(state.schema.groups).get(groupId) : undefined;
  if (!group || group.kind !== "group") {
    return null;
  }

  const rows = group.children.filter((child) => child.kind !== "group");

  return (
    <section class="details" data-testid="node-details">
      <h3>参数详情</h3>
      <table class="details-table">
        <thead>
          <tr>
            <th>参数</th>
            <th>基线值</th>
            <th>草稿值</th>
            <th>生效值</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((node) => (
            <tr key={node.id} data-testid={`detail-${node.id}`}>
              <td>
                <span class="mono-sm">{node.id}</span>
                <div class="muted">{node.label}</div>
                <div class="muted">
                  来源：{node.value_source}
                  {node.cost === "L1" ? " · L1：只更新草稿，手动触发预览" : ""}
                </div>
              </td>
              <td>{formatValue(baselineValue(node))}</td>
              <td>{formatValue(nodeValue(state.draft, node))}</td>
              <td>{formatValue(state.effective[node.id] ?? null)}</td>
              <td>
                <Chip label={node.supported ? "supported" : "unsupported"} tone={node.supported ? "ok" : "bad"} />
                <Chip label={node.editable ? "editable" : "readonly"} tone={node.editable ? "ok" : "warn"} />
                {node.readonly_reason ? <div class="muted">{reasonLabel(node.readonly_reason)}</div> : null}
                {node.reason ? <div class="muted">{node.reason}</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p class="muted">
        影响材质数：{impactMaterialCount(group)}
      </p>
    </section>
  );
}

function impactMaterialCount(group: SurfaceNode): string {
  if (group.kind !== "group") {
    return "—";
  }
  const node = group.children.find((child) => child.id.endsWith(".impact.material_count"));
  if (!node) {
    return "—";
  }
  const value = (node as { value?: unknown }).value;
  return value === null || value === undefined ? "—" : String(value);
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length === 4 && value.every((item) => typeof item === "number")) {
      return `RGBA ${value.map((item) => item.toFixed(3)).join(", ")}`;
    }
    return `[${value.map((item) => formatValue(item)).join(", ")}]`;
  }
  if (typeof value === "object") {
    const record = value as { elements?: Array<{ position: number; color: number[] }>; interpolation?: string };
    if (Array.isArray(record.elements)) {
      return `色带 ${record.elements.length} 档 · ${record.interpolation ?? ""} · ` +
        record.elements
          .map((element) => `${element.position.toFixed(2)}${rgbaToCss(element.color)}`)
          .join(" ");
    }
    return JSON.stringify(value);
  }
  return String(value);
}
