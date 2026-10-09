import { useWorkspace } from "../app/useWorkspace";
import type { Workspace } from "../state/workspace";

const STATUS_LABELS: Record<string, string> = {
  connected: "已连接",
  disconnected: "未连接",
  timeout: "超时",
  protocol_error: "协议异常",
  blender_error: "Blender 报错",
  unknown: "未知",
  unreachable: "服务不可达",
};

export function StatusBar({ workspace }: { workspace: Workspace }) {
  const state = useWorkspace(workspace);
  const connection = state.connection;

  return (
    <header class="status-bar" data-testid="status-bar">
      <span class="status-item" data-testid="status-blender">
        <em>Blender</em>
        <b class={`dot dot-${connection.status}`} aria-hidden="true" />
        {STATUS_LABELS[connection.status] ?? connection.status}
        {connection.latencyMs !== null ? <span class="muted"> {Math.round(connection.latencyMs)}ms</span> : null}
      </span>
      <span class="status-item">
        <em>工程</em>
        {String(state.framing?.["camera"] ?? "—")}
        {state.framing?.["frame_current"] !== undefined ? (
          <span class="muted"> · 帧 {String(state.framing["frame_current"])}</span>
        ) : null}
      </span>
      <span class="status-item" data-testid="status-baseline">
        <em>基线</em>
        {state.baselineId ?? "未建立"}
      </span>
      <span class="status-item" data-testid="status-structure">
        <em>结构</em>
        {state.historyBlocked ? (
          <b class="bad">已失效</b>
        ) : state.structureHash ? (
          <span class="mono">{state.structureHash.slice(0, 8)}</span>
        ) : (
          "—"
        )}
      </span>
      <span class="status-item" data-testid="status-dirty">
        <em>草稿</em>
        {state.dirtyIds.size} 项已改
        {state.previewStale ? <b class="warn"> · 画面已过期</b> : null}
      </span>
      <span class="status-item" data-testid="status-job" data-active={state.activeJob ? "true" : "false"}>
        <em>任务</em>
        {state.activeJob
          ? jobLabel(state.activeJob.status)
          : state.lastJob
            ? `空闲（上次：${jobLabel(state.lastJob.status)}）`
            : "空闲"}
      </span>
      <span class="status-item" data-testid="status-rollback">
        <em>回滚</em>
        {rollbackLabel(state)}
      </span>
    </header>
  );
}

function jobLabel(status: string): string {
  switch (status) {
    case "queued":
      return "排队中";
    case "running":
      return "渲染中";
    case "done":
      return "已完成";
    case "failed":
      return "失败";
    case "superseded":
      return "已被取代";
    default:
      return status;
  }
}

function rollbackLabel(state: ReturnType<typeof useWorkspace>): string {
  const restore = state.error?.details?.["restore"] as
    | { attempted?: boolean; verified?: boolean; code?: string | null }
    | undefined;
  if (!restore) {
    return state.error ? "未触发" : "正常";
  }
  if (restore.verified) {
    return "已恢复";
  }
  if (restore.attempted) {
    return "未恢复（需人工确认）";
  }
  return "未写入场景";
}
