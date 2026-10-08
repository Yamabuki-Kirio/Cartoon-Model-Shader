"""只读探针测试：解析鲁棒性、归一化、排序截断、Unicode、只读性静态检查。"""

from __future__ import annotations

import json
import re

import pytest

from src.server import errors
from src.server.scene_probe import (
    MAX_ROLE_CANDIDATES,
    PROBE_CODE,
    PROBE_MARKER,
    PROBE_SCHEMA,
    normalize_probe,
    parse_probe_output,
)
from tests.fake_mcp_server import FAKE_PROBE_PAYLOAD


def wrap(payload: dict) -> str:
    return PROBE_MARKER + json.dumps(payload, ensure_ascii=False)


def test_parse_and_normalize_full_payload() -> None:
    summary = normalize_probe(parse_probe_output(wrap(FAKE_PROBE_PAYLOAD)))
    assert summary["protocol"] == PROBE_SCHEMA
    assert summary["blender"]["file_name"] == "model.blend"
    assert summary["scene"]["name"] == "Scene"
    assert summary["objects"]["mesh_count"] == 139
    assert summary["role_candidates"][0]["polygons"] == 35544


def test_parse_rejects_empty_output() -> None:
    with pytest.raises(errors.BlenderUnexpectedResponse):
        parse_probe_output("")


def test_parse_rejects_missing_marker() -> None:
    with pytest.raises(errors.BlenderUnexpectedResponse) as exc:
        parse_probe_output("ordinary print output")
    assert exc.value.code == errors.BLENDER_UNEXPECTED_RESPONSE


def test_parse_rejects_broken_json() -> None:
    with pytest.raises(errors.BlenderUnexpectedResponse):
        parse_probe_output(PROBE_MARKER + "{not valid json")


def test_normalize_tolerates_missing_fields() -> None:
    summary = normalize_probe({})
    assert summary["blender"]["version"] == "unknown"
    assert summary["scene"]["render_engine"] == "unknown"
    assert summary["objects"]["total"] == 0
    assert summary["role_candidates"] == []


def test_normalize_empty_scene() -> None:
    payload = {
        "protocol": PROBE_SCHEMA,
        "blender": {"version": "5.2.1", "file_path": "", "is_saved": False},
        "scene": {"name": "Scene", "render_engine": "BLENDER_EEVEE_NEXT", "resolution": [], "frame_current": 1, "camera": None},
        "objects": {"total": 0, "mesh_count": 0, "visible_mesh_count": 0, "light_count": 0, "camera_count": 0},
        "role_candidates": [],
    }
    summary = normalize_probe(payload)
    assert summary["blender"]["file_name"] is None
    assert summary["scene"]["camera"] is None
    assert summary["role_candidates"] == []
    assert summary["objects"]["total"] == 0


def test_candidates_sorted_desc_and_limited_to_ten() -> None:
    payload = dict(FAKE_PROBE_PAYLOAD)
    payload["role_candidates"] = [
        {"name": f"mesh_{i:02d}", "polygons": i * 100, "material_slots": 2, "visible": True, "hide_render": False}
        for i in range(15)
    ]
    summary = normalize_probe(payload)
    candidates = summary["role_candidates"]
    assert len(candidates) == MAX_ROLE_CANDIDATES
    polygons = [item["polygons"] for item in candidates]
    assert polygons == sorted(polygons, reverse=True)
    assert candidates[0]["polygons"] == 1400


def test_unicode_names_preserved() -> None:
    payload = dict(FAKE_PROBE_PAYLOAD)
    payload["scene"] = dict(payload["scene"], name="场景·中文", camera="相机")
    payload["role_candidates"] = [
        {"name": "模型_主体", "polygons": 100, "material_slots": 1, "visible": True, "hide_render": False}
    ]
    summary = normalize_probe(payload)
    assert summary["scene"]["name"] == "场景·中文"
    assert summary["scene"]["camera"] == "相机"
    assert summary["role_candidates"][0]["name"] == "模型_主体"


# -- 只读性静态检查（对应验收标准第 12 项）--------------------------------
def test_probe_prints_marker() -> None:
    assert PROBE_MARKER in PROBE_CODE
    assert "print(" in PROBE_CODE


def test_probe_code_has_no_write_operations() -> None:
    # 不得赋值任何 bpy 数据
    assert not re.search(r"^\s*(bpy|_bpy)\.[\w.]+\s*=", PROBE_CODE, re.MULTILINE)
    # 不得触碰操作符 / 保存 / 渲染 / 导入 / 追加资源
    for forbidden in (
        "bpy.ops",
        ".save",
        ".append(",
        "import_",
        "shutil",
        "subprocess",
        "render(",
        "open(",
        ".remove(",
        "wm.",
    ):
        assert forbidden not in PROBE_CODE, f"探针不应包含 {forbidden!r}"
