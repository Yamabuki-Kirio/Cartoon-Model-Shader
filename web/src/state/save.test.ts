import { describe, expect, it } from "vitest";
import {
  commitPreparedPayload,
  commitStatusPayload,
} from "../testing/fixtures";
import { AppError, type ErrorDetail } from "../api/types";
import { parseSchema } from "../schema/parse";
import { schemaPayload } from "../testing/fixtures";
import {
  confirmFromError,
  confirmFromPrepared,
  emptySaveState,
  saveBusy,
  saveFailureNotice,
  saveStatusRows,
  saveTargetProblem,
  splitSaveDraft,
} from "./save";
import { commitStatusOf, commitStepsOf, presetDraftFor } from "./workspace";

function detail(overrides: Partial<ErrorDetail> = {}): ErrorDetail {
  return { code: "INTERNAL_ERROR", message: "失败", retryable: false, hint: null, details: null, ...overrides };
}

describe("保存状态：基本形状", () => {
  it("默认另存为、面板是关的、不在忙", () => {
    const save = emptySaveState();
    expect(save.mode).toBe("save_as");
    expect(save.open).toBe(false);
    expect(save.confirm).toBeNull();
    expect(save.outcome).toBeNull();
    expect(saveBusy(save)).toBe(false);
  });

  it("只有 prepare / commit 两个阶段算忙", () => {
    for (const phase of ["preparing", "committing"] as const) {
      expect(saveBusy({ ...emptySaveState(), phase })).toBe(true);
    }
    for (const phase of ["idle", "confirming", "saved", "failed"] as const) {
      expect(saveBusy({ ...emptySaveState(), phase })).toBe(false);
    }
  });
});

describe("另存为路径预检", () => {
  it("空 / 相对 / 后缀不对都要当场拦下", () => {
    expect(saveTargetProblem("")).toContain("绝对路径");
    expect(saveTargetProblem("   ")).toContain("绝对路径");
    expect(saveTargetProblem("角色.blend")).toContain("绝对路径");
    expect(saveTargetProblem("项目/角色.blend")).toContain("绝对路径");
    expect(saveTargetProblem("D:\\项目\\角色.blend")).toBeNull();
    expect(saveTargetProblem("D:/项目/角色.BLEND")).toBeNull();
    expect(saveTargetProblem("/home/me/角色.blend")).toBeNull();
    expect(saveTargetProblem("\\\\nas\\共享\\角色.blend")).toBeNull();
    expect(saveTargetProblem("D:\\项目\\角色.blend1")).toContain(".blend");
  });

  it("只拦「一定不合法」的两种，不猜能不能写", () => {
    // 盘符存在与否、目录权限、文件是否被占用 —— 只有服务端知道，这里不猜。
    expect(saveTargetProblem("Z:\\不存在的盘\\角色.blend")).toBeNull();
  });
});

describe("二次确认信息", () => {
  it("从 SAVE_CONFIRM_REQUIRED 的 details 里取路径，且只认这一条错误码", () => {
    const info = confirmFromError(
      detail({
        code: "SAVE_CONFIRM_REQUIRED",
        details: {
          mode: "save_as",
          target_path: "D:\\项目\\角色.blend",
          backup_path: "D:\\项目\\角色.backup-20261009.blend",
          warnings: ["目标文件已存在", "当前工程有未保存修改"],
        },
      })
    );
    expect(info).toEqual({
      mode: "save_as",
      targetPath: "D:\\项目\\角色.blend",
      backupPath: "D:\\项目\\角色.backup-20261009.blend",
      warnings: ["目标文件已存在", "当前工程有未保存修改"],
    });
    // 别的错误码不是「待确认」
    expect(confirmFromError(detail({ code: "SAVE_FAILED" }))).toBeNull();
  });

  it("details 形状对不上时返回 null —— 绝不编一条路径出来让用户确认", () => {
    expect(confirmFromError(detail({ code: "SAVE_CONFIRM_REQUIRED" }))).toBeNull();
    expect(
      confirmFromError(detail({ code: "SAVE_CONFIRM_REQUIRED", details: { target_path: 7 } }))
    ).toBeNull();
    expect(
      confirmFromError(detail({ code: "SAVE_CONFIRM_REQUIRED", details: { target_path: "" } }))
    ).toBeNull();
  });

  it("warnings 里混进非字符串时只留字符串", () => {
    const info = confirmFromError(
      detail({
        code: "SAVE_CONFIRM_REQUIRED",
        details: { target_path: "D:\\a.blend", warnings: ["ok", 3, null] },
      })
    );
    expect(info?.warnings).toEqual(["ok"]);
  });

  it("overwrite 模式归一；未知模式一律当 save_as", () => {
    expect(confirmFromPrepared({ ...commitPreparedPayload(), mode: "overwrite" }).mode).toBe("overwrite");
    expect(confirmFromPrepared({ ...commitPreparedPayload(), mode: "??? " }).mode).toBe("save_as");
  });
});

describe("四态展示", () => {
  it("没有状态时一行都不给（不编「全部 false」）", () => {
    expect(saveStatusRows(null)).toEqual([]);
  });

  it("成功保存：应用 / 回读 / 保存为绿，备份与回滚说明「不需要 / 未触发」", () => {
    const rows = saveStatusRows(commitStatusPayload() as never);
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.applied.tone).toBe("ok");
    expect(byKey.readback.tone).toBe("ok");
    expect(byKey.saved.tone).toBe("ok");
    expect(byKey.saved.value).toContain("D:\\项目\\角色-调参.blend");
    expect(byKey.backup.tone).toBe("skip");
    expect(byKey.backup.value).toContain("不需要");
    expect(byKey.rollback.tone).toBe("skip");
    expect(byKey.rollback.value).toContain("未触发");
    // 参数面那一半是真实写入过的，必须单独列出来
    expect(byKey["applied-surface"]).toBeTruthy();
    expect(byKey["readback-surface"].tone).toBe("ok");
  });

  it("失败保存：应用绿、回读红、备份红、保存红，五态各自独立", () => {
    const rows = saveStatusRows(
      commitStatusPayload({
        readback_verified: false,
        mismatches: [{ parameter: "color.exposure" }],
        backup: { required: true, created: false, path: null, path_confirmed: "D:\\项目\\角色.backup.blend" },
        saved: false,
        save: undefined,
      }) as never
    );
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.applied.tone).toBe("ok");
    expect(byKey.readback.tone).toBe("bad");
    expect(byKey.backup.tone).toBe("bad");
    expect(byKey.backup.value).toContain("未生成");
    expect(byKey.saved.tone).toBe("bad");
    expect(byKey.rollback.tone).toBe("skip");
  });

  it("状态缺失的字段一律「未知」，不写成 false", () => {
    const rows = saveStatusRows({ target_path: "D:\\a.blend" } as never);
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.applied.value).toBe("未知");
    expect(byKey.readback.value).toBe("未知");
    expect(byKey.saved.value).toBe("未知");
  });

  it("保存了但回读的路径/文件对不上时改判为红（不报假绿）", () => {
    const rows = saveStatusRows(
      commitStatusPayload({
        save: { path_after: "D:\\别的.blend", path_matches: false, file_exists: true },
      }) as never
    );
    const saved = rows.find((row) => row.key === "saved");
    expect(saved?.tone).toBe("bad");
    expect(saved?.value).toContain("回读路径与目标不一致");
  });

  it("回滚触发过就必须单独说清有没有验证", () => {
    const attempted = saveStatusRows(
      commitStatusPayload({
        saved: false,
        rollback: {
          attempted: true,
          verified: false,
          mismatches: [],
          code: "ROLLBACK_VERIFY_FAILED",
          error: "回滚后回读仍不一致",
          reason: null,
        },
      }) as never
    );
    const rollback = attempted.find((row) => row.key === "rollback");
    expect(rollback?.tone).toBe("bad");
    expect(rollback?.value).toContain("未验证");
  });
});

describe("失败文案", () => {
  it("永远说清「磁盘没被写」与「草稿还在」", () => {
    expect(saveFailureNotice(null)).toContain("草稿与最后一张成功预览都已保留");
    expect(saveFailureNotice(null)).toContain("保存未完成");
  });

  it("生成过备份时把备份路径说出来", () => {
    const text = saveFailureNotice(
      commitStatusPayload({
        saved: false,
        backup: { required: true, created: true, path: "D:\\项目\\角色.backup.blend", path_confirmed: null },
      }) as never
    );
    expect(text).toContain("D:\\项目\\角色.backup.blend");
    expect(text).toContain("目标文件未被改动");
    expect(text).toContain("草稿与最后一张成功预览都已保留");
  });

  it("草稿写进去了但没落盘时必须说「已写进 Blender」——那是完全不同的一件事", () => {
    const text = saveFailureNotice(commitStatusPayload({ saved: false, save: undefined }) as never);
    expect(text).toContain("已写进 Blender");
    expect(text).toContain("没能落盘");
  });
});

describe("错误信封里的四态", () => {
  it("details.status 拿得到就返回，拿不到一律 null", () => {
    expect(commitStatusOf(detail({ details: { status: { saved: false } } }))).toEqual({ saved: false });
    expect(commitStatusOf(detail())).toBeNull();
    expect(commitStatusOf(detail({ details: { status: [1, 2] } }))).toBeNull();
    expect(commitStatusOf(detail({ details: { status: "saved" } }))).toBeNull();
  });

  it("details.steps 只留字符串", () => {
    expect(commitStepsOf(detail({ details: { steps: ["a", 1, "b"] } }))).toEqual(["a", "b"]);
    expect(commitStepsOf(detail())).toEqual([]);
  });
});

describe("草稿拆分", () => {
  it("v4 工作台的草稿整份进参数面，L0 留空", () => {
    const schema = parseSchema(schemaPayload());
    const { l0, surface } = splitSaveDraft(schema, {
      "cel.Cel_Skin.emission_strength": 2.5,
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "EASE" },
    });
    expect(l0).toEqual({});
    expect(surface).toEqual({
      "cel.Cel_Skin.emission_strength": 2.5,
      "cel.Cel_Skin.ramp": { elements: [], interpolation: "EASE" },
    });
  });

  it("schema 里没有的 id 会被丢掉（不把本机不认识的东西发给服务端）", () => {
    const schema = parseSchema(schemaPayload());
    const { surface } = splitSaveDraft(schema, { "cel.不存在的组.x": 1 });
    expect(surface).toEqual({});
  });

  it("只读节点不进草稿 —— 它们本来就不接受写入", () => {
    const schema = parseSchema(schemaPayload());
    const { surface } = splitSaveDraft(schema, {
      "cel.Cel_Skin.managed_mode": "simple",
      "cel.Cel_Skin.emission_strength": 3,
    });
    expect(Object.keys(surface)).toEqual(["cel.Cel_Skin.emission_strength"]);
  });
});

describe("预设套用", () => {
  const schema = parseSchema(schemaPayload());

  it("只写当前工程认得且可写的项", () => {
    const { applied, skipped } = presetDraftFor(schema, {
      "cel.Cel_Skin.emission_strength": 4,
      "material.别的工程才有的材质.base_color": [1, 1, 1, 1],
    });
    expect(Object.keys(applied)).toEqual(["cel.Cel_Skin.emission_strength"]);
    expect(skipped).toEqual(["material.别的工程才有的材质.base_color"]);
  });

  it("只读项也算「认不得」：它一样写不进去", () => {
    const { applied, skipped } = presetDraftFor(schema, {
      "cel.Cel_Skin.managed_mode": "simple",
      "Sakura_Hair_Reference.ramp": { elements: [], interpolation: "LINEAR" },
    });
    expect(applied).toEqual({});
    expect(skipped).toContain("cel.Cel_Skin.managed_mode");
  });

  it("跳过的项必须报出来 —— 静默丢弃会让用户以为生效了", () => {
    const { skipped } = presetDraftFor(schema, {
      "material.另一个工程.roughness": 0.5,
      "cel.也不存在.x": 1,
    });
    expect(skipped).toHaveLength(2);
  });

  it("返回的是深拷贝：改它不该改到预设本体", () => {
    const source = { "cel.Cel_Skin.ramp": { elements: [{ position: 0, color: [0, 0, 0, 1] }] } };
    const before = JSON.stringify(source);
    const { applied } = presetDraftFor(schema, source);
    (applied["cel.Cel_Skin.ramp"] as { elements: unknown[] }).elements.push("x");
    expect(JSON.stringify(source)).toBe(before);
  });

  it("没有 schema 时全部跳过，而不是抛异常", () => {
    const { applied, skipped } = presetDraftFor(null, { "cel.X.y": 1 });
    expect(applied).toEqual({});
    expect(skipped).toEqual(["cel.X.y"]);
  });
});

describe("AppError 的两条判定", () => {
  it("SAVE_CONFIRM_REQUIRED 是「待确认」，不是结构失效", () => {
    const error = new AppError({ code: "SAVE_CONFIRM_REQUIRED" }, 409);
    expect(error.isSaveConfirmRequired).toBe(true);
    expect(error.isStructureFatal).toBe(false);
  });

  it("结构失效仍然只认两个稳定错误码", () => {
    expect(new AppError({ code: "STRUCTURE_CHANGED" }, 409).isStructureFatal).toBe(true);
    expect(new AppError({ code: "IDENTITY_MISSING" }, 409).isStructureFatal).toBe(true);
    expect(new AppError({ code: "COMMIT_TOKEN_EXPIRED" }, 409).isStructureFatal).toBe(false);
  });
});
