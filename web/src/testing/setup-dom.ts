/**
 * 测试环境保真度：补上 jsdom 缺失的 `onpointer*` IDL 属性。
 *
 * 为什么必须补：Preact 按 `"onpointerdown" in element` 决定注册的事件名 ——
 * 为真时用小写 `pointerdown`，为假时保留驼峰 `PointerDown`。jsdom 没有这些
 * IDL 属性，于是 Preact 注册的是 `PointerDown`，而浏览器/测试派发的
 * `pointerdown` **永远打不中**。表现是「拖动在测试里毫无反应」，
 * 看起来像组件坏了，实际是环境与浏览器不一致。
 *
 * 真实浏览器里 `in` 为真，注册的就是小写名 —— 所以这里补上属性，
 * 让测试环境与浏览器行为一致，而不是把组件改写成迁就 jsdom 的样子。
 *
 * 只补「存在性」，不伪造行为：`setPointerCapture` 等仍缺失，组件已按
 * 「不支持捕获时退化为轨道内拖动」处理。
 */

const POINTER_EVENT_NAMES = [
  "pointerdown",
  "pointermove",
  "pointerup",
  "pointercancel",
  "pointerover",
  "pointerout",
  "pointerenter",
  "pointerleave",
  "gotpointercapture",
  "lostpointercapture",
];

if (typeof HTMLElement !== "undefined") {
  for (const eventName of POINTER_EVENT_NAMES) {
    const property = `on${eventName}`;
    if (property in HTMLElement.prototype) {
      continue;
    }
    Object.defineProperty(HTMLElement.prototype, property, {
      configurable: true,
      writable: true,
      value: null,
    });
  }
}
