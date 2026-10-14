import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { VirtualList } from "./VirtualList";

afterEach(cleanup);

describe("虚拟列表", () => {
  it("只渲染可视范围（+ overscan），不是全部条目", () => {
    const items = Array.from({ length: 500 }, (_, index) => index);
    const { getByTestId } = render(
      <VirtualList
        items={items}
        itemHeight={20}
        height={200}
        overscan={4}
        renderItem={(item) => <div class="row">{item}</div>}
      />
    );
    const list = getByTestId("virtual-list");
    expect(list.getAttribute("data-total")).toBe("500");
    const rendered = Number(list.getAttribute("data-rendered"));
    // 可视 10 行 + 上下 overscan 各 4 行
    expect(rendered).toBeLessThanOrEqual(10 + 8);
    expect(rendered).toBeGreaterThanOrEqual(10);
    expect(list.querySelectorAll(".row").length).toBe(rendered);
  });

  it("滚动后渲染的是对应窗口", () => {
    const items = Array.from({ length: 100 }, (_, index) => `第 ${index} 行`);
    const { getByTestId, getByText } = render(
      <VirtualList items={items} itemHeight={20} height={100} overscan={0} renderItem={(item) => <div class="row">{item}</div>} />
    );
    const list = getByTestId("virtual-list") as HTMLElement;
    list.scrollTop = 200;
    fireEvent.scroll(list);
    // 200/20 = 第 10 行起
    expect(getByText("第 10 行")).toBeTruthy();
  });

  it("空列表显示占位，不渲染容器", () => {
    const { queryByTestId, getByText } = render(
      <VirtualList items={[]} itemHeight={20} height={100} renderItem={() => null} empty={<p>没有内容</p>} />
    );
    expect(queryByTestId("virtual-list")).toBeNull();
    expect(getByText("没有内容")).toBeTruthy();
  });

  it("不给 height 时进入开窗模式：不测量容器，仍然只渲染一段", () => {
    const items = Array.from({ length: 800 }, (_, index) => index);
    const { getByTestId } = render(
      <VirtualList items={items} itemHeight={20} renderItem={(item) => <div class="row">{item}</div>} />
    );
    const list = getByTestId("virtual-list");
    expect(list.getAttribute("data-mode")).toBe("window");
    expect(list.getAttribute("data-total")).toBe("800");
    const rendered = Number(list.getAttribute("data-rendered"));
    expect(rendered).toBeLessThan(800);
    expect(rendered).toBeGreaterThan(0);
    // 外层占位高度是精确值：滚动条长度因此仍然正确
    const spacer = list.firstElementChild as HTMLElement;
    expect(spacer.style.height).toBe(`${800 * 20}px`);
  });

  it("开窗模式下滚动会移动窗口", () => {
    const items = Array.from({ length: 500 }, (_, index) => `第 ${index} 行`);
    const { getByTestId, queryByText } = render(
      <VirtualList items={items} itemHeight={20} renderItem={(item) => <div class="row">{item}</div>} />
    );
    const list = getByTestId("virtual-list") as HTMLElement;
    expect(queryByText("第 200 行")).toBeNull();

    list.scrollTop = 4000;
    fireEvent.scroll(list);
    expect(queryByText("第 200 行")).toBeTruthy();
    expect(queryByText("第 0 行")).toBeNull();
  });

  it("可以用 testId 区分同页的多个列表", () => {
    const { getByTestId, queryByTestId } = render(
      <VirtualList
        items={[1, 2, 3]}
        itemHeight={20}
        testId="my-list"
        renderItem={(item) => <div>{item}</div>}
      />
    );
    expect(getByTestId("my-list")).toBeTruthy();
    expect(queryByTestId("virtual-list")).toBeNull();
  });
});
