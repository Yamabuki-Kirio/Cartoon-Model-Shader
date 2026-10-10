"""只读拓扑探针测试（v4 提交 1 第一步）。

钉住四件事：

1. **探到的就报告**：Cel 组的节点、ColorRamp 色标数量与逐色标位置/颜色都被读出；
2. **探不到就降级**：缺失的组标记 ``supported: false`` 并给出 reason，**不猜 socket 名**；
3. **脱敏**：工程路径类字段被丢弃、路径串被替换 —— 服务端兜底，不依赖 Blender 侧自觉；
4. **只读**：探针不写任何 ``bpy`` 数据（不改取值、不渲染、不改工程路径）。

技术方案 §1 决策 3 的判据也在这里固定：色标**数量**属结构信息，
**值**（位置/颜色）不参与结构签名。
"""

from __future__ import annotations

import json

import pytest

from src.server import surface_probe
from tests.fake_bpy import FakeBpy, run_generated_code

CEL_GROUPS = ("Cel_Skin", "Cel_Hair", "Cel_Cloth", "Cel_Dark", "Cel_Eyes")


def build_fake(*, groups: tuple[str, ...] = CEL_GROUPS, elements: int = 3) -> FakeBpy:
    fake = FakeBpy()
    for name in groups:
        fake.add_cel_group(name, element_count=elements, materials=4)
        fake.data.materials.add(__import__("tests.fake_bpy", fromlist=["FakeMaterial"]).FakeMaterial(name + "_mat"))
    return fake


def run_probe(fake: FakeBpy) -> dict:
    stdout = run_generated_code(surface_probe.build_describe_code(), fake)
    return surface_probe.parse_describe(stdout)


def describe(fake: FakeBpy) -> dict:
    return surface_probe.describe_groups(surface_probe.redact_describe(run_probe(fake)))


# -- 1. 探到的就报告 -------------------------------------------------------


def test_probe_reports_managed_groups_and_ramp_structure() -> None:
    fake = build_fake(elements=4)
    payload = run_probe(fake)

    managed = {item["name"]: item for item in payload["managed_groups"]}
    assert set(managed) == set(surface_probe.MANAGED_NODE_GROUPS)
    for name in CEL_GROUPS:
        assert managed[name]["exists"] is True
        assert managed[name]["node_count"] >= 1

    skin = managed["Cel_Skin"]
    ramps = [node for node in skin["nodes"] if node["color_ramp"] is not None]
    assert len(ramps) == 1
    ramp = ramps[0]["color_ramp"]
    assert ramp["element_count"] == 4
    assert ramp["interpolation"] == "LINEAR"
    assert len(ramp["elements"]) == 4
    # 逐色标的位置与 RGBA 都能读到（UI 的每个色标独立控件靠它）
    assert ramp["elements"][0]["position"] == pytest.approx(0.0)
    assert ramp["elements"][-1]["position"] == pytest.approx(1.0)
    assert len(ramp["elements"][0]["color"]) == 4


def test_probe_reports_emission_socket_only_when_present() -> None:
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=2, emission_strength=1.75)
    payload = run_probe(fake)
    skin = next(g for g in payload["managed_groups"] if g["name"] == "Cel_Skin")
    emission = [
        node
        for node in skin["nodes"]
        if any(sock["name"] == "Strength" for sock in node["inputs"])
    ]
    assert emission, "带 Emission 的组应能探到 Strength 插座"
    strength = next(s for s in emission[0]["inputs"] if s["name"] == "Strength")
    assert strength["value"] == pytest.approx(1.75)


# -- 2. 探不到就降级 -------------------------------------------------------


def test_probe_marks_missing_groups_unsupported_without_guessing() -> None:
    fake = FakeBpy()  # 一个 Cel 组都不建
    result = describe(fake)

    assert result["declared_groups"] == len(
        surface_probe.MANAGED_NODE_GROUPS + surface_probe.REFERENCE_NODE_GROUPS
    )
    assert result["found_groups"] == 0
    assert set(result["degraded"]) == set(
        surface_probe.MANAGED_NODE_GROUPS + surface_probe.REFERENCE_NODE_GROUPS
    )

    for group in result["groups"]:
        assert group["supported"] is False
        assert group["editable"] is False
        assert group["reason"], "降级必须给出原因，不能静默"
        assert group["ramp_count"] == 0, "没探到就不能声称有色带"


def test_reference_group_is_reported_but_never_editable() -> None:
    fake = FakeBpy()
    fake.add_cel_group("Sakura_Hair_Reference", element_count=2)
    result = describe(fake)
    reference = next(
        group for group in result["groups"] if group["name"] == "Sakura_Hair_Reference"
    )
    assert reference["exists"] is True
    assert reference["supported"] is True
    # 参考对象：存在、可探测，但按回退策略**不可编辑**
    assert reference["editable"] is False


# -- 3. 脱敏 ---------------------------------------------------------------


def test_server_side_redaction_drops_path_keys_and_paths() -> None:
    payload = {
        "schema": surface_probe.DESCRIBE_SCHEMA,
        "managed_groups": [],
        "filepath": "C:\\Users\\someone\\secret\\model.blend",
        "nested": {
            "texture_path": "C:/Users/someone/textures/skin.png",
            "说明": "工程位于 C:\\Users\\someone\\secret\\model.blend 里",
        },
        "images": [{"name": "skin.png", "path": "D:\\assets\\skin.png"}],
    }
    cleaned = surface_probe.redact_describe(payload)

    dumped = json.dumps(cleaned, ensure_ascii=False)
    assert "C:\\Users" not in dumped
    assert "C:/Users" not in dumped
    assert "D:\\assets" not in dumped
    assert "someone" not in dumped
    # 键名像路径的字段整体丢弃
    assert "filepath" not in cleaned
    assert "path" not in cleaned["images"][0]
    assert "texture_path" not in cleaned["nested"]
    # 非路径信息必须保留（脱敏不能把有用信息一起删掉）
    assert cleaned["images"][0]["name"] == "skin.png"
    assert cleaned["schema"] == surface_probe.DESCRIBE_SCHEMA


def test_probe_output_contains_no_local_paths_by_default() -> None:
    fake = build_fake()
    cleaned = surface_probe.redact_describe(run_probe(fake))
    dumped = json.dumps(cleaned, ensure_ascii=False)
    for needle in ("C:\\Users", "C:/Users", "AppData", ".blend"):
        assert needle not in dumped, f"探针输出不得包含 {needle}"


# -- 4. 只读 ---------------------------------------------------------------


def test_probe_is_read_only() -> None:
    fake = build_fake(elements=3)
    before_values = [
        [element.position for element in node.color_ramp.elements]
        for group in fake.node_groups.values()
        for node in group.nodes
        if node.color_ramp is not None
    ]
    before_filepath = fake.data.filepath
    before_ramp_colors = [
        list(element.color)
        for group in fake.node_groups.values()
        for node in group.nodes
        if node.color_ramp is not None
        for element in node.color_ramp.elements
    ]

    run_probe(fake)

    after_values = [
        [element.position for element in node.color_ramp.elements]
        for group in fake.node_groups.values()
        for node in group.nodes
        if node.color_ramp is not None
    ]
    after_ramp_colors = [
        list(element.color)
        for group in fake.node_groups.values()
        for node in group.nodes
        if node.color_ramp is not None
        for element in node.color_ramp.elements
    ]
    assert after_values == before_values
    assert after_ramp_colors == before_ramp_colors
    assert fake.data.filepath == before_filepath
    assert fake.render_count == 0, "探针绝不能触发渲染"


# -- 决策 3：数量是结构，值不是 -------------------------------------------


def _signature(fake: FakeBpy) -> list[str]:
    payload = run_probe(fake)
    skin = next(g for g in payload["managed_groups"] if g["name"] == "Cel_Skin")
    return list(skin["structure_signature"])


def test_structure_signature_ignores_value_changes() -> None:
    fake = build_fake(elements=3)
    before = _signature(fake)

    # 只改**值**：色标位置与颜色
    ramp = fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp
    ramp.elements[0].position = 0.25
    ramp.elements[0].color = (1.0, 0.0, 0.0, 1.0)

    assert _signature(fake) == before, "值变化不得进入结构签名"


def test_structure_signature_changes_when_element_count_changes() -> None:
    fake = build_fake(elements=3)
    before = _signature(fake)

    # 改**结构**：色标数量（决策 3）
    ramp = fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp
    ramp.elements.append(ramp.elements[-1])

    assert _signature(fake) != before, "色标数量变化必须被识别为结构变化"


def test_structure_signature_changes_when_node_added() -> None:
    from tests.fake_bpy import FakeNode

    fake = build_fake(elements=3)
    before = _signature(fake)
    fake.node_groups["Cel_Skin"].nodes._by_name["Extra"] = FakeNode("Extra", "MIX_RGB")
    assert _signature(fake) != before, "节点增删必须被识别为结构变化"


def test_parse_rejects_missing_marker() -> None:
    from src.server import errors

    with pytest.raises(errors.BlenderUnexpectedResponse):
        surface_probe.parse_describe("no marker here")


# -- 5. 接口（GET，只读、脱敏、不需要令牌）--------------------------------


def test_describe_endpoint_is_read_only_get_and_needs_no_token(tmp_path) -> None:
    from src.server.app import create_app
    from tests.fake_bpy import FakeBpy as _FakeBpy
    from tests.fake_mcp_server import FakeMCPServer
    from tests.support import make_config, unauthed

    fake = _FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3)
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        client = unauthed(app)  # 不带令牌：只读接口不应要求令牌
        response = client.get("/api/diagnostics/describe")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["compositor_group"] == surface_probe.COMPOSITOR_GROUP_NAME
    skin = next(group for group in body["groups"] if group["name"] == "Cel_Skin")
    assert skin["supported"] is True
    assert skin["ramp_element_counts"] == [3]
    assert "Cel_Hair" in body["degraded"], "未建基线前缺失的组必须如实降级"

    # 响应里不得出现任何路径类信息
    dumped = response.text
    for needle in ("C:\\Users", "C:/Users", "AppData", ".blend", "\\Users\\"):
        assert needle not in dumped, f"describe 响应不得包含 {needle}"


def test_describe_endpoint_rejects_post(tmp_path) -> None:
    """只读诊断只有 GET：POST 不应存在（避免变成写接口面）。"""
    from fastapi.testclient import TestClient

    from src.server.app import create_app
    from tests.fake_mcp_server import FakeMCPServer
    from tests.support import make_config

    with FakeMCPServer("ok") as server:
        client = TestClient(create_app(make_config(server.port, presets_dir=tmp_path / "p")))
        assert client.post("/api/diagnostics/describe").status_code == 405
