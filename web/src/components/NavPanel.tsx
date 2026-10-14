import { useMemo, useState } from "preact/hooks";
import { useWorkspace } from "../app/useWorkspace";
import { allIdsOf, writableIdsOf } from "../state/draft";
import { isMaterialGroup, materialInfo, materialParamCounts } from "../schema/material";
import { walk } from "../schema/parse";
import type { SurfaceNode, SurfaceSchema } from "../schema/types";
import type { Workspace } from "../state/workspace";
import { VirtualList } from "./VirtualList";

export type NavFilter = "all" | "material" | "modified" | "warning";

export interface NavPanelProps {
  workspace: Workspace;
}

/**
 * 左侧导航：搜索、分组列表、修改 / 错误数量、L0–L3 徽标。
 *
 * 逐材质分组（`material.<材质名>`）与 Cel 分组**并列**出现在同一个列表里 ——
 * 它们是同一层的东西（都是「一族参数」），分成两个列表只会让人找不到。
 * 材质条目额外展示分类、使用对象与可调项数，并提供「仅材质」过滤。
 *
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
    if (filter === "material" && !entry.material) {
      return false;
    }
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
        entry.matchingParamIds.some((id) => id.toLowerCase().includes(needle)) ||
        // 材质还能按分类键 / 分类标签 / 使用对象搜 —— 这三样都是用户真正会输入的词。
        (entry.material
          ? [entry.material.classification, entry.material.classificationLabel, ...entry.material.users]
              .filter((item): item is string => Boolean(item))
              .some((item) => item.toLowerCase().includes(needle))
          : false)
      );
    }
    return true;
  });

  const materialCount = entries.filter((entry) => entry.material).length;

  return (
    <nav class="nav-panel" data-testid="nav-panel">
      <div class="nav-search">
        <input
          type="search"
          placeholder="搜索参数 / 材质 / 使用对象…"
          value={query}
          aria-label="搜索参数"
          onInput={(event) => setQuery((event.currentTarget as HTMLInputElement).value)}
        />
        <div class="nav-filters">
          {(
            [
              ["all", "全部"],
              ["material", `仅材质${materialCount > 0 ? ` (${materialCount})` : ""}`],
              ["modified", "仅已修改"],
              ["warning", "仅警告"],
            ] as Array<[NavFilter, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              class={filter === value ? "chip chip-on" : "chip"}
              data-testid={`nav-filter-${value}`}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <VirtualList
        items={filtered}
        itemHeight={filter === "material" ? 62 : 44}
        class="nav-list"
        empty={<p class="muted pad">没有匹配的分组。</p>}
        renderItem={(entry) => (
          <button
            type="button"
            class={[
              "nav-item",
              entry.material ? "nav-item-tall" : "",
              state.activeGroupId === entry.id ? "nav-item-on" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            data-testid={`nav-${entry.id}`}
            onClick={() => workspace.actions.setActiveGroup(entry.id)}
          >
            <span class="nav-item-main">
              <span class="nav-item-label">{entry.label}</span>
              {entry.dirty > 0 ? <span class="badge badge-dirty">{entry.dirty}</span> : null}
              {entry.errors > 0 ? <span class="badge badge-error">{entry.errors}</span> : null}
            </span>
            {entry.material ? (
              <span class="nav-item-sub" data-testid={`nav-material-meta-${entry.id}`}>
                {entry.material.classificationLabel ?? "未分类"}
                {" · "}
                {entry.material.users.length > 0 ? entry.material.users.join("、") : "无使用对象"}
                {" · "}
                {entry.material.writableParams}/{entry.material.totalParams} 项可调
              </span>
            ) : null}
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

export interface NavEntryMaterialMeta {
  classification: string | null;
  classificationLabel: string | null;
  users: string[];
  writableParams: number;
  totalParams: number;
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
  /** 仅逐材质分组有；其余分组为 null。 */
  material: NavEntryMaterialMeta | null;
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
        material: isMaterialGroup(group) ? materialEntryMeta(group) : null,
      } satisfies NavEntry;
    });
}

function materialEntryMeta(group: SurfaceNode): NavEntryMaterialMeta {
  // `isMaterialGroup` 已保证这里是材质分组；这里只做一次类型收窄。
  if (!isMaterialGroup(group)) {
    return {
      classification: null,
      classificationLabel: null,
      users: [],
      writableParams: 0,
      totalParams: 0,
    };
  }
  const info = materialInfo(group);
  const counts = materialParamCounts(group);
  return {
    classification: info.classification,
    classificationLabel: info.classificationLabel,
    users: info.users,
    writableParams: counts.writable,
    totalParams: counts.total,
  };
}
