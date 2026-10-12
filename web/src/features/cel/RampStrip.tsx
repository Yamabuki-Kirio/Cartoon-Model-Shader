import { useRef, useState } from "preact/hooks";
import {
  clamp01,
  clampPositionForIndex,
  isValidColor,
  neighborBounds,
  rampToCssGradient,
  rgbaToCss,
} from "../../schema/ramp";
import type { RampNode } from "../../schema/types";
import type { Workspace } from "../../state/workspace";

export interface RampStripProps {
  workspace: Workspace;
  groupId: string;
  node: RampNode;
  elements: Array<{ position: number; color: number[] }>;
  interpolation: string;
  editable: boolean;
}

/**
 * 色带条 + 逐色标控件。
 *
 * 拖动语义（任务书要求）：
 * * 拖动**期间**只改本地草稿（`dragElement` 用同一个 `mergeKey`，多次 move 合并成一条命令）；
 * * **松开时** `sealHistory()`，于是下一次拖动是**新的一条**撤销记录；
 * * 位置夹在左右邻居之间（严格递增），首尾色标固定在 0 / 1 —— 当前不提供增删色标，
 *   端点被拖走后用户无法补回。
 *
 * 两个实现要点（都是被真实缺陷逼出来的，别改回去）：
 *
 * 1. **「正在拖动第几号色标」放在 `useRef` 里，判定只读 ref。**
 *    若读 `useState` 的值，`beginDrag` 里 `setDragging(i)` 之后本轮注册的监听器
 *    仍捕获着 `dragging === null`（state 更新要等下一次渲染），move 会被整段挡掉、
 *    up 也认不出拖动 —— 表现为「拖了没反应，历史也不封存」。state 只用于渲染高亮。
 * 2. **监听器挂在轨道的 JSX 上，靠 Pointer Capture 收全事件**，而不是
 *    `window.addEventListener` 把当轮闭包钉死。挂在 JSX 上意味着每次渲染都是最新 props，
 *    `elements` 永不发霉；Pointer Capture 则保证指针拖出轨道后事件仍回到轨道元素。
 */
export function RampStrip({
  workspace,
  groupId,
  node,
  elements,
  interpolation,
  editable,
}: RampStripProps) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragIndexRef = useRef<number | null>(null);
  const [dragging, setDragging] = useState<number | null>(null);

  function positionFromEvent(clientX: number): number | null {
    const track = trackRef.current;
    if (!track) {
      return null;
    }
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) {
      // 布局尚未就绪（或元素不可见）：宁可不动，也不要写入一个凭 0 宽度算出的位置。
      return null;
    }
    const clientXNumber = Number(clientX);
    if (!Number.isFinite(clientXNumber)) {
      return null;
    }
    return clamp01((clientXNumber - rect.left) / rect.width);
  }

  function withPosition(index: number, position: number) {
    return elements.map((element, itemIndex) =>
      itemIndex === index ? { position, color: element.color } : element
    );
  }

  function onTrackPointerMove(event: Event) {
    const index = dragIndexRef.current;
    if (index === null) {
      return;
    }
    const position = positionFromEvent((event as unknown as PointerEvent).clientX);
    if (position === null) {
      return;
    }
    const clamped = clampPositionForIndex(elements, index, position);
    if (Math.abs(clamped - (elements[index]?.position ?? 0)) < 1e-9) {
      return;
    }
    workspace.actions.dragElement(groupId, index, withPosition(index, clamped), interpolation);
  }

  function endDrag(event?: Event) {
    const index = dragIndexRef.current;
    if (index === null) {
      return;
    }
    dragIndexRef.current = null;
    setDragging(null);
    const track = trackRef.current;
    const pointerId = (event as unknown as PointerEvent | undefined)?.pointerId;
    if (track && typeof pointerId === "number" && typeof track.releasePointerCapture === "function") {
      try {
        track.releasePointerCapture(pointerId);
      } catch {
        // 指针已经释放 / 捕获从未建立：无需处理。
      }
    }
    // 封存历史：下一次拖动是一条**新的**撤销记录。
    workspace.actions.sealHistory();
  }

  function beginDrag(index: number, event: Event) {
    if (!editable) {
      return;
    }
    event.preventDefault();
    const track = trackRef.current;
    const pointerId = (event as unknown as PointerEvent).pointerId;
    if (track && typeof pointerId === "number" && typeof track.setPointerCapture === "function") {
      try {
        track.setPointerCapture(pointerId);
      } catch {
        // 不支持捕获的环境（如 jsdom）退化为「只在轨道内拖动」。
      }
    }
    dragIndexRef.current = index;
    setDragging(index);
  }

  return (
    <div class="ramp-strip" data-testid={`ramp-strip-${groupId}`}>
      <div
        class="ramp-track"
        ref={trackRef}
        data-testid={`ramp-track-${groupId}`}
        style={{ background: rampToCssGradient(elements, interpolation) }}
        onPointerMove={onTrackPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {elements.map((element, index) => {
          const bounds = neighborBounds(elements, index);
          const fixed = bounds.min === bounds.max;
          return (
            <button
              type="button"
              key={index}
              class={dragging === index ? "ramp-handle ramp-handle-drag" : "ramp-handle"}
              style={{ left: `${clamp01(element.position) * 100}%` }}
              data-testid={`ramp-handle-${groupId}-${index}`}
              data-position={element.position.toFixed(4)}
              aria-label={`${node.label} 第 ${index + 1} 个色标`}
              disabled={!editable || fixed}
              title={fixed ? "端点色标固定在 0 / 1" : `位置 ${element.position.toFixed(3)}`}
              onPointerDown={(event) => beginDrag(index, event as unknown as Event)}
            />
          );
        })}
      </div>

      <ul class="ramp-elements" data-testid={`ramp-elements-${groupId}`}>
        {elements.map((element, index) => (
          <li key={index} class="ramp-element">
            <span class="ramp-index">#{index + 1}</span>

            <label class="ramp-field">
              位置
              <input
                type="number"
                step="0.001"
                min="0"
                max="1"
                disabled={!editable || neighborBounds(elements, index).min === neighborBounds(elements, index).max}
                value={element.position.toFixed(3)}
                data-testid={`ramp-position-${groupId}-${index}`}
                onInput={(event) => {
                  const raw = Number((event.currentTarget as HTMLInputElement).value);
                  if (!Number.isFinite(raw)) {
                    return;
                  }
                  const clamped = clampPositionForIndex(elements, index, raw);
                  const next = elements.map((item, itemIndex) =>
                    itemIndex === index ? { position: clamped, color: item.color } : item
                  );
                  workspace.actions.dragElement(groupId, index, next, interpolation);
                }}
                onBlur={() => workspace.actions.sealHistory()}
              />
            </label>

            <label class="ramp-field">
              颜色
              <input
                type="color"
                disabled={!editable}
                value={rgbHex(element.color)}
                data-testid={`ramp-color-${groupId}-${index}`}
                onInput={(event) => {
                  const parsed = hexToRgba((event.currentTarget as HTMLInputElement).value, element.color[3] ?? 1);
                  if (!isValidColor(parsed)) {
                    return;
                  }
                  const next = elements.map((item, itemIndex) =>
                    itemIndex === index ? { position: item.position, color: parsed } : item
                  );
                  workspace.actions.setElementColor(groupId, index, next, interpolation);
                }}
              />
            </label>

            <label class="ramp-field">
              Alpha
              <input
                type="number"
                step="0.01"
                min="0"
                max="1"
                disabled={!editable}
                value={(element.color[3] ?? 1).toFixed(2)}
                data-testid={`ramp-alpha-${groupId}-${index}`}
                onInput={(event) => {
                  const alpha = clamp01(Number((event.currentTarget as HTMLInputElement).value));
                  const parsed = [element.color[0] ?? 0, element.color[1] ?? 0, element.color[2] ?? 0, alpha];
                  const next = elements.map((item, itemIndex) =>
                    itemIndex === index ? { position: item.position, color: parsed } : item
                  );
                  workspace.actions.setElementColor(groupId, index, next, interpolation);
                }}
                onBlur={() => workspace.actions.sealHistory()}
              />
            </label>

            <span class="ramp-swatch" style={{ background: rgbaToCss(element.color) }} aria-hidden="true" />
          </li>
        ))}
      </ul>
    </div>
  );
}

export function rgbHex(color: number[]): string {
  const toByte = (value: number) =>
    Math.round(clamp01(value ?? 0) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${toByte(color[0] ?? 0)}${toByte(color[1] ?? 0)}${toByte(color[2] ?? 0)}`;
}

export function hexToRgba(hex: string, alpha: number): number[] {
  const text = hex.replace("#", "");
  if (text.length !== 6) {
    return [0, 0, 0, clamp01(alpha)];
  }
  const r = parseInt(text.slice(0, 2), 16) / 255;
  const g = parseInt(text.slice(2, 4), 16) / 255;
  const b = parseInt(text.slice(4, 6), 16) / 255;
  return [r, g, b, clamp01(alpha)];
}
