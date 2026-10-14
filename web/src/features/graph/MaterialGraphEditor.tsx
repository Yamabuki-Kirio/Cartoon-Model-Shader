import { useEffect, useMemo, useRef } from "preact/hooks";
import { useWorkspace } from "../../app/useWorkspace";
import { CollapsibleSection } from "../../components/CollapsibleSection";
import { Chip, reasonLabel } from "../../components/chips";
import { VirtualList } from "../../components/VirtualList";
import type { GraphConfidence } from "../../api/types";
import {
  CONFIDENCE_LABELS,
  GRAPH_CONFIDENCES,
  GRAPH_SECTIONS,
  defaultGraphView,
  materialGraphIndexFor,
  matchesGraphFilter,
  sharedGroupOf,
  writableGraphParams,
  type GraphParam,
  type GraphSectionId,
  type GraphViewState,
} from "../../schema/graph";
import type { Workspace } from "../../state/workspace";
import { GraphParamRow } from "./GraphParamRow";
import { SharedConfirmPanel, SharedConfirmedList } from "./SharedConfirmPanel";

/** 超过这个数量的分区改用虚拟列表：DOM 里同时挂几百个控件会明显卡。 */
const VIRTUALIZE_THRESHOLD = 24;

/**
 * 材质图（Material Graph）深度编辑界面。
 *
 * 与逐材质适配器（`MaterialEditor`，6 个安全参数）是**两套不同的东西**：
 * 前者是服务端探测出来的完整节点图参数面，后者是固定白名单。它们并列显示，
 * 因为用户既想快速调那 6 项，也需要往下钻。
 *
 * 布局上这一块是「分区 + 参数行」：分区固定七个、顺序固定，每个都可折叠。
 * 展开状态与筛选**按材质分别保存**在 workspace 里，切回来时保持原样；
 * 滚动位置存在 ref 里（瞬时状态，进 store 只会带来无谓的重渲染）。
 */
export function MaterialGraphEditor({
  workspace,
  materialName,
}: {
  workspace: Workspace;
  materialName: string;
}) {
  const state = useWorkspace(workspace);
  const graph = state.graph;
  const material = materialGraphIndexFor(graph.schema, materialName);
  const view: GraphViewState = graph.view[materialName] ?? defaultGraphView();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** 各材质的滚动位置；切换材质时先存旧的、再还新的。 */
  const scrollMemory = useRef<Record<string, number>>({});
  const previousMaterial = useRef<string>(materialName);

  // 切换材质：把当前滚动位置记到**旧**材质名下，再把新材质的位置还原。
  useEffect(() => {
    const container = scrollRef.current;
    if (previousMaterial.current !== materialName) {
      if (container) {
        scrollMemory.current[previousMaterial.current] = container.scrollTop;
        container.scrollTop = scrollMemory.current[materialName] ?? 0;
      }
      previousMaterial.current = materialName;
    }
  }, [materialName]);

  const sections = useMemo(() => {
    if (!material) {
      return [];
    }
    return material.sections.map((section) => {
      const shown = section.params.filter((param) =>
        matchesGraphFilter(param, view, state.dirtyIds)
      );
      return { ...section, shown };
    });
  }, [material, view, state.dirtyIds]);

  if (!graph.loaded && !graph.loading) {
    return (
      <section class="graph-editor" data-testid="graph-editor-unavailable">
        <h3>材质图参数</h3>
        <p class="muted" data-testid="graph-unavailable-reason">
          {graph.error
            ? `材质图 schema 加载失败：${graph.error.message}`
            : "尚未采集材质图：点「刷新基线」建立基线时会同时探测材质节点图。"}
        </p>
      </section>
    );
  }

  if (graph.loading && !material) {
    return (
      <section class="graph-editor" data-testid="graph-editor-loading">
        <h3>材质图参数</h3>
        <p class="muted">正在读取材质图 schema…</p>
      </section>
    );
  }

  if (!material) {
    return (
      <section class="graph-editor" data-testid={`graph-editor-missing-${materialName}`}>
        <h3>材质图参数</h3>
        <p class="muted" data-testid="graph-missing-reason">
          当前材质图 schema 里没有材质「{materialName}」。
          它可能是指向别的材质的共享节点组，或本次探测未覆盖到。
        </p>
      </section>
    );
  }

  const writable = writableGraphParams(material).length;
  const dirtyCount = material.params.filter((param) => state.dirtyIds.has(param.node.id)).length;
  const touchedShared = new Set(
    material.params
      .filter((param) => param.node.id in state.draft && param.meta.shared)
      .map((param) => sharedGroupOf(param, material))
      .filter((item): item is string => Boolean(item))
  );

  function toggleSection(id: GraphSectionId, open: boolean): void {
    const current = graph.view[materialName] ?? defaultGraphView();
    const next = open
      ? Array.from(new Set([...current.openSections, id]))
      : current.openSections.filter((item) => item !== id);
    workspace.actions.setGraphView(materialName, { openSections: next });
  }

  function setView(patch: Partial<GraphViewState>): void {
    workspace.actions.setGraphView(materialName, patch);
  }

  const isolated = state.framingChoice.isolateMaterial === materialName;

  return (
    <section class="graph-editor" data-testid={`graph-editor-${materialName}`}>
      <div class="cel-head">
        <h3>材质图参数</h3>
        <div class="status-chips">
          <Chip label={material.available ? "已探测" : "探测不到"} tone={material.available ? "ok" : "bad"} />
          <Chip label={`${writable} / ${material.params.length} 项可调`} tone={writable > 0 ? "ok" : "warn"} />
          {material.counts.shared > 0 ? (
            <Chip label={`${material.counts.shared} 项共享`} tone="warn" />
          ) : null}
          <Chip label="L1" tone="info" />
        </div>
      </div>

      {material.degradedReason ? (
        <p class="inline-warn" data-testid={`graph-degraded-${materialName}`}>
          整份参数面已降级为只读：{reasonLabel(material.degradedReason)}
          <span class="muted">（{material.degradedReason}）</span>
        </p>
      ) : null}

      <p class="muted" data-testid={`graph-summary-${materialName}`}>
        材质图基线 <span class="mono-sm">{(material.structureHash ?? "—").slice(0, 12)}</span>
        {" · "}追踪 {material.trace?.nodes?.length ?? 0} 个节点
        {material.trace?.max_depth ? ` · 最大深度 ${material.trace.max_depth}` : ""}
        {material.trace?.cycle && material.trace.cycle.length > 0
          ? ` · 检测到环：${material.trace.cycle.join(" → ")}`
          : ""}
      </p>

      <div class="graph-toolbar">
        <input
          type="search"
          class="graph-search"
          placeholder="搜索参数 / 来源节点 / 语义 / 影响对象…"
          aria-label="搜索材质图参数"
          data-testid={`graph-search-${materialName}`}
          value={view.query}
          onInput={(event) => setView({ query: (event.currentTarget as HTMLInputElement).value })}
        />
        <div class="graph-filters">
          <button
            type="button"
            class={view.confidence === null ? "chip chip-on" : "chip"}
            data-testid={`graph-filter-confidence-all-${materialName}`}
            onClick={() => setView({ confidence: null })}
          >
            全部等级
          </button>
          {GRAPH_CONFIDENCES.map((confidence: GraphConfidence) => {
            const count = material.counts.byConfidence[confidence] ?? 0;
            return (
              <button
                key={confidence}
                type="button"
                class={view.confidence === confidence ? "chip chip-on" : "chip"}
                data-testid={`graph-filter-confidence-${confidence}-${materialName}`}
                data-count={String(count)}
                onClick={() =>
                  setView({ confidence: view.confidence === confidence ? null : confidence })
                }
              >
                {CONFIDENCE_LABELS[confidence]}
                {count > 0 ? ` (${count})` : ""}
              </button>
            );
          })}
          <button
            type="button"
            class={view.onlyModified ? "chip chip-on" : "chip"}
            data-testid={`graph-filter-modified-${materialName}`}
            onClick={() => setView({ onlyModified: !view.onlyModified })}
          >
            仅已修改{dirtyCount > 0 ? ` (${dirtyCount})` : ""}
          </button>
          <button
            type="button"
            class={view.onlyShared ? "chip chip-on" : "chip"}
            data-testid={`graph-filter-shared-${materialName}`}
            onClick={() => setView({ onlyShared: !view.onlyShared })}
          >
            仅共享节点组{material.counts.shared > 0 ? ` (${material.counts.shared})` : ""}
          </button>
        </div>
        <div class="graph-toolbar-actions">
          <button
            type="button"
            class="btn btn-sm"
            data-testid={`graph-expand-all-${materialName}`}
            onClick={() => setView({ openSections: GRAPH_SECTIONS.map((item) => item.id) })}
          >
            展开全部
          </button>
          <button
            type="button"
            class="btn btn-sm"
            data-testid={`graph-collapse-all-${materialName}`}
            onClick={() => setView({ openSections: [] })}
          >
            折叠全部
          </button>
        </div>
      </div>

      {graph.pendingShared ? (
        <SharedConfirmPanel workspace={workspace} request={graph.pendingShared} material={material} />
      ) : null}

      {touchedShared.size > 0 ? (
        <SharedConfirmedList workspace={workspace} material={material} />
      ) : null}

      <div class="graph-sections" ref={scrollRef} data-testid={`graph-sections-${materialName}`}>
        {sections.map((section) => {
          const open = view.openSections.includes(section.spec.id);
          const shownDirty = section.shown.filter((param) => state.dirtyIds.has(param.node.id)).length;
          return (
            <CollapsibleSection
              key={section.spec.id}
              id={section.spec.id}
              title={section.spec.label}
              hint={section.spec.hint}
              total={section.params.length}
              shown={section.shown.length}
              dirty={shownDirty}
              open={open}
              onToggle={(next) => toggleSection(section.spec.id, next)}
            >
              {section.shown.length > VIRTUALIZE_THRESHOLD ? (
                <VirtualList
                  items={section.shown}
                  itemHeight={64}
                  testId={`graph-list-${section.spec.id}`}
                  class="graph-param-list virtual"
                  renderItem={(param: GraphParam) => (
                    <div class="graph-param-slot">
                      <GraphParamRow
                        workspace={workspace}
                        param={param}
                        sharedGroup={sharedGroupOf(param, material)}
                      />
                    </div>
                  )}
                />
              ) : (
                <div class="graph-param-list" data-testid={`graph-list-${section.spec.id}`}>
                  {section.shown.map((param) => (
                    <GraphParamRow
                      key={param.node.id}
                      workspace={workspace}
                      param={param}
                      sharedGroup={sharedGroupOf(param, material)}
                    />
                  ))}
                </div>
              )}
            </CollapsibleSection>
          );
        })}
      </div>

      <div class="graph-actions">
        <button
          type="button"
          class={isolated ? "btn btn-primary" : "btn"}
          data-testid={`graph-isolate-${materialName}`}
          disabled={state.historyBlocked}
          title="渲染时临时隐藏不使用该材质的对象（渲染后自动恢复）"
          onClick={() => void workspace.actions.isolateMaterial(isolated ? null : materialName)}
        >
          {isolated ? "取消隔离（显示全部材质）" : "隔离该材质预览"}
        </button>
        <button
          type="button"
          class="btn"
          data-testid={`graph-reset-material-${materialName}`}
          disabled={dirtyCount === 0 || state.historyBlocked}
          title="复位该材质下全部材质图参数（不影响 Cel 与逐材质适配器草稿）"
          onClick={() => workspace.actions.resetGraphMaterial(materialName)}
        >
          复位该材质（材质图）
        </button>
        <button
          type="button"
          class="btn"
          data-testid="graph-reset-all"
          disabled={state.historyBlocked}
          title="复位全部材质图草稿"
          onClick={() => workspace.actions.resetAllGraphDraft()}
        >
          复位全部材质图
        </button>
      </div>
    </section>
  );
}
