import { describe, expect, it } from "vitest";
import { DraftHistory } from "./commands";

function hist() {
  return new DraftHistory();
}

const A = { a: 1 };
const B = { a: 2 };
const C = { a: 3 };

describe("草稿命令栈", () => {
  it("相同 mergeKey 的连续输入合并成一条（撤销一次退回最初）", () => {
    const history = hist();
    history.push({ kind: "setValue", label: "改 a", groupId: "g", before: A, after: B, mergeKey: "setValue:x" });
    history.push({ kind: "setValue", label: "改 a", groupId: "g", before: B, after: C, mergeKey: "setValue:x" });
    expect(history.undoDepth).toBe(1);

    const step = history.undoStep();
    expect(step?.draft).toEqual(A);
    expect(step?.command.after).toEqual(C);
  });

  it("seal() 之后不再合并 —— 一次拖动一条记录", () => {
    const history = hist();
    history.push({ kind: "dragElement", label: "拖", groupId: "g", before: A, after: B, mergeKey: "drag:x" });
    history.seal();
    history.push({ kind: "dragElement", label: "拖", groupId: "g", before: B, after: C, mergeKey: "drag:x" });
    expect(history.undoDepth).toBe(2);

    expect(history.undoStep()?.draft).toEqual(B);
    expect(history.undoStep()?.draft).toEqual(A);
  });

  it("不同色标（mergeKey 不同）永不合并", () => {
    const history = hist();
    history.push({ kind: "dragElement", label: "色标 1", groupId: "g", before: A, after: B, mergeKey: "drag:x#0" });
    history.push({ kind: "dragElement", label: "色标 2", groupId: "g", before: B, after: C, mergeKey: "drag:x#1" });
    expect(history.undoDepth).toBe(2);
  });

  it("复位与复制是原子命令：即使给了 mergeKey 也不合并", () => {
    const history = hist();
    history.push({ kind: "resetGroup", label: "复位", groupId: "g", before: A, after: B, mergeKey: "same" });
    history.push({ kind: "resetGroup", label: "复位", groupId: "g", before: B, after: C, mergeKey: "same" });
    history.push({ kind: "copyToGroup", label: "复制", groupId: "g", before: C, after: A, mergeKey: "same" });
    expect(history.undoDepth).toBe(3);
  });

  it("mergeKey 为 null 的命令不合并", () => {
    const history = hist();
    history.push({ kind: "setValue", label: "x", groupId: "g", before: A, after: B, mergeKey: null });
    history.push({ kind: "setValue", label: "y", groupId: "g", before: B, after: C, mergeKey: null });
    expect(history.undoDepth).toBe(2);
  });

  it("撤销 / 重做往返，且重做栈在 push 后被清空", () => {
    const history = hist();
    history.push({ kind: "setValue", label: "1", groupId: "g", before: A, after: B, mergeKey: null });
    history.push({ kind: "setValue", label: "2", groupId: "g", before: B, after: C, mergeKey: null });

    expect(history.undoStep()?.draft).toEqual(B);
    expect(history.redoDepth).toBe(1);
    expect(history.redoStep()?.draft).toEqual(C);
    expect(history.redoDepth).toBe(0);

    history.undoStep();
    history.push({ kind: "setValue", label: "3", groupId: "g", before: A, after: { a: 9 }, mergeKey: null });
    expect(history.redoDepth).toBe(0);
  });

  it("空栈撤销 / 重做返回 null", () => {
    const history = hist();
    expect(history.undoStep()).toBeNull();
    expect(history.redoStep()).toBeNull();
  });

  it("clear() 清空两个栈（刷新基线时用）", () => {
    const history = hist();
    history.push({ kind: "setValue", label: "1", groupId: "g", before: A, after: B, mergeKey: null });
    history.undoStep();
    history.clear();
    expect(history.undoDepth).toBe(0);
    expect(history.redoDepth).toBe(0);
  });

  it("快照是深拷贝：命令里的 before/after 不会被后续改动污染", () => {
    const history = hist();
    const before = { a: [1] };
    const after = { a: [2] };
    history.push({ kind: "setValue", label: "1", groupId: "g", before, after, mergeKey: null });
    after.a[0] = 99;
    before.a[0] = 42;
    const step = history.undoStep();
    expect(step?.draft).toEqual({ a: [1] });
    expect(step?.command.after).toEqual({ a: [2] });
  });

  it("合并时保留第一个 before、用新的 after", () => {
    const history = hist();
    history.push({ kind: "setValue", label: "1", groupId: "g", before: A, after: B, mergeKey: "k" });
    history.push({ kind: "setValue", label: "2", groupId: "g", before: B, after: C, mergeKey: "k" });
    const step = history.undoStep();
    expect(step?.draft).toEqual(A);
    expect(step?.command.after).toEqual(C);
    // 重做回到合并后的最终状态
    expect(history.redoStep()?.draft).toEqual(C);
  });

  it("mergeKey 生成器区分参数与色标", () => {
    expect(DraftHistory.mergeKey("setValue", "p")).toBe("setValue:p");
    expect(DraftHistory.mergeKey("dragElement", "p", 2)).toBe("dragElement:p#2");
  });
});
