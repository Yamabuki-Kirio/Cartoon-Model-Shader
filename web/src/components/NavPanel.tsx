import { useMemo, useState } from "preact/hooks";
import { useWorkspace } from "../app/useWorkspace";
import { allIdsOf, writableIdsOf } from "../state/draft";
import { walk } from "../schema/parse";
import type { SurfaceNode, SurfaceSchema } from "../schema/types";
import type { Workspace } from "../state/workspace";
import { VirtualList } from "./VirtualList";

export type NavFilter = "all" | "modified" | "warning";

export interface NavPanelProps {
  workspace: Workspace;
}

/**
 * 左侧导航：搜索、分组列表、修改 / 错误数量、L0–L3 徽标。
 *
 * 当前**只启用 Cel 分组**：其余参数族要等提交 4 接入 schema 才会出现，
 * 这里不生成任何占位伪控件 —— 假控件比没有控件更糟。
 */
export function NavPanel({ workspace }: NavPanelProps) {
  const state = useWorkspace(workspace);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<NavFilter>("all");

  const entries = useMemo(
    () => buildEntries(state.schema, state.dirtyIds),
    [state.schema, state.dirtyIds]
  );

  const filtered = entries.filter((entry) => {
    if (filter === "modified" && entry.dirty === 0) {
      return false;
    }
    if (filter === "warning" && !entry.hasWarning) {
      return false;
    }
    if (query.trim()) {
      const needle = query.trim().toLowerCase();
      return (
        entry.label.toLowerCase().includes(needle) ||
        entry.id.toLowerCase().includes(needle) ||
        entry.matchingParamIds.some((id) => id.toLowerCase().includes(needle))
      );
    }
    return true;
  });

  return (
    <nav class="nav-panel" data-testid="nav-panel">
      <div class="nav-search">
        <input
          type="search"
          placeholder="搜索参数…"
          value={query}
          aria-label="搜索参数"
          onInput={(event) => setQuery((event.currentTarget as HTMLInputElement).value)}
        />
        <div class="nav-filters">
          {(
            [
              ["all", "全部"],
              ["modified", "仅已修改"],
              ["warning", "仅警告"],
            ] as Array<[NavFilter, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              class={filter === value ? "chip chip-on" : "chip"}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <VirtualList
        items={filtered}
        itemHeight={44}
        height={560}
        class="nav-list"
        empty={<p class="muted pad">没有匹配的分组。</p>}
        renderItem={(entry) => (
          <button
            type="button"
            class={state.activeGroupId === entry.id ? "nav-item nav-item-on" : "nav-item"}
            data-testid={`nav-${entry.id}`}
            onClick={() => workspace.actions.setActiveGroup(entry.id)}
          >
            <span class="nav-item-main">
              <span class="nav-item-label">{entry.label}</span>
              {entry.dirty > 0 ? <span class="badge badge-dirty">{entry.dirty}</span> : null}
              {entry.errors > 0 ? <span class="badge badge-error">{entry.errors}</span> : null}
            </span>
            <span class="nav-item-meta">
              <span class={`cost cost-${entry.cost.toLowerCase()}`}>{entry.cost}</span>
              {entry.degraded ? <span class="muted">降级只读</span> : null}
            </span>
          </button>
        )}
      />
    </nav>
  );
}

export interface NavEntry {
  id: string;
  label: string;
  cost: string;
  dirty: number;
  errors: number;
  hasWarning: boolean;
  degraded: boolean;
  matchingParamIds: string[];
}

/** 分组级别的统计：修改数量、不可用项数量、是否降级只读。 */
export function buildEntries(schema: SurfaceSchema | null, dirtyIds: Set<string>): NavEntry[] {
  if (!schema) {
    return [];
  }
  return schema.groups
    .filter((node) => node.kind === "group")
    .map((group) => {
      const descendants: SurfaceNode[] = walk([group]);
      const writes = writableIdsOf(group);
      const all = allIdsOf(group);
      const unavailable = descendants.filter(
        (node) => node.kind !== "group" && !node.supported
      ).length;
      const dirty = all.filter((id) => dirtyIds.has(id)).length;
      return {
        id: group.id,
        label: group.label,
        cost: group.cost,
        dirty,
        errors: unavailable,
        hasWarning: unavailable > 0 || !group.editable || dirty > 0,
        degraded: !group.supported || !group.editable,
        matchingParamIds: writes.concat(all),
      } satisfies NavEntry;
    });
}
