from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"
EXAMPLE_MAP = PIPELINE / "model_material_maps" / "example.material-map.json"


def _source_files():
    for path in PIPELINE.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".py", ".md", ".json", ".html"}:
            yield path


def test_pipeline_contains_no_developer_machine_paths():
    forbidden = (
        re.compile(r"(?:A|B|Z):[\\/]", re.IGNORECASE),
        re.compile(r"C:[\\/]Users[\\/]", re.IGNORECASE),
    )
    hits = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in forbidden):
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, "发现开发机绝对路径：" + ", ".join(hits)


def test_pipeline_contains_no_generated_or_model_assets():
    forbidden_suffixes = {
        ".pmx", ".blend", ".blend1", ".png", ".jpg", ".jpeg", ".log", ".done"
    }
    hits = [
        str(path.relative_to(ROOT))
        for path in PIPELINE.rglob("*")
        if path.is_file() and path.suffix.lower() in forbidden_suffixes
    ]
    assert not hits, "发现不应入库的模型或生成物：" + ", ".join(hits)


def test_only_synthetic_material_map_is_present():
    maps = sorted((PIPELINE / "model_material_maps").glob("*.json"))
    assert [path.name for path in maps] == ["example.material-map.json"]
    text = maps[0].read_text(encoding="utf-8")
    assert "0" * 64 in text


# ---------------------------------------------------------------------------
# 示例映射的**字段语义**守卫。
#
# 背景：真实侧车映射（`_tools/confirm_server.py::save_model` 写出、
# `material_classifier.load_model_map` 读入）里两个字段分工是明确的：
#
#   group = 源工程实有的**节点组名**（`class_to_group` 的值，形如 ``Cel_Skin``）
#   class = **逻辑分类名**（`class_to_group` 的键，形如 ``skin``）
#
# 仓库里那份示例曾经把类名（``face_detail``）写进 ``group`` —— 示例是给人照抄的，
# 字段一漂移，照着填的映射就会在渲染侧被当成「未知节点组」而失效。
# 下面两条把它钉在 `material_rules.json` 的真实命名上。
# ---------------------------------------------------------------------------

def _load_example_json() -> dict:
    return json.loads(EXAMPLE_MAP.read_text(encoding="utf-8"))


def _class_to_group() -> dict:
    rules = json.loads((PIPELINE / "material_rules.json").read_text(encoding="utf-8"))
    return rules["class_to_group"]


def test_example_material_map_parses_with_pipeline_loader():
    """示例必须能被管线自己的加载器解析 —— 不是「看着像 JSON」而已。"""
    if str(PIPELINE) not in sys.path:
        sys.path.insert(0, str(PIPELINE))
    from material_classifier import load_model_map  # noqa: E402

    assignments, meta = load_model_map(explicit_path=str(EXAMPLE_MAP))
    assert assignments, "示例映射必须能解析出非空的 assignments"
    assert meta is not None
    assert meta.get("schema") == "toon-material-map/1"
    for name, entry in assignments.items():
        assert entry.get("group"), "材质 %s 缺少 group" % name
        assert entry.get("source") == "user_confirmed"


def test_example_material_map_group_and_class_follow_rules():
    """``group`` 必须是真实节点组名，``class`` 必须是逻辑分类名，且两者对应一致。"""
    mapping = _class_to_group()
    valid_groups = set(mapping.values())
    payload = _load_example_json()

    assert payload.get("schema") == "toon-material-map/1"
    assignments = payload.get("assignments") or {}
    assert assignments, "示例映射至少要有一条 assignment"

    for name, entry in assignments.items():
        klass = entry.get("class")
        group = entry.get("group")
        assert klass in mapping, "示例材质 %s 的 class=%r 不在 material_rules.json 里" % (name, klass)
        assert group in valid_groups, "示例材质 %s 的 group=%r 不是真实节点组名" % (name, group)
        assert group == mapping[klass], (
            "示例材质 %s 的 group 与 class 不匹配：class=%r 应映射到 %r，却写了 %r"
            % (name, klass, mapping[klass], group)
        )
        assert entry.get("source") == "user_confirmed"
        assert "示例" in name, "示例材质名必须自证是虚构的：%r" % name

