import { useWorkspace } from "../../app/useWorkspace";
import { reasonLabel } from "../../components/chips";
import { baselineValue, nodeValue } from "../../state/draft";
import {
  CONFIDENCE_HINTS,
  CONFIDENCE_LABELS,
  affectedScopeLabel,
  socketLabelOf,
  type GraphParam,
} from "../../schema/graph";
import {
  draftValueFromNode,
  elementsFromNode,
  validateDraftValue,
} from "../../schema/ramp";
import type { RampDraftValue, RampNode } from "../../schema/types";
import type { Workspace } from "../../state/workspace";
import { RampStrip } from "../cel/RampStrip";
import { ConfidenceBadge } from "./ConfidenceBadge";
import {
  alphaOf,
  formatGraphValue,
  hexToRgbaColor,
  rgbaToCssColor,
  rgbaToHex,
} from "./format";

export interface GraphParamRowProps {
  workspace: Workspace;
  param: GraphParam;
  /** 该参数所属的共享节点组（服务端用来做确认键的那个字符串）；非共享为 `null`。 */
  sharedGroup: string | null;
}

/**
 * 单个材质图参数。
 *
 * 这一行必须同时回答四个问题，缺一个用户就得去别处找：
 *
 * 1. **这是什么**：参数名、来源节点与插座、语义；
 * 2. **现在是多少**：当前值 / 基线值 / 草稿值 / 生效值**四列并存**
 *    （「配置值 vs 生效值」这类老问题不能再复发）；
 * 3. **能不能信**：识别等级 + 识别依据 + 只读原因，推测项**永远**带着「推测」芯片；
 * 4. **改了会怎样**：影响对象与材质、是否共享节点组、回滚能力。
 *
 * `supported && editable` 才画控件：其余一律只读输出 + 原因 —— 绝不画一个
 * 写下去没反应的输入框。
 */
export function GraphParamRow({ workspace, param, sharedGroup }: GraphParamRowProps) {
  const state = useWorkspace(workspace);
  const node = param.node;
  const meta = param.meta;
  const editable = node.supported && node.editable;
  const dirty = state.dirtyIds.has(node.id);
  const draftValue: unknown = node.id in state.draft ? state.draft[node.id] : undefined;
  const effective: unknown = state.effective[node.id];
  const current = nodeValue({}, node);
  const socket = socketLabelOf(param);
  const impact = meta.affectedMaterials;

  return (
    <div
      class="graph-param"
      data-testid={`graph-param-${node.id}`}
      data-confidence={meta.confidence}
      data-section={meta.section}
      data-shared={meta.shared ? "1" : "0"}
      data-editable={editable ? "1" : "0"}
    >
      <div class="graph-param-head">
        <span class="graph-param-label" data-testid={`graph-param-label-${node.id}`}>
          {node.label}
        </span>
        <ConfidenceBadge
          confidence={meta.confidence}
          testId={`graph-param-confidence-${node.id}`}
        />
        {meta.shared ? (
          <span class="chip chip-warn" data-testid={`graph-param-shared-${node.id}`}>
            共享节点组
          </span>
        ) : null}
        {!editable ? (
          <span class="chip chip-info" data-testid={`graph-param-readonly-${node.id}`}>
            只读
          </span>
        ) : null}
        {dirty ? (
          <span class="badge badge-dirty" data-testid={`graph-param-dirty-${node.id}`}>
            已改
          </span>
        ) : null}
      </div>

      <div class="graph-param-value" data-testid={`graph-param-value-${node.id}`}>
        <ParamControl workspace={workspace} param={param} draftValue={draftValue} />
      </div>

      <dl class="graph-param-values" data-testid={`graph-param-values-${node.id}`}>
        <div>
          <dt>当前值</dt>
          <dd data-testid={`graph-current-${node.id}`}>{formatGraphValue(current)}</dd>
        </div>
        <div>
          <dt>基线值</dt>
          <dd data-testid={`graph-baseline-${node.id}`}>{formatGraphValue(baselineValue(node))}</dd>
        </div>
        <div>
          <dt>草稿值</dt>
          <dd data-testid={`graph-draft-${node.id}`}>
            {draftValue === undefined ? "未改动" : formatGraphValue(draftValue)}
          </dd>
        </div>
        <div>
          <dt>生效值</dt>
          <dd data-testid={`graph-effective-${node.id}`}>
            {effective === undefined ? "未确认" : formatGraphValue(effective)}
          </dd>
        </div>
      </dl>

      <div class="graph-param-scope">
        <div data-testid={`graph-source-${node.id}`}>
          来源节点：{meta.sourceLabel ?? "—"} · 插座：{socket ?? "—"}（字段{" "}
          {meta.bindingField ?? "—"}）
        </div>
        <div data-testid={`graph-impact-${node.id}`}>
          {affectedScopeLabel(param)}：
          {impact.length > 0 ? impact.join("、") : "服务端未给出"}
        </div>
        <div data-testid={`graph-shared-group-${node.id}`}>
          共享节点组：
          {sharedGroup ?? (meta.inGroup ? `${meta.inGroup}（未被多个材质引用）` : "否")}
        </div>
        <div data-testid={`graph-rollback-${node.id}`}>回滚能力：{meta.rollback}</div>
        <div data-testid={`graph-confidence-note-${node.id}`}>
          识别等级：{CONFIDENCE_LABELS[meta.confidence]}
          <span class="muted">
            （{meta.confidence}）· {CONFIDENCE_HINTS[meta.confidence]}
          </span>
        </div>
        {!editable ? (
          <div class="inline-warn" data-testid={`graph-readonly-reason-${node.id}`}>
            只读原因：{reasonLabel(node.readonly_reason)}
            {node.reason ? ` · ${node.reason}` : ""}
            {node.note ? <span class="muted"> · {node.note}</span> : null}
          </div>
        ) : null}
        {meta.frontendDowngrade ? (
          <div class="inline-warn" data-testid={`graph-downgrade-${node.id}`}>
            前端已降级为只读：{reasonLabel(meta.frontendDowngrade)}
          </div>
        ) : null}

        <details class="graph-param-evidence" data-testid={`graph-evidence-${node.id}`}>
          <summary>识别依据与影响路径</summary>
          {meta.evidence.length > 0 ? (
            <ul class="graph-evidence-list">
              {meta.evidence.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : (
            <p class="muted">服务端未给出证据条目。</p>
          )}
          {meta.influencePaths.length > 0 ? (
            <ol class="graph-path-list">
              {meta.influencePaths.map((path, index) => (
                <li key={index}>{path.join(" → ")}</li>
              ))}
            </ol>
          ) : (
            <p class="muted">服务端未给出影响路径。</p>
          )}
          <p class="muted">
            被下游取用的输出插座：
            {meta.usedOutputs.length > 0 ? meta.usedOutputs.join("、") : "—"}
          </p>
        </details>
      </div>

      <div class="graph-param-actions">
        <button
          type="button"
          class="btn btn-sm"
          data-testid={`graph-reset-${node.id}`}
          disabled={!dirty || state.historyBlocked}
          title={dirty ? "只复位这一项，不影响同材质的其它修改" : "该项与基线一致，无需复位"}
          onClick={() => workspace.actions.resetGraphParam(node.id)}
        >
          复位该项
        </button>
      </div>
    </div>
  );
}

/** 按 kind 分派控件；不可写一律只读输出。 */
function ParamControl({
  workspace,
  param,
  draftValue,
}: {
  workspace: Workspace;
  param: GraphParam;
  draftValue: unknown;
}) {
  const node = param.node;
  const editable = node.supported && node.editable;
  const current = nodeValue({}, node);

  if (node.kind === "ramp") {
    return <RampControl workspace={workspace} node={node} draftValue={draftValue} />;
  }

  if (!editable) {
    return (
      <output data-testid={`graph-value-output-${node.id}`}>
        {formatGraphValue(draftValue === undefined ? current : draftValue)}
      </output>
    );
  }

  if (node.kind === "color") {
    const value = draftValue ?? current;
    const alpha = alphaOf(value);
    return (
      <span class="graph-color-inputs">
        <input
          type="color"
          data-testid={`graph-color-${node.id}`}
          value={rgbaToHex(value)}
          onInput={(event) => {
            const hex = (event.currentTarget as HTMLInputElement).value;
            workspace.actions.setValue(node.id, hexToRgbaColor(hex, alpha));
          }}
          onBlur={() => workspace.actions.sealHistory()}
        />
        <label class="graph-alpha">
          Alpha
          <input
            type="number"
            step="0.01"
            min="0"
            max="1"
            data-testid={`graph-alpha-${node.id}`}
            value={alpha.toFixed(2)}
            onInput={(event) => {
              const next = hexToRgbaColor(
                rgbaToHex(value),
                Number((event.currentTarget as HTMLInputElement).value)
              );
              workspace.actions.setValue(node.id, next);
            }}
            onBlur={() => workspace.actions.sealHistory()}
          />
        </label>
        <span class="ramp-swatch" style={{ background: rgbaToCssColor(value) }} aria-hidden="true" />
      </span>
    );
  }

  if (node.kind === "float" || node.kind === "int") {
    const minimum = node.minimum;
    const maximum = node.maximum;
    // 只有服务端**明确给了**一个有效区间时才画滑块。凭 0–1 编一个区间，
    // 会让「Roughness 是 0–1」这种巧合看起来像一条规则，而别的参数上就是误导。
    const sliderOk =
      typeof minimum === "number" &&
      typeof maximum === "number" &&
      Number.isFinite(minimum) &&
      Number.isFinite(maximum) &&
      maximum > minimum;
    const step = node.step ?? (node.kind === "int" ? 1 : 0.01);
    const shown = draftValue ?? current;
    return (
      <span class="graph-number-inputs">
        <input
          type="number"
          step={String(step)}
          min={minimum ?? undefined}
          max={maximum ?? undefined}
          data-testid={`graph-number-${node.id}`}
          value={shown === null || shown === undefined ? "" : String(shown)}
          onInput={(event) => {
            const raw = (event.currentTarget as HTMLInputElement).value;
            if (raw === "") {
              return;
            }
            workspace.actions.setValue(node.id, Number(raw));
          }}
          onBlur={() => workspace.actions.sealHistory()}
        />
        {sliderOk ? (
          <input
            type="range"
            class="graph-slider"
            step={String(step)}
            min={String(minimum)}
            max={String(maximum)}
            data-testid={`graph-slider-${node.id}`}
            value={typeof shown === "number" ? String(shown) : String(minimum)}
            onInput={(event) =>
              workspace.actions.setValue(
                node.id,
                Number((event.currentTarget as HTMLInputElement).value)
              )
            }
            onBlur={() => workspace.actions.sealHistory()}
          />
        ) : (
          <span class="muted graph-no-slider" data-testid={`graph-no-slider-${node.id}`}>
            服务端未给出值域，只提供精确输入
          </span>
        )}
      </span>
    );
  }

  return (
    <output data-testid={`graph-value-output-${node.id}`}>
      {formatGraphValue(draftValue === undefined ? current : draftValue)}
    </output>
  );
}

/** 色带：拖动 / 位置 / 颜色 / Alpha 全部写进**这一个** opaque 参数 id。 */
function RampControl({
  workspace,
  node,
  draftValue,
}: {
  workspace: Workspace;
  node: RampNode;
  draftValue: unknown;
}) {
  const editable = node.supported && node.editable;
  const value =
    (draftValue as RampDraftValue | undefined) ?? draftValueFromNode(node);
  const elements = elementsFromNode(node).map((element, index) => ({
    position: value.elements[index]?.position ?? element.position.value,
    color: value.elements[index]?.color ?? element.color.value,
  }));
  const invalid = validateDraftValue(value);

  return (
    <div class="graph-ramp">
      <RampStrip
        workspace={workspace}
        groupId={node.id}
        node={node}
        elements={elements}
        interpolation={value.interpolation}
        editable={editable}
        bridge={{
          dragElement: (index, next, interpolation) =>
            workspace.actions.dragGraphRamp(node.id, index, next, interpolation),
          setElementColor: (index, next, interpolation) =>
            workspace.actions.setGraphRampColor(node.id, index, next, interpolation),
          sealHistory: () => workspace.actions.sealHistory(),
        }}
      />
      <div class="graph-ramp-interpolation">
        {/* 插值方式**只读展示**：材质图 schema 不下发可选插值清单
            （`_ramp_node` 不填 `interpolation_options`），因此没有可选项可画。
            前端不编一份 Blender 枚举出来 —— 写一个 Blender 不认的字符串，
            失败会发生在写入那一刻，而那时用户已经在别处改了很多东西。 */}
        <span class="muted">
          插值方式：
          <output data-testid={`graph-interpolation-${node.id}`}>{value.interpolation}</output>
          <span class="muted"> （服务端未下发可选清单，只读展示）</span>
        </span>
        <span class="muted" data-testid={`graph-element-count-${node.id}`}>
          色标数量 {node.element_count.value}（结构信息，只读；不提供增删）
        </span>
      </div>
      {invalid ? (
        <p class="inline-error" data-testid={`graph-ramp-invalid-${node.id}`}>
          {invalid}
        </p>
      ) : null}
    </div>
  );
}
