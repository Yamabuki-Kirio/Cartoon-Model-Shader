"""PR #7 验收回归：支持节点矩阵、注入拒绝、任务生命周期与保存闭环。

与既有的三个测试文件分工：

* ``test_material_graph.py`` / ``test_material_graph_trace.py`` —— 探测、追踪与 schema；
* ``test_material_graph_execution.py`` —— 计划编译与执行器（单元级）；
* ``test_material_graph_api.py`` —— 接口契约与令牌绑定；
* **本文件** —— 把「真机验收清单」里能在桩上复核的部分端到端钉住：
  支持节点矩阵、请求注入矩阵、成功 / 渲染失败 / 写入失败 / 回读不一致 / 回滚失败
  五种任务终态，以及「保存完整材质图草稿」。

真机（Blender 本体）验收仍需人工执行，见 ``docs/技术设计.md`` 的验收矩阵；
本文件**不声称**真机已验收。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from src.server.app import create_app
from src.server.material_graph.semantics import (
    LEVEL_CONFIRMED,
    LEVEL_INFERRED,
    SEM_MAP_RANGE,
    SEM_MATH,
    SEM_RAMP,
    SEM_VALUE,
)
from src.server.material_graph.trace import MAX_DEPTH
from src.server.material_graph_service import MaterialGraphService
from tests.fake_bpy import FakeBpy, FakeColorRamp, FakeLink, FakeNode, run_generated_code
from tests.test_v4_surface_api import (
    CEL_GROUP,
    FakeMCPServer,
    SurfaceInjector,
    authed,
    make_config,
    unauthed,
    wait_job,
)

MATERIAL_A = "皮肤_脸"
MATERIAL_B = "衣服_上衣"


def _out(node: FakeNode, name: str):
    for sock in node.outputs:
        if sock.name == name:
            return sock
    raise KeyError(f"{node.name} 没有输出插座 {name}")


def _rich_nodes() -> dict[str, FakeNode]:
    """一张覆盖 Value / RGB / ColorRamp / Math / Map Range 的最小节点图。"""
    out = FakeNode(
        "Material Output",
        "OUTPUT_MATERIAL",
        {"Surface": None},
        bl_idname="ShaderNodeOutputMaterial",
    )
    bsdf = FakeNode(
        "Principled BSDF",
        "BSDF_PRINCIPLED",
        {"Base Color": [1.0, 1.0, 1.0, 1.0], "Roughness": 0.5},
        bl_idname="ShaderNodeBsdfPrincipled",
        outputs={"BSDF": "SHADER"},
    )
    mix = FakeNode(
        "Mix",
        "MIX_RGB",
        {"Fac": 0.5, "Color A": [0.9, 0.4, 0.4, 1.0], "Color B": [0.2, 0.2, 0.3, 1.0]},
        bl_idname="ShaderNodeMixRGB",
        outputs={"Color": "RGBA"},
    )
    rgb = FakeNode(
        "RGB", "RGB", {"Color": [0.9, 0.4, 0.4, 1.0]},
        bl_idname="ShaderNodeRGB", outputs={"Color": "RGBA"},
    )
    ramp = FakeNode(
        "ColorRamp", "VALTORGB", {"Fac": 0.5},
        color_ramp=FakeColorRamp(), bl_idname="ShaderNodeValToRGB",
        outputs={"Color": "RGBA"},
    )
    value = FakeNode(
        "Value", "VALUE", {"Value": 0.25},
        bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"},
    )
    math = FakeNode(
        "Math", "MATH", {"Value": 0.5, "Value_001": 0.25},
        bl_idname="ShaderNodeMath", outputs={"Value": "VALUE"},
    )
    map_range = FakeNode(
        "Map Range", "MAP_RANGE",
        {"Value": 0.5, "From Min": 0.0, "From Max": 1.0, "To Min": 0.0, "To Max": 10.0},
        bl_idname="ShaderNodeMapRange", outputs={"Result": "VALUE"},
    )
    # Hue/Saturation 是唯一**带声明值域**的可写标量来源（饱和度 0–10）。
    # 没有它，「参数越界拒绝」这条验收就只能在单元层覆盖，接口层会静默跳过。
    hue_sat = FakeNode(
        "Hue Saturation Value", "HUE_SAT",
        {"Hue": 0.5, "Saturation": 1.0, "Value": 1.0, "Fac": 1.0},
        bl_idname="ShaderNodeHueSaturation", outputs={"Color": "RGBA"},
    )
    return {
        "out": out, "bsdf": bsdf, "mix": mix, "rgb": rgb,
        "ramp": ramp, "value": value, "math": math, "map_range": map_range,
        "hue_sat": hue_sat,
    }


def build_rich_material(
    fake: FakeBpy, material: str = MATERIAL_A, *, orphan: bool = False
) -> None:
    nodes = _rich_nodes()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    links = [
        FakeLink(nodes["value"], _out(nodes["value"], "Value"), nodes["ramp"], nodes["ramp"].inputs["Fac"]),
        FakeLink(nodes["ramp"], _out(nodes["ramp"], "Color"), nodes["mix"], nodes["mix"].inputs["Color A"]),
        FakeLink(nodes["rgb"], _out(nodes["rgb"], "Color"), nodes["mix"], nodes["mix"].inputs["Color B"]),
        FakeLink(nodes["mix"], _out(nodes["mix"], "Color"), nodes["bsdf"], nodes["bsdf"].inputs["Base Color"]),
        FakeLink(nodes["math"], _out(nodes["math"], "Value"), nodes["bsdf"], nodes["bsdf"].inputs["Roughness"]),
        FakeLink(
            nodes["map_range"], _out(nodes["map_range"], "Result"),
            nodes["hue_sat"], nodes["hue_sat"].inputs["Fac"],
        ),
        FakeLink(
            nodes["hue_sat"], _out(nodes["hue_sat"], "Color"),
            nodes["mix"], nodes["mix"].inputs["Fac"],
        ),
        FakeLink(nodes["bsdf"], _out(nodes["bsdf"], "BSDF"), nodes["out"], nodes["out"].inputs["Surface"]),
    ]
    all_nodes = list(nodes.values())
    if orphan:
        # 在树里但**没有任何连线**：反向追踪必须把这类节点排除在参数面之外。
        all_nodes.append(
            FakeNode("Orphan", "VALUE", {"Value": 9.0}, bl_idname="ShaderNodeValue",
                     outputs={"Value": "VALUE"})
        )
    fake.add_material_graph(material, nodes=all_nodes, links=links, users=[mesh])


def describe_material(fake: FakeBpy) -> dict[str, Any]:
    from src.server import surface_probe

    return surface_probe.describe_groups(
        surface_probe.redact_describe(
            surface_probe.parse_describe(
                run_generated_code(surface_probe.build_describe_code(), fake)
            )
        )
    )


def capture(fake: FakeBpy) -> MaterialGraphService:
    service = MaterialGraphService()
    service.capture(describe_material(fake), baseline_id="mg-acceptance")
    return service


def capture_rich() -> MaterialGraphService:
    """造一张覆盖全部支持节点的图并采集参数面。"""
    fake = FakeBpy()
    build_rich_material(fake)
    return capture(fake)


@contextlib.contextmanager
def running_env(
    tmp_path: Path, *, fake: FakeBpy | None = None
) -> Iterator[SimpleNamespace]:
    scene = fake if fake is not None else FakeBpy()
    if fake is None:
        build_rich_material(scene, MATERIAL_A)
    if scene.node_groups.get(CEL_GROUP) is None:
        scene.add_cel_group(CEL_GROUP, element_count=3, interpolation="LINEAR")
    injector = SurfaceInjector(scene)
    with FakeMCPServer(executor=injector) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            yield SimpleNamespace(
                client=client,
                app=app,
                fake=scene,
                injector=injector,
                tmp_path=tmp_path,
                unauthed_client=unauthed(app),
            )


def take_baseline(env: SimpleNamespace) -> dict[str, Any]:
    body = env.client.post("/api/session/baseline").json()
    assert body["ok"] is True
    wait_job(env.client, body["job_id"])
    env.injector.reset()
    return body


def schema_of(env: SimpleNamespace) -> dict[str, Any]:
    payload = env.client.get("/api/material-graph/schema").json()
    assert payload["ok"] is True
    return payload


def params_of(env: SimpleNamespace, material: str = MATERIAL_A) -> list[dict[str, Any]]:
    payload = schema_of(env)
    entry = next(item for item in payload["materials"] if item["material"] == material)
    return list(entry["parameters"])


def writable(params: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item for item in params if item["editable"] and item["kind"] == kind]


def node_value(fake: FakeBpy, material: str, node_type: str, socket: str = "Value") -> Any:
    entry = fake.data.materials.get(material)
    assert entry is not None and entry.node_tree is not None
    for node in entry.node_tree.nodes:
        if node.type == node_type:
            return node.inputs[socket].default_value
    return None


def submit(env: SimpleNamespace, draft: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body = {"material_draft": draft, **extra}
    response = env.client.post("/api/material-graph/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# -- A. 支持节点矩阵 -------------------------------------------------------


def test_supported_node_matrix_yields_expected_semantics() -> None:
    """Value / RGB / ColorRamp / Math / Map Range 逐个出现在参数面里，且语义与可达性对得上。"""
    service = capture_rich()
    material = service.schema_public()["materials"][0]
    params = material["parameters"]
    by_semantic: dict[str, list[dict[str, Any]]] = {}
    for param in params:
        by_semantic.setdefault(param["semantic"], []).append(param)

    # 每个语义都探到了，且至少有一个可写参数
    for semantic in (SEM_VALUE, "color", SEM_RAMP, SEM_MATH, SEM_MAP_RANGE):
        assert semantic in by_semantic, f"缺少语义 {semantic}：{sorted(by_semantic)}"
        assert any(item["editable"] for item in by_semantic[semantic]), semantic

    # Value / Math / Map Range 的 kind 都是标量，且分区落在「数值参数」
    for semantic in (SEM_VALUE, SEM_MATH, SEM_MAP_RANGE):
        for item in by_semantic[semantic]:
            if item["editable"]:
                assert item["kind"] == "float", item
                assert item["category"] == "numeric", item

    # 色带是单一复合节点（不是 2N 个独立 id），且色标数量不在参数面里
    ramp_params = [item for item in by_semantic[SEM_RAMP] if item["kind"] == "ramp"]
    assert len(ramp_params) == 1, "ColorRamp 必须是单一复合节点"
    ramp = ramp_params[0]
    assert ramp["elements"], "色带必须带色标"
    # `element_count` 与 `interpolation` 都是 `surface.schema` 的复合形状，
    # 不是裸标量 —— 前端与回归都按这个形状读。
    assert ramp["element_count"]["value"] == len(ramp["elements"])
    assert ramp["element_count"]["editable"] is False
    assert isinstance(ramp["interpolation"], dict)
    assert ramp["interpolation"]["value"]
    assert all("element_count" not in item["id"] for item in params)

    # RGB 是颜色 kind，可写，且有 4 个分量
    rgb_params = [item for item in by_semantic["color"] if item["editable"]]
    assert rgb_params
    assert rgb_params[0]["kind"] == "color"
    assert len(rgb_params[0]["value"]["baseline"]) == 4


def test_material_graph_ramp_draft_compiles_and_writes() -> None:
    """材质图色带的**写入**必须真的能跑通（不只是计划编译成功）。

    这条用例是为一个真实缺陷补的：`_literal` 只特判了 `COLOR_RAMP.elements`，
    材质图的色带被当成「复合取值里的 dict」拒绝，色带**根本写不进去**，
    而计划编译阶段的用例全绿。
    """
    from src.server.material_graph.execution import build_param_index
    from src.server.surface import executor as surface_executor

    fake = FakeBpy()
    build_rich_material(fake)
    service = capture(fake)
    baseline = service.require()
    assert baseline.param_index is not None
    ramp_param = next(
        param for param in baseline.param_index.params.values() if param.kind == "ramp"
    )
    elements = [
        {"position": element["position"], "color": element["color"]}
        for element in ramp_param.baseline_value["elements"]
    ]
    elements[0]["color"] = [0.25, 0.5, 0.75, 1.0]

    from src.server.material_graph.execution import validate_graph_draft

    ops = validate_graph_draft({ramp_param.param_id: elements}, baseline.param_index)
    payload = surface_executor.extract_json(
        run_generated_code(surface_executor.build_apply_code(ops), fake)
    )
    assert payload["applied"] is True, payload.get("failure")
    assert payload["failure"] is None
    entry = fake.data.materials.get(MATERIAL_A)
    assert entry is not None and entry.node_tree is not None
    ramp = next(node for node in entry.node_tree.nodes if node.type == "VALTORGB")
    written = list(ramp.color_ramp.elements)
    assert [round(float(item.color[0]), 4) for item in written][0] == 0.25


def test_isolated_nodes_stay_out_of_the_parameter_surface() -> None:
    """孤立节点不进默认参数面（只跟踪真正影响输出的链路）。"""
    fake = FakeBpy()
    build_rich_material(fake, orphan=True)
    service = capture(fake)
    material = service.schema_public()["materials"][0]
    traced = material["trace"]["nodes"]
    assert all(node["name"] != "Orphan" for node in traced), "孤立节点不该进入追踪结果"
    assert all("Orphan" not in (item["source_node"] or "") for item in material["parameters"])


def test_depth_limit_is_eight() -> None:
    """深度上限写死为 8；实际链路深度不得超过它，超深一律降级而不是猜。"""
    assert MAX_DEPTH == 8
    service = capture_rich()
    material = service.schema_public()["materials"][0]
    assert 0 < material["trace"]["max_depth"] <= MAX_DEPTH
    assert material["degraded_reason"] is None


# -- B. 请求注入矩阵 -------------------------------------------------------


def test_param_id_injection_matrix_is_rejected(tmp_path: Path) -> None:
    """参数 ID 只认 opaque 形态；代码 / 属性路径 / 命令 / 文件路径一律「未知参数」。"""
    hostile = [
        "__import__('os').system('echo pwned')",
        "eval(\"1+1\")",
        "exec('import os')",
        "bpy.data.materials['皮肤_脸'].node_tree",
        "Value/inputs['Value'].default_value",
        "../../etc/passwd",
        "C:\\Windows\\System32\\cmd.exe",
        "/usr/bin/python3 -c 'print(1)'",
        "材质_测试/Value/Value",
        "mg-0000;rm -rf /",
        "mg-0000\n__import__('os')",
        "mg-0000`whoami`",
        "$(whoami)",
        "%USERPROFILE%\\secret.txt",
    ]
    with running_env(tmp_path) as env:
        take_baseline(env)
        for payload in hostile:
            response = env.client.post(
                "/api/material-graph/preview", json={"material_draft": {payload: 1.0}}
            )
            assert response.status_code == 400, payload
            assert response.json()["error"]["code"] == "PARAM_INVALID", payload


def test_param_value_injection_matrix_is_rejected(tmp_path: Path) -> None:
    """取值同样只认类型与范围：字符串 / 表达式 / 路径都不会被当成数值写下去。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        params = params_of(env)
        float_param = writable(params, "float")[0]
        color_param = writable(params, "color")[0]

        hostile_values: list[Any] = [
            "__import__('os').system('echo pwned')",
            "0.5; import os",
            "C:\\Windows\\System32",
            "../../etc/passwd",
            None,
            True,
            [1.0, 2.0],
            {"value": 1.0},
        ]
        for value in hostile_values:
            response = env.client.post(
                "/api/material-graph/preview",
                json={"material_draft": {float_param["id"]: value}},
            )
            assert response.status_code == 400, value
            assert response.json()["error"]["code"] == "PARAM_INVALID", value

        # 颜色必须是 4 个分量；3 个 / 5 个都不行
        for value in (["a", "b", "c", "d"], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 1.0, 0.5]):
            response = env.client.post(
                "/api/material-graph/preview",
                json={"material_draft": {color_param["id"]: value}},
            )
            assert response.status_code == 400, value
            assert response.json()["error"]["code"] == "PARAM_INVALID", value


def test_request_cannot_carry_script_or_scheme_fields(tmp_path: Path) -> None:
    """请求体里根本没有「脚本路径 / 方案 / 命令」这些字段的位置（extra=forbid）。"""
    forbidden = [
        {"script": "C:\\evil.py"},
        {"script_path": "render_pipeline/开始渲染.py"},
        {"python": "import os"},
        {"command": "rm -rf /"},
        {"scheme": "volume_fog"},
        {"executor": "custom"},
    ]
    with running_env(tmp_path) as env:
        take_baseline(env)
        for extra in forbidden:
            response = env.client.post(
                "/api/material-graph/preview",
                json={"material_draft": {}, **extra},
            )
            assert response.status_code == 422, extra

            response = env.client.post(
                "/api/v4/preview",
                json={"draft": {}, **extra},
            )
            assert response.status_code == 422, extra


def test_writable_params_declare_no_range_yet(tmp_path: Path) -> None:
    """「参数越界拒绝」在接口层的现状：**没有可越的界**。

    材质图当前下发的可写标量**不带值域**（`_bounds` 只覆盖 mix / hue_saturation，
    而这两个语义恒为 inferred ⇒ 只读）。因此接口层能拒绝的只有类型与有限性，
    值域闸门由执行器单测（显式造一个带值域的参数）钉住。

    这条用例是**故意的**现状声明：后端一旦开始下发值域，它会红，
    提醒把接口层的越界断言补上。
    """
    with running_env(tmp_path) as env:
        take_baseline(env)
        floats = writable(params_of(env), "float")
        assert floats, "夹具应当至少有一个可写标量"
        assert all(item["minimum"] is None and item["maximum"] is None for item in floats)


def test_non_finite_and_non_numeric_values_are_rejected(tmp_path: Path) -> None:
    """可越的界没有，但类型闸门是真的：非数字一律拒绝，且不产生任务。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        for value in ("0.5", ["0.5"], {"v": 0.5}, None, True):
            response = env.client.post(
                "/api/material-graph/preview",
                json={"material_draft": {target["id"]: value}},
            )
            assert response.status_code == 400, value
            assert response.json()["error"]["code"] == "PARAM_INVALID", value


# -- C. 任务生命周期 -------------------------------------------------------


def test_successful_preview_renders_once_and_restores(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        before = node_value(env.fake, MATERIAL_A, "VALUE")
        before_renders = env.fake.render_count

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "done", job.get("error")
        assert env.fake.render_count - before_renders == 1, "材质图草稿只允许渲染一次"
        # 渲染用的是草稿值，结束后必须逐项写回并**回读确认**
        assert node_value(env.fake, MATERIAL_A, "VALUE") == before
        assert job["result"]["material_graph_restore_verified"] is True
        assert job["result"]["applied_material_graph"] == {target["id"]: 0.875}


def test_render_failure_restores_material_graph(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        before = node_value(env.fake, MATERIAL_A, "VALUE")
        env.injector.fail_render = True

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "PREVIEW_FAILED"
        restore = job["error"]["details"]["restore"]
        assert restore["verified"] is True
        assert restore["material_graph"]["verified"] is True
        assert restore["material_graph"]["attempted"] is True
        assert node_value(env.fake, MATERIAL_A, "VALUE") == before


def test_write_failure_reports_material_graph_error_code(tmp_path: Path) -> None:
    """材质图写入失败必须报材质图自己的错误码 —— 报 Cel 的会让人去错地方找原因。

    写入顺序：第 1 次是「恢复到基线」，第 2 次才是「应用草稿」。
    """
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        before = node_value(env.fake, MATERIAL_A, "VALUE")
        env.injector.fail_mg_write_at = 2

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "MATERIAL_GRAPH_APPLY_FAILED"
        assert "已被回滚" in job["error"]["message"]
        assert job["error"]["details"]["restored"] is True
        assert node_value(env.fake, MATERIAL_A, "VALUE") == before


def test_readback_mismatch_blocks_render_and_restores(tmp_path: Path) -> None:
    """回读不一致：中止本次预览（绝不渲染一张与草稿不符的图），并恢复到基线。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        before = node_value(env.fake, MATERIAL_A, "VALUE")
        before_renders = env.fake.render_count
        env.injector.mismatch_mg_readback_at = 2

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "APPLY_VERIFY_FAILED"
        assert env.fake.render_count == before_renders, "回读不一致时不该渲染"
        assert node_value(env.fake, MATERIAL_A, "VALUE") == before


def test_restore_failure_names_the_material_graph_segment(tmp_path: Path) -> None:
    """恢复失败必须直说没恢复成，并且**点名是哪一段**（L0 / Cel / 材质图）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = writable(params_of(env), "float")[0]
        env.injector.fail_render = True
        # 第 3 次材质图写入 = 渲染失败之后的「恢复基线」那一次
        env.injector.fail_mg_write_at = 3

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "failed"
        restore = job["error"]["details"]["restore"]
        assert restore["attempted"] is True
        assert restore["verified"] is False
        assert restore["code"] == "ROLLBACK_FAILED"
        assert restore["material_graph"]["code"] == "ROLLBACK_FAILED"
        assert restore["material_graph"]["error"], "必须带上回滚失败的原因"
        assert "ROLLBACK_FAILED" in job["error"]["message"]


def test_one_material_does_not_touch_another(tmp_path: Path) -> None:
    """改材质 A 的材质节点，绝不误改材质 B。"""
    fake = FakeBpy()
    build_rich_material(fake, MATERIAL_A)
    build_rich_material(fake, MATERIAL_B)
    with running_env(tmp_path, fake=fake) as env:
        take_baseline(env)
        target = writable(params_of(env, MATERIAL_A), "float")[0]
        before_b = node_value(env.fake, MATERIAL_B, "VALUE")

        job = wait_job(env.client, submit(env, {target["id"]: 0.875})["job_id"])
        assert job["status"] == "done", job.get("error")
        assert node_value(env.fake, MATERIAL_B, "VALUE") == before_b


def _shared_group_env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """两个材质共用一个节点组的场景（复用既有夹具，避免第二套构造）。"""
    from tests.test_material_graph_execution import build_shared_group_materials

    return running_env(tmp_path, fake=build_shared_group_materials())


def test_shared_parameter_requires_and_honours_confirmation(tmp_path: Path) -> None:
    """共享节点组：未确认拒绝（并列出受影响材质），确认后写进去。"""
    with _shared_group_env(tmp_path) as env:
        take_baseline(env)
        candidates = [
            item
            for item in params_of(env, MATERIAL_A)
            if item["shared"] and item["editable"] and item["in_group"]
        ]
        if not candidates:
            pytest.skip("桩未构造出可写的共享组参数")
        target = candidates[0]

        denied = env.client.post(
            "/api/material-graph/preview", json={"material_draft": {target["id"]: 0.5}}
        )
        assert denied.status_code == 409, denied.text
        error = denied.json()["error"]
        assert error["code"] == "SHARED_PARAMETER_CONFIRM_REQUIRED"
        assert error["details"]["shared_group"] == target["in_group"]
        assert set(error["details"]["affected_materials"]) == {MATERIAL_A, MATERIAL_B}

        allowed = env.client.post(
            "/api/material-graph/preview",
            json={
                "material_draft": {target["id"]: 0.5},
                "confirmed_shared_groups": [target["in_group"]],
            },
        )
        assert allowed.status_code == 200, allowed.text
        job = wait_job(env.client, allowed.json()["job_id"])
        assert job["status"] == "done", job.get("error")


def test_preview_requires_token(tmp_path: Path) -> None:
    """写接口一律要会话令牌（材质图这条路不例外）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.unauthed_client.post(
            "/api/material-graph/preview", json={"material_draft": {}}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"


# -- D. 保存完整材质图草稿 -------------------------------------------------


def test_commit_applies_full_material_graph_draft(tmp_path: Path) -> None:
    """保存闭环：材质图草稿真正落盘，且四态表里材质图那一段独立报出。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        params = params_of(env)
        # 挑 Value 节点上的那个标量：写得进去、也读得回来，断言最直白。
        target = next(
            item for item in writable(params, "float") if item["source_node"] == "Value"
        )
        ramp = next(
            (item for item in params if item["kind"] == "ramp" and item["editable"]),
            None,
        )
        draft: dict[str, Any] = {target["id"]: 0.625}
        if ramp is not None:
            draft[ramp["id"]] = {
                "elements": [
                    {"position": element["position"]["value"], "color": element["color"]["value"]}
                    for element in ramp["elements"]
                ],
                "interpolation": ramp["interpolation"]["value"],
            }
        material_hash = schema_of(env)["structure_hash"]
        target_path = str(tmp_path / "out.blend")

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": target_path,
                "confirm": True,
                "material_draft": draft,
                "expected_material_structure_hash": material_hash,
            },
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        # 参数计数按**写入计划的操作条数**算：色带是复合节点，除了色标表本身
        # 还会多写一条插值，因此它比草稿的键数多 1。
        expected_ops = len(draft) + (1 if ramp is not None else 0)
        assert body["material_graph_parameter_count"] == expected_ops

        committed = env.client.post(
            "/api/session/commit",
            json={
                "token": body["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": target_path,
                "material_draft": draft,
                "expected_material_structure_hash": material_hash,
            },
        )
        assert committed.status_code == 200, committed.text
        result = committed.json()
        assert result["saved"] is True
        status = result["status"]
        applied = status["applied_material_graph"]
        # 草稿里的每一个参数都在「实际写入」的清单里（色带会多一条插值操作）
        for param_id in draft:
            assert param_id in applied, param_id
        assert applied[target["id"]] == 0.625
        assert status["material_graph_readback_verified"] is True
        assert status["material_graph_mismatches"] == []
        # 写盘后工程里的材质节点就是草稿值
        assert node_value(env.fake, MATERIAL_A, "VALUE") == 0.625
        assert (tmp_path / "out.blend").is_file()


def test_commit_rejects_shared_change_without_confirmation(tmp_path: Path) -> None:
    """共享组未确认时，保存这条路同样拒绝（闸门只有一道，不在两条路上各写一份）。"""
    with _shared_group_env(tmp_path) as env:
        take_baseline(env)
        shared = next(
            (
                item
                for item in params_of(env, MATERIAL_A)
                if item["shared"] and item["editable"] and item["in_group"]
            ),
            None,
        )
        if shared is None:
            pytest.skip("桩未构造出可写的共享组参数")
        response = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
                "material_draft": {shared["id"]: 0.5},
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SHARED_PARAMETER_CONFIRM_REQUIRED"


def test_schema_only_marks_confirmed_levels_writable() -> None:
    """不得把推测参数显示成确定参数：可写的一定是 confirmed / user_confirmed。"""
    service = capture_rich()
    material = service.schema_public()["materials"][0]
    for param in material["parameters"]:
        if param["editable"]:
            assert param["confidence"] in (LEVEL_CONFIRMED, "user_confirmed"), param
        if param["confidence"] == LEVEL_INFERRED:
            assert param["editable"] is False, param
            assert param["category"] == "inferred", param
