import { useEffect } from "preact/hooks";
import { useWorkspace } from "./useWorkspace";
import { useStore } from "../state/useStore";
import { ActionBar } from "../components/ActionBar";
import { Banner } from "../components/Banner";
import { Inspector } from "../components/Inspector";
import { NavPanel } from "../components/NavPanel";
import { PreviewCanvas } from "../components/PreviewCanvas";
import { StatusBar } from "../components/StatusBar";
import type { Workspace } from "../state/workspace";

/** v4 工作台。三栏：导航 / 固定预览 / 检查器，加顶部状态栏与底部操作栏。 */
export function App({ workspace, autoBootstrap = true }: { workspace: Workspace; autoBootstrap?: boolean }) {
  const state = useWorkspace(workspace);
  const externalChanges = useStore(workspace.store, (snapshot) => snapshot.externalChanges);

  useEffect(() => {
    if (!autoBootstrap) {
      return undefined;
    }
    void workspace.actions.bootstrap();
    // 卸载时停止轮询：在途任务的结果一律丢弃。
    return () => workspace.actions.dispose();
  }, [workspace, autoBootstrap]);

  return (
    <div class="workspace">
      <StatusBar workspace={workspace} />

      <div class="banners">
        {state.historyBlocked ? (
          <Banner tone="error" title="结构已失效" testId="banner-structure">
            工程结构发生变化，草稿与已签发的保存确认令牌均已作废。
            <button
              type="button"
              class="link"
              onClick={() => workspace.actions.refreshBaseline()}
              data-testid="banner-refresh-baseline"
            >
              立即刷新基线
            </button>
          </Banner>
        ) : null}

        {state.error ? (
          <Banner
            tone="error"
            title={`错误：${state.error.code}`}
            testId="banner-error"
            onDismiss={() => workspace.actions.clearError()}
          >
            {state.error.message}
            {state.error.hint ? <div class="muted">{state.error.hint}</div> : null}
            {state.error.code === "FRONTEND_NOT_BUILT" ? (
              <div class="muted">请在 web/ 目录执行 npm ci 与 npm run build。</div>
            ) : null}
          </Banner>
        ) : null}

        {state.notice ? (
          <Banner tone="info" testId="banner-notice" onDismiss={() => workspace.actions.clearNotice()}>
            {state.notice}
          </Banner>
        ) : null}

        {/* O3：工程本来干净、预览后 Blender 显示「有未保存的修改」时，说清楚原因。 */}
        {state.projectNotice ? (
          <Banner
            tone="info"
            title="工程未被保存"
            testId="banner-project-dirty"
            onDismiss={() => workspace.actions.clearProjectNotice()}
          >
            {state.projectNotice}
          </Banner>
        ) : null}

        {externalChanges.length > 0 ? (
          <Banner tone="warn" title="检测到外部改动" testId="banner-external">
            以下取值在 Blender 里被改过（只提示，不作废草稿）：
            <ul class="external-list">
              {externalChanges.slice(0, 8).map((change) => (
                <li key={change.id}>
                  <span class="mono-sm">{change.id}</span>
                </li>
              ))}
            </ul>
            {externalChanges.length > 8 ? (
              <span class="muted">…另有 {externalChanges.length - 8} 项</span>
            ) : null}
          </Banner>
        ) : null}
      </div>

      <main class="workbench">
        <NavPanel workspace={workspace} />
        <PreviewCanvas workspace={workspace} />
        <Inspector workspace={workspace} />
      </main>

      <ActionBar workspace={workspace} />
    </div>
  );
}
