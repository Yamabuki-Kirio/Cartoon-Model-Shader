/**
 * 草稿的纯函数操作 + `dirtyIds` 计算。
 *
 * 「草稿」是一个**稀疏**映射：只放用户真正改过的参数 id。完整草稿（基线 + 草稿）
 * 由服务端拼装，前端不重复这件事 —— 前端少算一次就少一次算错的机会。
 */

import type { SurfaceDraft, SurfaceNode, SurfaceSchema } from "../schema/types";
import { flatten, walk } from "../schema/parse";

export type SurfaceValues = Record<string, unknown>;

/**
 * 额外的参数索引（材质图参数面，PR #7）。
 *
 * 草稿里可以同时存在**两族** id：v4 参数面（`cel.*` / `material.*`）与材质图
 * （opaque `mg-<hash>`）。它们共用同一份草稿与同一个撤销栈 —— 用户眼里就是
 * 「我改的东西」，分成两个撤销栈只会让 Ctrl+Z 猜不到会退哪一步。
 *
 * 该索引只用于**查询**，不会被复制：材质图 schema 可能上千项，每次改草稿都
 * 复制一遍是没有理由的开销。两族 id 不可能冲突（`mg-` vs `cel.` / `material.`），
 * 因此「先查 v4、再查额外」的合并语义是安全的。不传时行为与只有 v4 时完全一致。
 */
export type ExtraParamIndex = ReadonlyMap<string, SurfaceNode>;

export function emptyDraft(): SurfaceDraft {
  return {};
}

export function deepCopy<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => deepCopy(item)) as unknown as T;
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = deepCopy(item);
    }
    return out as T;
  }
  return value;
}

/** 结构化比较：数值按容差，其余严格相等。 */
export function valuesEqual(left: unknown, right: unknown, tolerance = 1e-6): boolean {
  if (typeof left === "number" && typeof right === "number") {
    return Math.abs(left - right) <= tolerance;
  }
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => valuesEqual(item, right[index], tolerance));
  }
  if (left !== null && right !== null && typeof left === "object" && typeof right === "object") {
    const a = left as Record<string, unknown>;
    const b = right as Record<string, unknown>;
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const key of keys) {
      if (!valuesEqual(a[key], b[key], tolerance)) {
        return false;
      }
    }
    return true;
  }
  return Object.is(left, right);
}

export function setDraftValue(draft: SurfaceDraft, paramId: string, value: unknown): SurfaceDraft {
  return { ...draft, [paramId]: deepCopy(value) };
}

export function clearDraftValue(draft: SurfaceDraft, paramId: string): SurfaceDraft {
  if (!(paramId in draft)) {
    return draft;
  }
  const next = { ...draft };
  delete next[paramId];
  return next;
}

/** 节点当前应显示的取值：草稿优先，否则回落基线。 */
export function nodeValue(draft: SurfaceDraft, node: SurfaceNode): unknown {
  if (node.id in draft) {
    return draft[node.id];
  }
  if (node.kind === "ramp") {
    return {
      elements: node.elements.map((element) => ({
        position: element.position.value,
        color: [...element.color.value],
      })),
      interpolation: node.interpolation.value,
    };
  }
  if (node.kind === "group" || node.kind === "unknown") {
    return null;
  }
  return (node as { value?: unknown }).value ?? null;
}

/** 节点在**基线**上的取值（右侧检查器的「基线值」列）。 */
export function baselineValue(node: SurfaceNode): unknown {
  if (node.kind === "ramp") {
    return {
      elements: node.elements.map((element) => ({
        position: element.position.baseline ?? element.position.value,
        color: [...(element.color.baseline ?? element.color.value)],
      })),
      interpolation: node.interpolation.baseline ?? node.interpolation.value,
    };
  }
  if (node.kind === "group" || node.kind === "unknown") {
    return null;
  }
  return (node as { baseline?: unknown }).baseline ?? null;
}

/** 节点**实际生效**的取值（Blender 回读）。 */
export function effectiveValue(node: SurfaceNode): unknown {
  if (node.kind === "group" || node.kind === "ramp" || node.kind === "unknown") {
    return null;
  }
  return (node as { effective?: unknown }).effective ?? null;
}

/** 子树内全部可写参数 id。 */
export function writableIdsOf(node: SurfaceNode): string[] {
  const out: string[] = [];
  for (const item of walk([node])) {
    if (item.kind === "group" || item.kind === "unknown") {
      continue;
    }
    if (!item.editable || !item.supported) {
      continue;
    }
    out.push(item.id);
  }
  return out;
}

/** 分组及其子树的全部参数 id（用于「已修改数量」统计）。 */
export function allIdsOf(node: SurfaceNode): string[] {
  return walk([node]).map((item) => item.id);
}

/** 已修改的 id 集合：草稿值与基线值不一致的项。 */
export function computeDirtyIds(
  schema: SurfaceSchema | null,
  draft: SurfaceDraft,
  extra?: ExtraParamIndex
): Set<string> {
  const dirty = new Set<string>();
  const index = flatten(schema?.groups ?? []);
  for (const [paramId, value] of Object.entries(draft)) {
    const node = index.get(paramId) ?? extra?.get(paramId);
    if (!node) {
      // 草稿里出现了当前 schema 没有的 id（例如刷新基线后残留）：算作脏，
      // 但提交前会被过滤掉。这里不静默忽略，否则「已修改数量」会与界面不符。
      dirty.add(paramId);
      continue;
    }
    if (!valuesEqual(value, baselineValue(node))) {
      dirty.add(paramId);
    }
  }
  return dirty;
}

/** 仅保留当前 schema 里可写的 id —— 提交前的最后一道过滤。 */
export function sanitizeDraft(
  schema: SurfaceSchema | null,
  draft: SurfaceDraft,
  extra?: ExtraParamIndex
): SurfaceDraft {
  if (!schema && !extra) {
    return {};
  }
  const index = flatten(schema?.groups ?? []);
  const out: SurfaceDraft = {};
  for (const [paramId, value] of Object.entries(draft)) {
    const node = index.get(paramId) ?? extra?.get(paramId);
    if (!node || node.kind === "group" || node.kind === "unknown") {
      continue;
    }
    if (!node.editable || !node.supported) {
      continue;
    }
    out[paramId] = deepCopy(value);
  }
  return out;
}

/**
 * 只保留 `ids` **之外**的草稿项。
 *
 * 材质图按「参数 / 材质」两种粒度复位都走这里：材质分组的 id 不在任何索引里
 * （参数 id 才是索引键），所以不能靠 `resetGroup` 的按名查找。
 */
export function resetIds(draft: SurfaceDraft, ids: Iterable<string>): SurfaceDraft {
  const keep = ids instanceof Set ? ids : new Set(ids);
  const next: SurfaceDraft = {};
  for (const [paramId, value] of Object.entries(draft)) {
    if (!keep.has(paramId)) {
      next[paramId] = value;
    }
  }
  return next;
}

/** 复位某个分组：删掉子树里所有 id 的草稿项。 */
export function resetGroup(
  draft: SurfaceDraft,
  schema: SurfaceSchema | null,
  groupId: string,
  extra?: ExtraParamIndex
): SurfaceDraft {
  const node = flatten(schema?.groups ?? []).get(groupId) ?? extra?.get(groupId);
  if (!node) {
    return draft;
  }
  return resetIds(draft, allIdsOf(node));
}

/** 复位全部草稿：只清掉当前两族索引覆盖的 id，不碰别的东西。 */
export function resetAll(
  draft: SurfaceDraft,
  schema: SurfaceSchema | null,
  extra?: ExtraParamIndex
): SurfaceDraft {
  if (!schema && !extra) {
    return {};
  }
  const index = flatten(schema?.groups ?? []);
  const next: SurfaceDraft = {};
  for (const [paramId, value] of Object.entries(draft)) {
    if (index.has(paramId) || extra?.has(paramId)) {
      continue;
    }
    next[paramId] = value;
  }
  return next;
}
