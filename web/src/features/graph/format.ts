/**
 * 材质图参数的取值格式化。
 *
 * 单独成文件是为了打断组件之间的环：参数行、共享确认面板与整体编辑器都要用，
 * 放在任一组件里都会让它们互相 import。
 *
 * 一条准则：**拿不到就说「未知」，不要编一个像样的默认值**。颜色 / 色带这类
 * 复合值尤其容易「顺手给个全黑」，而全黑是一个**有意义**的取值，编出来会被当真。
 */

import { clamp01 } from "../../schema/ramp";

/** RGBA / 任意数组 → 可读文本。 */
export function formatGraphValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "string") {
    return value || "（空）";
  }
  if (Array.isArray(value)) {
    if (value.length === 4 && value.every((item) => typeof item === "number")) {
      return `RGBA ${value.map((item) => (item as number).toFixed(3)).join(", ")}`;
    }
    return `[${value.map((item) => formatGraphValue(item)).join(", ")}]`;
  }
  if (typeof value === "object") {
    const record = value as {
      elements?: Array<{ position: number; color: number[] }>;
      interpolation?: string;
    };
    if (Array.isArray(record.elements)) {
      const stops = record.elements
        .map((element) => {
          const color = Array.isArray(element.color) ? element.color : [0, 0, 0, 1];
          return `${Number(element.position).toFixed(2)}@${rgbaToHex(color)}`;
        })
        .join(" ");
      return `色带 ${record.elements.length} 档 · ${record.interpolation ?? ""} · ${stops}`;
    }
    return JSON.stringify(value);
  }
  return String(value);
}

/** RGBA（0–1）→ `#rrggbb`（颜色输入只认 hex）。 */
export function rgbaToHex(color: unknown): string {
  const list = Array.isArray(color) ? color : [];
  const channel = (value: unknown) => {
    const number = typeof value === "number" && Number.isFinite(value) ? value : 0;
    return Math.round(clamp01(number) * 255)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${channel(list[0])}${channel(list[1])}${channel(list[2])}`;
}

/** RGBA（0–1）→ CSS 颜色（含 alpha）。 */
export function rgbaToCssColor(color: unknown): string {
  const list = Array.isArray(color) ? color : [];
  const number = (index: number, fallback: number) => {
    const value = list[index];
    return typeof value === "number" && Number.isFinite(value) ? value : fallback;
  };
  const to255 = (value: number) => Math.round(clamp01(value) * 255);
  return `rgba(${to255(number(0, 0))}, ${to255(number(1, 0))}, ${to255(number(2, 0))}, ${clamp01(
    number(3, 1)
  ).toFixed(3)})`;
}

/** `#rrggbb` + alpha → RGBA（alpha 沿用原值，绝不悄悄改成 1）。 */
export function hexToRgbaColor(hex: string, alpha: unknown): number[] {
  const fallbackAlpha =
    typeof alpha === "number" && Number.isFinite(alpha) ? clamp01(alpha) : 1;
  const text = hex.replace("#", "");
  if (text.length !== 6) {
    return [0, 0, 0, fallbackAlpha];
  }
  const parse = (offset: number) => parseInt(text.slice(offset, offset + 2), 16) / 255;
  return [parse(0), parse(2), parse(4), fallbackAlpha];
}

/** 从 RGBA 数组取 alpha（缺省 1）。 */
export function alphaOf(color: unknown): number {
  if (Array.isArray(color) && typeof color[3] === "number" && Number.isFinite(color[3])) {
    return clamp01(color[3]);
  }
  return 1;
}
