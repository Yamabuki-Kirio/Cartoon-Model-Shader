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
  const [dragging, setDragging] = useState<number | null>(null);

  function positionFromEvent(clientX: number): number {
    const track = trackRef.current;
    if (!track) {
      return 0;
    }
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) {
      return 0;
    }
    return clamp01((clientX - rect.left) / rect.width);
  }

  function onPointerMove(event: PointerEvent) {
    if (dragging === null) {
      return;
    }
    const desired = positionFromEvent(event.clientX);
    const clamped = clampPositionForIndex(elements, dragging, desired);
    if (Math.abs(clamped - (elements[dragging]?.position ?? 0)) < 1e-9) {
      return;
    }
    const next = elements.map((element, index) =>
      index === dragging ? { position: clamped, color: element.color } : element
    );
    workspace.actions.dragElement(groupId, dragging, next, interpolation);
  }

  function endDrag() {
    if (dragging === null) {
      return;
    }
    setDragging(null);
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", endDrag);
    workspace.actions.sealHistory();
  }

  function beginDrag(index: number, event: PointerEvent) {
    if (!editable) {
      return;
    }
    event.preventDefault();
    setDragging(index);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endDrag, { once: true });
  }

  return (
    <div class="ramp-strip" data-testid={`ramp-strip-${groupId}`}>
      <div
        class="ramp-track"
        ref={trackRef}
        data-testid={`ramp-track-${groupId}`}
        style={{ background: rampToCssGradient(elements, interpolation) }}
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
              onPointerDown={(event) => beginDrag(index, event as unknown as PointerEvent)}
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
