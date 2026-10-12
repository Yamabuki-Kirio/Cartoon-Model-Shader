import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import { NavPanel, buildEntries } from "./NavPanel";
import { makeTestWorkspace } from "../testing/endpoints";
import { groupNode, materialNode, schemaPayload } from "../testing/fixtures";
import { parseSchema } from "../schema/parse";

afterEach(cleanup);

async function setup(groups?: unknown[]) {
  const { workspace } = makeTestWorkspace({
    surfaceSchema: async () => schemaPayload({ groups: groups ?? defaultGroups() }),
  });
  await workspace.actions.bootstrap();
  return workspace;
}

function defaultGroups() {
  return [
    groupNode("Cel_Skin", { children: [] }),
    materialNode({
      material: "皮肤_脸",
      classification: "skin",
      classificationLabel: "皮肤",
      targetGroup: "Cel_Skin",
      users: ["model_mesh"],
    }),
    materialNode({
      material: "头发_前",
      classification: "hair",
      classificationLabel: "头发",
      targetGroup: "Cel_Hair",
      users: ["model_hair"],
      params: { roughness: { editable: false, readonlyReason: "structural" } },
    }),
  ];
}

describe("导航面板 · 逐材质", () => {
  it("材质分组与 Cel 分组并列出现，材质条目带分类 / 使用对象 / 可调项数", async () => {
    const workspace = await setup();
    const { getByTestId } = render(<NavPanel workspace={workspace} />);

    expect(getByTestId("nav-cel.Cel_Skin")).toBeTruthy();
    expect(getByTestId("nav-material.皮肤_脸")).toBeTruthy();
    expect(getByTestId("nav-material.头发_前")).toBeTruthy();

    const meta = getByTestId("nav-material-meta-material.皮肤_脸").textContent ?? "";
    expect(meta).toContain("皮肤");
    expect(meta).toContain("model_mesh");
    expect(meta).toContain("6/6 项可调");

    // 只读项也算进总数：可调数如实反映「这次能改几项」
    const hair = getByTestId("nav-material-meta-material.头发_前").textContent ?? "";
    expect(hair).toContain("5/6 项可调");
  });

  it("「仅材质」过滤只留材质分组", async () => {
    const workspace = await setup();
    const { getByTestId, queryByTestId } = render(<NavPanel workspace={workspace} />);

    fireEvent.click(getByTestId("nav-filter-material"));
    expect(getByTestId("nav-material.皮肤_脸")).toBeTruthy();
    expect(queryByTestId("nav-cel.Cel_Skin")).toBeNull();
  });

  it("搜索命中材质名、分类标签与使用对象", async () => {
    const workspace = await setup();
    const { getByTestId, queryByTestId } = render(<NavPanel workspace={workspace} />);
    const search = getByTestId("nav-panel").querySelector("input[type=search]") as HTMLInputElement;

    fireEvent.input(search, { target: { value: "头发" } });
    expect(getByTestId("nav-material.头发_前")).toBeTruthy();
    expect(queryByTestId("nav-material.皮肤_脸")).toBeNull();

    // 分类键也能搜（用户可能照着 rules 里的键名找）
    fireEvent.input(search, { target: { value: "skin" } });
    expect(getByTestId("nav-material.皮肤_脸")).toBeTruthy();
    expect(queryByTestId("nav-material.头发_前")).toBeNull();

    // 使用对象名也能搜 —— 这是「这个材质影响谁」最直接的入口
    fireEvent.input(search, { target: { value: "model_hair" } });
    expect(getByTestId("nav-material.头发_前")).toBeTruthy();
    expect(queryByTestId("nav-material.皮肤_脸")).toBeNull();
  });

  it("非材质分组的 material 元信息为 null（不会被误当成材质）", async () => {
    const schema = parseSchema(
      schemaPayload({ groups: [groupNode("Cel_Skin", { children: [] })] })
    );
    const entries = buildEntries(schema, new Set<string>());
    expect(entries).toHaveLength(1);
    expect(entries[0].material).toBeNull();
  });
});
