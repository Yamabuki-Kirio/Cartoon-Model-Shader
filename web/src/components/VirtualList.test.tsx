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
});
