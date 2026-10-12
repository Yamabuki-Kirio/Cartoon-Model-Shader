import type { ReadonlyReason } from "../schema/types";

/**
 * 状态芯片与只读原因的文案。
 *
 * 单独成文件是为了**打断循环依赖**：检查器（Inspector）与 Cel 编辑器互相需要这两种展示，
 * 放在任一方的文件里都会形成 `Inspector ⇄ CelEditor` 的环。
 */

export const READONLY_REASON_LABELS: Record<string, string> = {
  not_found: "当前工程中探测不到",
  rollback_unavailable: "失败后无法可靠恢复，暂不可编辑",
  keyframed_or_constrained: "带动画 / 约束，只读",
  reference_only: "参考对象，只读",
  structural: "结构性信息，只读",
  unconfirmed_capability: "真实 Blender 拓扑未经确认，暂只读",
  unsupported_kind: "未知参数类型（前端较旧），只读",
};

export function reasonLabel(reason: ReadonlyReason | null): string {
  if (!reason) {
    return "只读";
  }
  return READONLY_REASON_LABELS[reason] ?? reason;
}

export function Chip({ label, tone }: { label: string; tone: "ok" | "warn" | "bad" | "info" }) {
  return <span class={`chip chip-${tone}`}>{label}</span>;
}
