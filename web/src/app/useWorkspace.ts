import { useStore } from "../state/useStore";
import type { WorkspaceState, Workspace } from "../state/workspace";

/** 订阅整个工作台状态。组件数量不大，整份订阅比到处写 selector 更不容易出错。 */
export function useWorkspace(workspace: Workspace): WorkspaceState {
  return useStore(workspace.store, (state) => state);
}
