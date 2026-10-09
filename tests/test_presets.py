"""预设（``/api/presets``）测试。

覆盖任务书点名的场景：新增、更新、非法名称、参数越界、依赖枚举非法、
路径穿越、原子写入失败；外加「接口不接受任何路径参数」这一结构性保证。

所有用例的预设目录都指向 ``tmp_path``，**绝不**碰开发者真实的
``%LOCALAPPDATA%\\Cartoon-Model-Shader\\presets``。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server import errors, presets as presets_module
from src.server.app import create_app
from src.server.presets import PresetStore, preset_id_for, preset_path, validate_name
from tests.fake_bpy import FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed, make_config, unauthed

VALID_DRAFT = {"color.exposure": 0.5, "color.gamma": 1.2, "glow.threshold": 1.5}
CHINESE_NAME = "夜战·高对比 预设"


@pytest.fixture()
def preset_env(tmp_path: Path):
    """已建立基线的调参台 + 隔离预设目录。"""
    fake = FakeBpy()
    directory = tmp_path / "presets"
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        app = create_app(make_config(server.port, presets_dir=directory))
        with authed(app) as client:
            client.post("/api/session/baseline")
            yield SimpleNamespace(client=client, app=app, fake=fake, directory=directory)


# -- 名称校验（单元）-------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        CHINESE_NAME,
        "preset 01",
        "夜间-1.2",
        "α・β",
        "a.b",
        "x" * 64,
    ],
)
def test_validate_name_accepts(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "a/b",
        "a\\b",
        "..",
        ".",
        "...",
        ".hidden",
        "trailing.",
        "a:b",
        "a*b",
        "a?b",
        'a"b',
        "a<b",
        "a|b",
        "a\x00b",
        "a\nb",
        "x" * 65,
        "CON",
        "nul.blend",
        "com1",
        "LPT9.txt",
        123,
        None,
        ["x"],
    ],
)
def test_validate_name_rejects(raw: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_name(raw)
    assert excinfo.value.code == errors.PRESET_NAME_INVALID


def test_preset_id_is_stable_and_path_free() -> None:
    assert preset_id_for(CHINESE_NAME) == preset_id_for(CHINESE_NAME)
    assert preset_id_for(CHINESE_NAME) != preset_id_for(CHINESE_NAME + "（改）")
    ident = preset_id_for(CHINESE_NAME)
    assert ident.isascii()
    assert "/" not in ident and "\\" not in ident and ".." not in ident


@pytest.mark.parametrize(
    "name",
    [CHINESE_NAME, "预设", "..", "a", "x" * 64, "CON"],
)
def test_preset_path_never_escapes_directory(tmp_path: Path, name: str) -> None:
    """文件名完全由服务端生成，任何名称都只能落在预设目录**直属**位置。"""
    path = preset_path(tmp_path, name)
    assert path.parent == tmp_path
    assert path.suffix == ".json"
    assert path.name.isascii()
    assert "/" not in path.name and "\\" not in path.name


# -- 列表 / 保存 -----------------------------------------------------------


def test_list_is_empty_before_any_save(preset_env) -> None:
    body = preset_env.client.get("/api/presets").json()
    assert body["ok"] is True
    assert body["presets"] == []


def test_save_preset_with_chinese_name(preset_env) -> None:
    response = preset_env.client.post(
        "/api/presets", json={"name": CHINESE_NAME, "draft": VALID_DRAFT}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["name"] == CHINESE_NAME
    assert body["parameter_count"] == len(VALID_DRAFT)
    assert body["updated"] is False

    files = list(preset_env.directory.glob("*.json"))
    assert len(files) == 1
    # 文件名是服务端生成的 ASCII 名，绝不等于（也不包含）中文名
    assert files[0].name.isascii()
    assert CHINESE_NAME not in files[0].name

    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["name"] == CHINESE_NAME
    assert stored["draft"] == VALID_DRAFT
    assert stored["schema"] == presets_module.PRESET_SCHEMA

    listing = preset_env.client.get("/api/presets").json()
    assert [item["name"] for item in listing["presets"]] == [CHINESE_NAME]
    assert listing["presets"][0]["parameter_count"] == len(VALID_DRAFT)


def test_save_same_name_updates_in_place(preset_env) -> None:
    first = preset_env.client.post(
        "/api/presets", json={"name": CHINESE_NAME, "draft": VALID_DRAFT}
    ).json()
    second = preset_env.client.post(
        "/api/presets",
        json={"name": CHINESE_NAME, "draft": {**VALID_DRAFT, "color.exposure": 1.75}},
    ).json()

    assert second["updated"] is True
    assert second["created_at"] == first["created_at"], "更新必须保留原始创建时间"
    names = [path.name for path in preset_env.directory.glob("*.json")]
    assert len(names) == 1, "同名保存是更新，不应堆出重复文件"

    stored = json.loads(
        (preset_env.directory / names[0]).read_text(encoding="utf-8")
    )
    assert stored["draft"]["color.exposure"] == pytest.approx(1.75)


def test_save_preset_stores_framing_and_baseline(preset_env) -> None:
    preset_env.client.post(
        "/api/presets",
        json={
            "name": CHINESE_NAME,
            "draft": VALID_DRAFT,
            "framing": {"mode": "auto_full_body", "margin": 0.2},
        },
    )
    file = next(preset_env.directory.glob("*.json"))
    stored = json.loads(file.read_text(encoding="utf-8"))
    assert stored["framing"] == {"mode": "auto_full_body", "margin": 0.2}
    assert stored["baseline_id"]
    assert stored["blender"] == "5.2.1 LTS"


def test_save_preset_requires_baseline(tmp_path: Path) -> None:
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            response = client.post(
                "/api/presets", json={"name": CHINESE_NAME, "draft": VALID_DRAFT}
            )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_BASELINE"


# -- 草稿校验 -------------------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        {"color.exposure": 999},
        {"color.gamma": 0.01},
        {"glow.size": 2.5},
        {"color.exposure": "0.5"},
        {"color.exposure": True},
        {"system.evil": 1},
        {"glow.type": "SuperNova"},
    ],
)
def test_save_preset_rejects_bad_draft(preset_env, draft: dict) -> None:
    response = preset_env.client.post("/api/presets", json={"name": CHINESE_NAME, "draft": draft})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "PARAM_INVALID"
    assert not list(preset_env.directory.glob("*.json")), "校验失败不得落盘"


def test_save_preset_rejects_invalid_dependent_enum(preset_env) -> None:
    """look 依赖 view_transform：不查能力表就写下去必然踩 enum not found。"""
    response = preset_env.client.post(
        "/api/presets",
        json={
            "name": CHINESE_NAME,
            "draft": {"color.view_transform": "Standard", "color.look": "AgX - Punchy"},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_DEPENDENT_ENUM"
    assert not list(preset_env.directory.glob("*.json"))


# -- 安全：不接受路径 / 令牌 ------------------------------------------------


def test_save_preset_rejects_path_fields(preset_env) -> None:
    """预设接口没有、也不接受任何路径字段：多一个字段就是 422。"""
    for payload in (
        {"name": CHINESE_NAME, "draft": VALID_DRAFT, "path": "C:/Windows/System32/x.json"},
        {"name": CHINESE_NAME, "draft": VALID_DRAFT, "dir": "../.."},
        {"name": CHINESE_NAME, "draft": VALID_DRAFT, "code": "print(1)"},
        {"name": CHINESE_NAME, "draft": VALID_DRAFT, "filename": "../../evil.json"},
    ):
        response = preset_env.client.post("/api/presets", json=payload)
        assert response.status_code == 422, payload
    assert not list(preset_env.directory.glob("*.json"))


def test_save_preset_rejects_traversal_name(preset_env) -> None:
    for name in ("../../evil", "..\\..\\evil", "sub/dir", "C:\\abs"):
        response = preset_env.client.post(
            "/api/presets", json={"name": name, "draft": VALID_DRAFT}
        )
        assert response.status_code == 400, name
        assert response.json()["error"]["code"] == "PRESET_NAME_INVALID"
    assert not list(preset_env.directory.glob("*.json"))


def test_save_preset_requires_token(preset_env) -> None:
    client = unauthed(preset_env.app)
    response = client.post("/api/presets", json={"name": CHINESE_NAME, "draft": VALID_DRAFT})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"
    assert not list(preset_env.directory.glob("*.json"))


def test_list_presets_does_not_require_token(preset_env) -> None:
    assert unauthed(preset_env.app).get("/api/presets").status_code == 200


# -- 原子写入 / 目录 -------------------------------------------------------


def test_atomic_write_failure_reports_and_leaves_no_partial_file(
    preset_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.replace`` 失败时必须报错，且目录里不留半截文件、不留临时文件。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(presets_module.os, "replace", boom)
    response = preset_env.client.post(
        "/api/presets", json={"name": CHINESE_NAME, "draft": VALID_DRAFT}
    )
    monkeypatch.undo()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PRESET_SAVE_FAILED"
    assert list(preset_env.directory.glob("*")) == [], "失败后不得留下任何文件（含临时文件）"


def test_store_skips_corrupt_files(preset_env) -> None:
    preset_env.directory.mkdir(parents=True, exist_ok=True)
    (preset_env.directory / "broken.json").write_text("{ not json", encoding="utf-8")
    (preset_env.directory / "notdict.json").write_text("[1,2,3]", encoding="utf-8")
    body = preset_env.client.get("/api/presets").json()
    assert body["presets"] == []
    assert PresetStore(preset_env.directory).list() == []


def test_store_survives_missing_directory(tmp_path: Path) -> None:
    store = PresetStore(tmp_path / "does-not-exist")
    assert store.list() == []
