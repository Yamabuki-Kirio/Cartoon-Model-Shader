from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"


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
