import type { ComponentChildren } from "preact";

export interface CollapsibleSectionProps {
  /** 稳定 id（用于记住展开状态）。 */
  id: string;
  title: string;
  /** 这个分区装的是什么、以及需要留意的语义。 */
  hint?: string;
  /** 分区内参数总数（与筛选后的数量分开显示）。 */
  total: number;
  /** 当前筛选后要显示的数量。 */
  shown: number;
  /** 其中已修改的数量。 */
  dirty: number;
  open: boolean;
  onToggle: (open: boolean) => void;
  children: ComponentChildren;
}

/**
 * 可折叠分区。
 *
 * 展开状态由**调用方**持有（按材质分别保存），因此这里是无状态组件：
 * 分区自己记状态的话，切一次材质回来展开结构就丢了。
 *
 * `total` 与 `shown` 分开报：用户开了筛选却看到空分区时，必须能看出
 * 「本来有几项、被筛掉了」—— 只显示 0 会让人以为这个材质就是没有这类参数。
 */
export function CollapsibleSection({
  id,
  title,
  hint,
  total,
  shown,
  dirty,
  open,
  onToggle,
  children,
}: CollapsibleSectionProps) {
  const empty = shown === 0;
  return (
    <section
      class={open ? "graph-section graph-section-open" : "graph-section"}
      data-testid={`graph-section-${id}`}
      data-open={open ? "1" : "0"}
      data-total={String(total)}
      data-shown={String(shown)}
    >
      <h4 class="graph-section-head">
        <button
          type="button"
          class="graph-section-toggle"
          data-testid={`graph-section-toggle-${id}`}
          aria-expanded={open}
          onClick={() => onToggle(!open)}
        >
          <span class="graph-section-caret" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
          <span class="graph-section-title">{title}</span>
        </button>
        <span class="graph-section-counts">
          <span class="muted" data-testid={`graph-section-count-${id}`}>
            {shown === total ? `${total} 项` : `${shown} / ${total} 项`}
          </span>
          {dirty > 0 ? <span class="badge badge-dirty">{dirty}</span> : null}
        </span>
      </h4>
      {open ? (
        <div class="graph-section-body">
          {hint ? <p class="muted graph-section-hint">{hint}</p> : null}
          {empty ? (
            <p class="muted" data-testid={`graph-section-empty-${id}`}>
              {total === 0 ? "该分区没有参数。" : `该分区 ${total} 项全部被当前筛选排除。`}
            </p>
          ) : (
            children
          )}
        </div>
      ) : null}
    </section>
  );
}
