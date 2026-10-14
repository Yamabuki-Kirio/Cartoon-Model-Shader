import { useState } from "preact/hooks";
import type { ComponentChildren } from "preact";

export interface VirtualListProps<T> {
  items: T[];
  itemHeight: number;
  /**
   * 容器高度（像素）。
   *
   * **省略时进入「开窗模式」**：容器高度交给 CSS 决定（工作台是视口内固定布局，
   * 参数区要跟着视口伸缩），渲染窗口按滚动位置 + {@link VirtualListProps.windowSize}
   * 计算。两种模式都只渲染一小段，区别只是前者知道确切的可视行数。
   */
  height?: number;
  renderItem: (item: T, index: number) => ComponentChildren;
  overscan?: number;
  /** 开窗模式下一次渲染的行数（省略时按视口高度估算）。 */
  windowSize?: number;
  class?: string;
  empty?: ComponentChildren;
  /** 覆盖默认的 `virtual-list`。同一页里出现多个列表时用它区分。 */
  testId?: string;
}

/**
 * 极小的固定行高虚拟列表。
 *
 * 只解决「不要把几百个控件同时挂进 DOM」这一个问题：参数树会长到上千项，
 * 全量渲染会让第一次交互就明显卡顿。行高固定是刻意的取舍 —— 变高行需要测量，
 * 而测量的成本与收益在当前数据量下不成比例；真需要时再引入专门依赖。
 *
 * 开窗模式（不给 `height`）刻意**不测量容器**：`ResizeObserver` 在测试环境里
 * 不存在，而为了拿一个高度去引入 polyfill，代价比收益大。改为按固定窗口渲染：
 * 窗口足够覆盖常见视口，滚动条长度因为外层占位高度是精确值而依然正确。
 */
export function VirtualList<T>({
  items,
  itemHeight,
  height,
  renderItem,
  overscan = 4,
  windowSize,
  class: className,
  empty,
  testId = "virtual-list",
}: VirtualListProps<T>) {
  const [scrollTop, setScrollTop] = useState(0);

  if (items.length === 0) {
    return <div class={className}>{empty ?? null}</div>;
  }

  const fixed = typeof height === "number" && height > 0;
  const total = items.length * itemHeight;
  let start: number;
  let end: number;
  if (fixed) {
    const visibleCount = Math.max(1, Math.ceil((height as number) / itemHeight));
    start = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
    end = Math.min(items.length, start + visibleCount + overscan * 2);
  } else {
    const perWindow = windowSize ?? estimatedWindow(itemHeight, overscan);
    start = Math.max(0, Math.floor(scrollTop / itemHeight) - overscan);
    end = Math.min(items.length, start + perWindow);
  }
  const slice = items.slice(start, end);

  return (
    <div
      class={className}
      style={fixed ? { height: `${height}px`, overflowY: "auto" } : undefined}
      onScroll={(event) => {
        setScrollTop((event.currentTarget as HTMLElement).scrollTop);
      }}
      data-testid={testId}
      data-total={String(items.length)}
      data-rendered={String(slice.length)}
      data-mode={fixed ? "fixed" : "window"}
    >
      <div style={{ height: `${total}px`, position: "relative" }}>
        <div style={{ transform: `translateY(${start * itemHeight}px)` }}>
          {slice.map((item, offset) => renderItem(item, start + offset))}
        </div>
      </div>
    </div>
  );
}

/**
 * 开窗模式的默认窗口大小。
 *
 * 取「视口高度 / 行高」再留余量：jsdom 里 `innerHeight` 是 768，浏览器里是真实值，
 * 两种情况都能算出足够覆盖可视区的窗口。上限 400 行是为了不让「视口异常大」
 * 变成事实上的全量渲染。
 */
function estimatedWindow(itemHeight: number, overscan: number): number {
  const viewport = typeof window === "undefined" ? 900 : window.innerHeight || 900;
  const rows = Math.ceil(viewport / Math.max(1, itemHeight));
  return Math.min(400, Math.max(24, rows + overscan * 4));
}
