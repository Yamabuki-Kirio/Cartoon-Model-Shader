import { useState } from "preact/hooks";
import type { ComponentChildren } from "preact";

export interface VirtualListProps<T> {
  items: T[];
  itemHeight: number;
  height: number;
  renderItem: (item: T, index: number) => ComponentChildren;
  overscan?: number;
  class?: string;
  empty?: ComponentChildren;
}

/**
 * 极小的固定行高虚拟列表。
 *
 * 只解决「不要把几百个控件同时挂进 DOM」这一个问题：参数树会长到上千项，
 * 全量渲染会让第一次交互就明显卡顿。行高固定是刻意的取舍 —— 变高行需要测量，
 * 而测量的成本与收益在当前数据量下不成比例；真需要时再引入专门依赖。
 */
export function VirtualList<T>({
  items,
  itemHeight,
  height,
  renderItem,
  overscan = 4,
  class: className,
  empty,
}: VirtualListProps<T>) {
  const [scrollTop, setScrollTop] = useState(0);

  if (items.length === 0) {
    return <div class={className}>{empty ?? null}</div>;
  }

  const visibleCount = Math.max(1, Math.ceil(height / itemHeight));
  const start = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
  const end = Math.min(items.length, start + visibleCount + overscan * 2);
  const slice = items.slice(start, end);

  return (
    <div
      class={className}
      style={{ height: `${height}px`, overflowY: "auto" }}
      onScroll={(event) => {
        setScrollTop((event.currentTarget as HTMLElement).scrollTop);
      }}
      data-testid="virtual-list"
      data-total={String(items.length)}
      data-rendered={String(slice.length)}
    >
      <div style={{ height: `${items.length * itemHeight}px`, position: "relative" }}>
        <div style={{ transform: `translateY(${start * itemHeight}px)` }}>
          {slice.map((item, offset) => renderItem(item, start + offset))}
        </div>
      </div>
    </div>
  );
}
