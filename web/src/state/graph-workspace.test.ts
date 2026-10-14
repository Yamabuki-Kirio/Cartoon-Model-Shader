import { afterEach, describe, expect, it, vi } from "vitest";
import { makeTestWorkspace, settle, settleTimers } from "../testing/endpoints";
import {
  graphMaterialFixture,
  graphSchemaPayload,
  graphSharedFixture,
  jobPayload,
  materialNode,
  schemaPayload,
} from "../testing/fixtures";
import { AppError } from "../api/types";

afterEach(() => {
  // 每个用例都自己造工作台；这里只保证没有跨用例的计时器残留。
});

const MATERIAL = "皮肤_脸";
const SHARED_PARAM = "mg-shared01";
const PLAIN_PARAM = "mg-common02";
const TARGET = "D:\\项目\\角色-材质图.blend";

function surfaceFixture() {
  return schemaPayload({ groups: [materialNode({ material: MATERIAL })] });
}

function graphFixture(materials?: unknown[]) {
  return graphSchemaPayload({
    materials:
      materials ??
      [
        graphMaterialFixture({
          material: MATERIAL,
          parameters: [
            graphSharedFixture(SHARED_PARAM, { group: "Cel_Shared", users: [MATERIAL, "头发_前"] }),
            ...(graphMaterialFixture().parameters as unknown[]),
          ],
          sharedGroups: { Cel_Shared: [MATERIAL, "头发_前"] },
        }),
      ],
  });
}

/**
 * 造一个带材质图的工作台。
 *
 * 默认响应都包成 `vi.fn`：用例要断言「服务端到底被调了几次」，
 * 传一个裸 async 函数进去会把夹具里的 `vi.fn` 覆盖掉，断言就成了空指针。
 */
async function setup(options: { materials?: unknown[]; overrides?: Record<string, unknown> } = {}) {
  const { workspace, endpoints } = makeTestWorkspace({
    surfaceSchema: vi.fn(async () => surfaceFixture()),
    materialGraphSchema: vi.fn(async () => graphFixture(options.materials)),
    ...(options.overrides ?? {}),
  });
  await workspace.actions.bootstrap();
  return { workspace, endpoints };
}

/** 打开保存面板并填好目标路径（`save_as` 路径必填，否则 prepare 直接返回 false）。 */
function armSave(workspace: ReturnType<typeof makeTestWorkspace>["workspace"]): void {
  workspace.actions.openSaveDialog("save_as");
  workspace.actions.setSaveTarget(TARGET);
}

describe("材质图 · 提交预览的请求体", () => {
  it("材质图 id 进 material_draft，参数面 id 进 draft，两半不串门", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.setValue(`material.${MATERIAL}.roughness`, 0.25);
    await settle();

    await workspace.actions.applyAndPreview();
    await settleTimers();

    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    const payload = endpoints.submitPreview.mock.calls[0][0] as {
      draft: Record<string, unknown>;
      material_draft: Record<string, unknown>;
      expected_material_structure_hash?: string;
    };
    expect(Object.keys(payload.material_draft)).toEqual([PLAIN_PARAM]);
    expect(Object.keys(payload.draft)).toEqual([`material.${MATERIAL}.roughness`]);
    expect(payload.expected_material_structure_hash).toBe("b".repeat(64));
  });

  it("没有材质图草稿时不发 material_draft（少发一个字段永远比发一个空对象稳）", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(`material.${MATERIAL}.roughness`, 0.25);
    await settle();
    await workspace.actions.applyAndPreview();
    await settleTimers();

    const payload = endpoints.submitPreview.mock.calls[0][0] as Record<string, unknown>;
    expect("material_draft" in payload).toBe(false);
    expect("confirmed_shared_groups" in payload).toBe(false);
  });

  it("请求体里没有节点路径、脚本、命令或文件路径（服务端也不接受）", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.setValue(`material.${MATERIAL}.base_color`, [1, 0, 0, 1]);
    await settle();
    await workspace.actions.applyAndPreview();
    await settleTimers();

    const payload = endpoints.submitPreview.mock.calls[0][0] as Record<string, unknown>;
    const serialised = JSON.stringify(payload);
    for (const forbidden of ["\\", ".py", "bpy", "import ", "eval(", "exec(", "os.", "subprocess"]) {
      expect(serialised.includes(forbidden), `请求体不应出现 ${forbidden}`).toBe(false);
    }
    // 材质图参数 id 必须是 opaque 形态（客户端拿不到节点路径）
    for (const id of Object.keys(payload.material_draft as Record<string, unknown>)) {
      expect(id).toMatch(/^mg-[A-Za-z0-9_-]+$/);
    }
  });
});

describe("材质图 · 共享节点组确认", () => {
  it("草稿写了共享参数但未确认：本地就拦下并给出影响范围，不发请求", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    await settle();

    const ok = await workspace.actions.applyAndPreview();
    await settleTimers();

    expect(ok).toBe(false);
    expect(endpoints.submitPreview).not.toHaveBeenCalled();
    const pending = workspace.store.getState().graph.pendingShared;
    expect(pending?.sharedGroup).toBe("Cel_Shared");
    expect(pending?.affectedMaterials).toEqual([MATERIAL, "头发_前"]);
    // 这不是失败：不弹红色错误，而是给一条说明
    expect(workspace.store.getState().error).toBeNull();
    expect(workspace.store.getState().notice).toContain("Cel_Shared");
  });

  it("确认后提交带上 confirmed_shared_groups，并真的发出请求", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    await settle();
    await workspace.actions.applyAndPreview();
    await settleTimers();
    expect(endpoints.submitPreview).not.toHaveBeenCalled();

    workspace.actions.confirmSharedGroup("Cel_Shared");
    await settleTimers();

    expect(workspace.store.getState().graph.confirmedSharedGroups).toEqual(["Cel_Shared"]);
    expect(endpoints.submitPreview).toHaveBeenCalledTimes(1);
    const payload = endpoints.submitPreview.mock.calls[0][0] as {
      confirmed_shared_groups: string[];
      material_draft: Record<string, unknown>;
    };
    expect(payload.confirmed_shared_groups).toEqual(["Cel_Shared"]);
    expect(Object.keys(payload.material_draft)).toEqual([SHARED_PARAM]);
  });

  it("取消确认会撤销该组的草稿改动（撤销栈里能退回来）", async () => {
    const { workspace } = await setup();
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    await settle();
    await workspace.actions.applyAndPreview();
    await settle();

    workspace.actions.cancelSharedConfirm();
    await settle();

    const draft = workspace.store.getState().draft;
    expect(SHARED_PARAM in draft).toBe(false);
    // 非共享参数的改动不受牵连
    expect(draft[PLAIN_PARAM]).toBe(3.5);
    expect(workspace.store.getState().graph.pendingShared).toBeNull();

    // 取消是走命令栈的：撤销得回来
    workspace.actions.undo();
    expect(workspace.store.getState().draft[SHARED_PARAM]).toBe(0.9);
  });

  it("服务端拒绝（SHARED_PARAMETER_CONFIRM_REQUIRED）同样走确认流程，而不是报错", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: async () => graphFixture(),
      submitPreview: (async () => {
        throw new AppError(
          {
            code: "SHARED_PARAMETER_CONFIRM_REQUIRED",
            message: "该参数属于共享节点组。",
            retryable: false,
            details: {
              parameter: SHARED_PARAM,
              shared_group: "Cel_Shared",
              affected_materials: [MATERIAL, "头发_前"],
            },
          },
          409
        );
      }) as never,
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    await settle();
    // 本地自检只拦共享参数；这里模拟「本地没拦住、服务端拦住了」
    await workspace.actions.applyAndPreview();
    await settle();

    expect(workspace.store.getState().error).toBeNull();
    expect(workspace.store.getState().graph.pendingShared).toEqual({
      parameter: SHARED_PARAM,
      sharedGroup: "Cel_Shared",
      affectedMaterials: [MATERIAL, "头发_前"],
    });
    expect(workspace.store.getState().notice).toContain("头发_前");
  });

  it("details 形状不对时不编影响范围，如实报错", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: async () => graphFixture(),
      submitPreview: (async () => {
        throw new AppError(
          {
            code: "SHARED_PARAMETER_CONFIRM_REQUIRED",
            message: "该参数属于共享节点组。",
            retryable: false,
            details: { affected_materials: ["a"] },
          },
          409
        );
      }) as never,
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    await settle();
    await workspace.actions.applyAndPreview();
    await settle();

    expect(workspace.store.getState().graph.pendingShared).toBeNull();
    expect(workspace.store.getState().error?.message).toContain("没有给出组名与受影响材质");
  });
});

describe("材质图 · 生效值与恢复回执", () => {
  it("生效值取上次成功预览的 applied_material_graph", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: async () => graphFixture(),
      job: (async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          result: {
            ...jobPayload().result,
            applied_material_graph: { [PLAIN_PARAM]: 2.5 },
            material_graph_restore_verified: true,
            material_graph_restore_mismatches: [],
            material_graph_baseline_id: "mg0000000002",
            material_structure_hash: "c".repeat(64),
          },
        })) as never,
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue(PLAIN_PARAM, 2.5);
    await settle();
    await workspace.actions.applyAndPreview();
    await settleTimers();

    const state = workspace.store.getState();
    expect(state.effective[PLAIN_PARAM]).toBe(2.5);
    expect(state.graph.restoreVerified).toBe(true);
    expect(state.graph.restoreMismatches).toEqual([]);
    expect(state.graph.baselineId).toBe("mg0000000002");
    expect(state.graph.structureHash).toBe("c".repeat(64));
  });

  it("拿不到恢复证据时是「未知」而不是「已恢复」（不报假绿）", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: async () => graphFixture(),
      job: (async (jobId: string) =>
        jobPayload({
          job_id: jobId,
          result: { ...jobPayload().result, applied_material_graph: { [PLAIN_PARAM]: 2.5 } },
        })) as never,
    });
    await workspace.actions.bootstrap();
    workspace.actions.setValue(PLAIN_PARAM, 2.5);
    await settle();
    await workspace.actions.applyAndPreview();
    await settleTimers();
    expect(workspace.store.getState().graph.restoreVerified).toBeNull();
  });
});

describe("材质图 · 基线与结构失效", () => {
  it("没有材质图基线（NO_BASELINE）不是错误，界面提示先刷新基线", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: (async () => {
        throw new AppError({ code: "NO_BASELINE", message: "尚未建立材质图基线。", retryable: true }, 409);
      }) as never,
    });
    await workspace.actions.bootstrap();
    await settle();

    const graph = workspace.store.getState().graph;
    expect(graph.loaded).toBe(false);
    expect(graph.baselineReady).toBe(false);
    expect(graph.error).toBeNull();
    expect(workspace.store.getState().error).toBeNull();
  });

  it("其他加载失败如实报出（遮住只会让人以为材质图本来就没参数）", async () => {
    const { workspace } = makeTestWorkspace({
      surfaceSchema: async () => surfaceFixture(),
      materialGraphSchema: (async () => {
        throw new AppError({ code: "BLENDER_ERROR", message: "探针连不上。", retryable: true }, 502);
      }) as never,
    });
    await workspace.actions.bootstrap();
    await settle();
    expect(workspace.store.getState().graph.error?.code).toBe("BLENDER_ERROR");
  });

  it("刷新基线会重新加载材质图 schema，并清空共享组确认", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.confirmSharedGroup("Cel_Shared", { preview: false });
    expect(workspace.store.getState().graph.confirmedSharedGroups).toEqual(["Cel_Shared"]);

    const before = endpoints.materialGraphSchema.mock.calls.length;
    await workspace.actions.refreshBaseline();
    await settleTimers();

    expect(endpoints.materialGraphSchema.mock.calls.length).toBeGreaterThan(before);
    expect(workspace.store.getState().graph.confirmedSharedGroups).toEqual([]);
    expect(workspace.store.getState().graph.loaded).toBe(true);
  });

  it("结构失效会作废材质图草稿与共享组确认", async () => {
    const { workspace } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.confirmSharedGroup("Cel_Shared", { preview: false });
    await settle();

    workspace.actions.applyStructureFatal({
      code: "STRUCTURE_CHANGED",
      message: "材质节点图结构已变化",
      retryable: true,
    });

    const state = workspace.store.getState();
    expect(state.historyBlocked).toBe(true);
    expect(state.draft).toEqual({});
    expect(state.graph.confirmedSharedGroups).toEqual([]);
    expect(state.graph.pendingShared).toBeNull();
  });
});

describe("材质图 · 草稿范围", () => {
  it("改一个材质的参数不影响别的材质（草稿与复位都按 id 归属）", async () => {
    const { workspace } = await setup({
      materials: [
        graphMaterialFixture({
          material: MATERIAL,
          parameters: [graphSharedFixture(SHARED_PARAM, { group: "", users: [] })],
        }),
        graphMaterialFixture({
          material: "头发_前",
          parameters: [
            graphSharedFixture("mg-hair01", { group: "", users: [] }),
            graphSharedFixture("mg-hair02", { group: "", users: [] }),
          ],
        }),
      ],
    });
    workspace.actions.setValue(SHARED_PARAM, 1.5);
    workspace.actions.setValue("mg-hair01", 2.5);
    await settle();

    workspace.actions.resetGraphMaterial(MATERIAL);
    await settle();

    const draft = workspace.store.getState().draft;
    expect(SHARED_PARAM in draft).toBe(false);
    expect(draft["mg-hair01"]).toBe(2.5);
  });

  it("「复位全部材质图」只清材质图，不碰参数面草稿", async () => {
    const { workspace } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.setValue(`material.${MATERIAL}.roughness`, 0.25);
    await settle();

    workspace.actions.resetAllGraphDraft();
    await settle();

    const draft = workspace.store.getState().draft;
    expect(PLAIN_PARAM in draft).toBe(false);
    expect(draft[`material.${MATERIAL}.roughness`]).toBe(0.25);
  });

  it("材质图与参数面共用同一个撤销栈", async () => {
    const { workspace } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.sealHistory();
    workspace.actions.setValue(`material.${MATERIAL}.roughness`, 0.25);
    await settle();
    expect(workspace.store.getState().undoDepth).toBe(2);

    workspace.actions.undo();
    expect(workspace.store.getState().draft[`material.${MATERIAL}.roughness`]).toBeUndefined();
    expect(workspace.store.getState().draft[PLAIN_PARAM]).toBe(3.5);
    workspace.actions.undo();
    expect(workspace.store.getState().draft[PLAIN_PARAM]).toBeUndefined();
  });
});

describe("材质图 · 保存", () => {
  it("prepare 与 commit 都带材质图草稿、共享组确认与材质图结构指纹", async () => {
    const { workspace, endpoints } = await setup();
    workspace.actions.setValue(PLAIN_PARAM, 3.5);
    workspace.actions.confirmSharedGroup("Cel_Shared", { preview: false });
    workspace.actions.setValue(SHARED_PARAM, 0.9);
    await settle();

    armSave(workspace);
    const ok = await workspace.actions.prepareSave(false);
    await settleTimers();

    expect(ok).toBe(true);
    const prepared = endpoints.commitPrepare.mock.calls[0][0] as Record<string, unknown>;
    expect(prepared.material_draft).toEqual({ [PLAIN_PARAM]: 3.5, [SHARED_PARAM]: 0.9 });
    expect(prepared.confirmed_shared_groups).toEqual(["Cel_Shared"]);
    expect(prepared.expected_material_structure_hash).toBe("b".repeat(64));
    // 参数面那一半不会混进材质图 id
    expect(Object.keys(prepared.surface_draft as Record<string, unknown>)).toEqual([]);

    const committed = endpoints.commitApply.mock.calls[0][0] as Record<string, unknown>;
    expect(committed.material_draft).toEqual({ [PLAIN_PARAM]: 3.5, [SHARED_PARAM]: 0.9 });
    expect(committed.confirmed_shared_groups).toEqual(["Cel_Shared"]);
    expect(committed.expected_material_structure_hash).toBe("b".repeat(64));
  });

  it("只读的材质图参数不会进保存载荷", async () => {
    const { workspace, endpoints } = await setup();
    // 直接塞一个只读参数进草稿：提交前必须被 sanitize 掉
    const state = workspace.store.getState();
    expect(state.graph.schema).toBeTruthy();
    const readonlyId = state.graph.schema!.materials[0].params.find(
      (param) => !param.node.editable
    )?.node.id;
    expect(readonlyId).toBeTruthy();
    workspace.actions.setValue(readonlyId as string, 123);
    await settle();

    armSave(workspace);
    await workspace.actions.prepareSave(false);
    await settleTimers();

    const prepared = endpoints.commitPrepare.mock.calls[0][0] as Record<string, unknown>;
    expect(prepared.material_draft).toEqual({});
  });
});
