import { useWorkspace } from "../../app/useWorkspace";
import { nodeValue } from "../../state/draft";
import {
  hexToRgba,
  isMaterialGroup,
  materialInfo,
  materialParamCounts,
  materialParams,
  rgbaToHex,
} from "../../schema/material";
import { flatten } from "../../schema/parse";
import type { SurfaceNode } from "../../schema/types";
import type { Workspace } from "../../state/workspace";
import { Chip, reasonLabel } from "../../components/chips";

/**
 * 逐材质编辑器。
 *
 * 三条约束：
 *
 * 1. **L1 语义**：改参数只进草稿，底部「应用并预览」才提交 —— 与 Cel 编辑器一致。
 * 2. **只渲染 schema 确认可写的控件**：`editable === false` 的项显示只读值 + 原因，
 *    绝不画一个写了没反应的输入框。
 * 3. **隔离要重新渲染**：它改的是渲染时的可见性，不是已落盘的那张图，
 *    所以「隔离」按钮会真的提交一次预览，而不是只切一个本地开关。
 *
 * 「选中的材质」与「隔离的材质」刻意分开：选中决定编辑谁，隔离决定画面里留谁。
 * 合成一件事会让「我想边看整体边调某个材质」这种最常用的姿势没法表达。
 */
export function MaterialEditor({ workspace, groupId }: { workspace: Workspace; groupId: string }) {
  const state = useWorkspace(workspace);
  const group = state.schema ? flatten(state.schema.groups).get(groupId) : undefined;

  if (!group || !isMaterialGroup(group)) {
    return (
      <p class="muted pad" data-testid="material-missing">
        该分组不是受管的材质分组。
      </p>
    );
  }

  const info = materialInfo(group);
  const params = materialParams(group);
  const counts = materialParamCounts(group);
  const isolated = state.framingChoice.isolateMaterial === info.name;
  const busy = state.loading || state.historyBlocked;

  return (
    <section class="material-editor" data-testid={`material-editor-${info.name}`}>
      <div class="cel-head">
        <h3>{group.label}</h3>
        <div class="status-chips">
          <Chip label={group.supported ? "已探测" : "探测不到"} tone={group.supported ? "ok" : "bad"} />
          <Chip label={group.editable ? "可编辑" : "只读"} tone={group.editable ? "ok" : "warn"} />
          <Chip label="L1" tone="info" />
        </div>
      </div>

      {!group.editable ? (
        <p class="inline-warn" data-testid="material-readonly-reason">
          只读：{reasonLabel(group.readonly_reason)}
          {group.reason ? ` · ${group.reason}` : ""}
        </p>
      ) : null}

      <dl class="material-scope" data-testid={`material-scope-${info.name}`}>
        <div>
          <dt>分类</dt>
          <dd data-testid={`material-class-${info.name}`}>
            {info.classificationLabel ?? "未分类"}
            {info.classification ? <span class="muted mono-sm"> ({info.classification})</span> : null}
          </dd>
        </div>
        <div>
          <dt>目标 Cel 组</dt>
          <dd data-testid={`material-target-${info.name}`}>{info.targetGroup ?? "—"}</dd>
        </div>
        <div>
          <dt>使用对象</dt>
          <dd data-testid={`material-users-${info.name}`}>
            {info.users.length > 0 ? info.users.join("、") : "—"}
          </dd>
        </div>
        <div>
          <dt>影响范围</dt>
          <dd data-testid={`material-impact-${info.name}`}>
            {info.users.length} 个对象
            {info.nodeCount !== null ? ` · ${info.nodeCount} 个节点` : ""}
            {info.blendMethod ? ` · 混合模式 ${info.blendMethod}` : ""}
          </dd>
        </div>
      </dl>

      <p class="muted" data-testid={`material-count-${info.name}`}>
        安全参数 {counts.writable} / {counts.total} 项可调
        {counts.writable < counts.total ? "（其余项未确认，只读）" : ""}
      </p>

      <div class="material-params">
        {params.map((node) => (
          <MaterialParamRow key={node.id} workspace={workspace} node={node} />
        ))}
      </div>

      <div class="cel-actions">
        <button
          type="button"
          class={isolated ? "btn btn-primary" : "btn"}
          data-testid={`material-isolate-${info.name}`}
          disabled={busy}
          title={
            state.historyBlocked
              ? "结构已变化，请先刷新基线"
              : "渲染时临时隐藏不使用该材质的对象（渲染后自动恢复）"
          }
          onClick={() => void workspace.actions.isolateMaterial(isolated ? null : info.name)}
        >
          {isolated ? "取消隔离（显示全部材质）" : "隔离显示该材质"}
        </button>
        <button
          type="button"
          class="btn"
          data-testid={`material-reset-${info.name}`}
          disabled={state.historyBlocked}
          onClick={() => workspace.actions.resetGroupDraft(groupId)}
        >
          复位该材质
        </button>
      </div>

      {isolated ? (
        <p class="muted" data-testid={`material-isolate-hint-${info.name}`}>
          已请求隔离：画面里只保留使用该材质的对象。隔离只影响渲染时的可见性，
          预览结束会逐个写回原值；若材质已不存在，服务端会中止渲染而不是给一张误导的图。
        </p>
      ) : null}

      <IsolationReport workspace={workspace} />
    </section>
  );
}

/** 隔离结果回执：隐藏了哪些对象、有没有完整写回。 */
function IsolationReport({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const isolation = state.lastSuccessfulPreview?.isolation;
  if (!isolation || !isolation.requested) {
    return null;
  }
  return (
    <div class="material-isolation-report" data-testid="material-isolation-report">
      <div>
        隔离材质：<span class="mono-sm">{isolation.material ?? "—"}</span>
        {isolation.applied ? <Chip label="已生效" tone="ok" /> : <Chip label="未生效" tone="bad" />}
      </div>
      <div class="muted">
        保留对象：{isolation.objects_kept.join("、") || "—"}
      </div>
      <div class="muted">
        临时隐藏：{isolation.objects_hidden.join("、") || "（没有需要隐藏的对象）"}
      </div>
      <div class="muted">
        {isolation.restored_ok === true
          ? "可见性已逐项写回原值。"
          : isolation.restored_ok === false
            ? `可见性未能完整写回：${Object.keys(isolation.mismatches).join("、")}`
            : "可见性是否写回未被确认。"}
      </div>
    </div>
  );
}

/**
 * 单个材质参数。
 *
 * `supported && editable` 才画控件：颜色走取色器 + 原始 RGBA 文本，
 * 标量走数字输入框。其余一律只读输出 + 原因 —— 与 Cel 的 Emission 行同一规矩。
 */
export function MaterialParamRow({
  workspace,
  node,
}: {
  workspace: Workspace;
  node: SurfaceNode;
}) {
  const state = useWorkspace(workspace);
  const editable = node.supported && node.editable;
  // `nodeValue` 已经做了「草稿优先、回落节点自带 value」的两级取值。
  const value = nodeValue(state.draft, node);

  return (
    <div class="material-param" data-testid={`material-param-${node.id}`}>
      <label>
        <span class="material-param-label">{node.label}</span>
        {!editable ? (
          <output data-testid={`material-param-readonly-${node.id}`}>
            {formatParamValue(value)}
          </output>
        ) : node.kind === "color" ? (
          <span class="material-param-inputs">
            <input
              type="color"
              data-testid={`material-param-color-${node.id}`}
              value={rgbaToHex(value)}
              onInput={(event) => {
                const hex = (event.currentTarget as HTMLInputElement).value;
                const alpha = Array.isArray(value) && typeof value[3] === "number" ? value[3] : 1;
                workspace.actions.setValue(node.id, hexToRgba(hex, alpha));
              }}
              onBlur={() => workspace.actions.sealHistory()}
            />
            <span class="mono-sm muted">{formatParamValue(value)}</span>
          </span>
        ) : (
          <input
            type="number"
            data-testid={`material-param-number-${node.id}`}
            step={String((node as { step?: number }).step ?? 0.01)}
            min={(node as { minimum?: number }).minimum ?? undefined}
            max={(node as { maximum?: number }).maximum ?? undefined}
            value={value === null || value === undefined ? "" : String(value)}
            onInput={(event) => {
              const raw = (event.currentTarget as HTMLInputElement).value;
              if (raw === "") {
                return;
              }
              workspace.actions.setValue(node.id, Number(raw));
            }}
            onBlur={() => workspace.actions.sealHistory()}
          />
        )}
      </label>
      <span class="muted">
        {editable
          ? (node.note ?? "已确认，可编辑。")
          : `只读：${reasonLabel(node.readonly_reason)}${node.reason ? ` · ${node.reason}` : ""}`}
      </span>
    </div>
  );
}

function formatParamValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  if (Array.isArray(value)) {
    if (value.length === 4 && value.every((item) => typeof item === "number")) {
      return `RGBA ${value.map((item) => (item as number).toFixed(3)).join(", ")}`;
    }
    return `[${value.map((item) => formatParamValue(item)).join(", ")}]`;
  }
  return String(value);
}
