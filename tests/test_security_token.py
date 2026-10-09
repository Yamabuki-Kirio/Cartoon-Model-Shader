"""会话令牌与「不可注入」契约测试。

两件事在这里被钉死：

1. **所有写接口**（本文件里全部 POST）缺令牌 / 令牌错误一律 401 拒绝，
   且拒绝时**不产生任何副作用**（不建基线、不排队预览、不写文件）。
2. 请求里塞不进任意路径 / 命令 / Python：要么被模型 422 拒绝，要么被白名单 400 拒绝；
   路径字面量一律经 ``repr`` 量化，含引号的恶意路径也只会被当成**普通文件名**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.server import errors, project_ops
from src.server.app import create_app
from src.server.security import TOKEN_HEADER, generate_session_token
from tests.fake_bpy import FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed, make_config, unauthed

#: 全部写接口 + 一份「结构合法」的请求体（令牌缺失时不应走到业务校验）
WRITE_ENDPOINTS: list[tuple[str, dict[str, Any] | None]] = [
    ("/api/blender/reconnect", None),
    ("/api/session/baseline", None),
    ("/api/session/restore", None),
    ("/api/color/looks/refresh", None),
    ("/api/preview", {"draft": {}}),
    ("/api/presets", {"name": "临时预设", "draft": {}}),
    ("/api/session/commit/prepare", {"mode": "save_as", "draft": {}}),
    ("/api/session/commit", {"token": "x", "mode": "save_as", "draft": {}}),
]


@pytest.fixture()
def running(tmp_path: Path):
    fake = FakeBpy()
    codes: list[str] = []

    def executor(code: str) -> str:
        codes.append(code)
        return run_generated_code(code, fake)

    with FakeMCPServer(executor=executor) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            yield {"client": client, "app": app, "fake": fake, "server": server,
                   "codes": codes, "tmp_path": tmp_path}


def _post(client: TestClient, path: str, payload: dict[str, Any] | None):
    return client.post(path, json=payload) if payload is not None else client.post(path)


def _wait_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    """等任务到终态：后台预览任务会继续产生 Blender 调用，不等它就会把计数测成竞态。"""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{body}")


# -- 令牌 -------------------------------------------------------------------


@pytest.mark.parametrize("path,payload", WRITE_ENDPOINTS)
def test_write_endpoint_rejects_missing_token(running, path: str, payload: dict | None) -> None:
    response = _post(unauthed(running["app"]), path, payload)
    assert response.status_code == 401, f"{path}: {response.status_code} {response.text}"
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "SESSION_TOKEN_INVALID"
    assert body["error"]["details"]["reason"] == "missing"
    assert body["error"]["retryable"] is False


@pytest.mark.parametrize("path,payload", WRITE_ENDPOINTS)
def test_write_endpoint_rejects_wrong_token(running, path: str, payload: dict | None) -> None:
    client = unauthed(running["app"])
    response = client.post(
        path,
        json=payload,
        headers={TOKEN_HEADER: "definitely-not-the-token"},
    )
    assert response.status_code == 401, f"{path}: {response.status_code} {response.text}"
    assert response.json()["error"]["details"]["reason"] == "mismatch"


def test_rejected_requests_have_no_side_effects(running) -> None:
    """拒绝必须是「拒绝在动作之前」：不能建基线、不能排队预览、不能写文件。"""
    client = unauthed(running["app"])
    _post(client, "/api/session/baseline", None)
    _post(client, "/api/preview", {"draft": {"color.exposure": 1.0}})
    _post(client, "/api/presets", {"name": "不该出现", "draft": {"color.exposure": 1.0}})

    assert running["fake"].render_count == 0
    assert running["codes"] == [], "未通过令牌校验的请求不得下发任何 Blender 代码"
    assert list((running["tmp_path"] / "presets").glob("*")) == []


def test_valid_token_passes_the_gate(running) -> None:
    response = running["client"].post("/api/blender/reconnect")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_token_is_random_per_process() -> None:
    first = generate_session_token()
    second = generate_session_token()
    assert first != second
    assert len(first) >= 32


def test_each_app_instance_gets_its_own_token() -> None:
    with FakeMCPServer("ok") as server:
        app_a = create_app(make_config(server.port))
        app_b = create_app(make_config(server.port))
        assert app_a.state.session_token != app_b.state.session_token
        # A 的令牌在 B 上无效
        response = unauthed(app_b).post(
            "/api/blender/reconnect", headers={TOKEN_HEADER: app_a.state.session_token}
        )
        assert response.status_code == 401


def test_token_is_never_echoed_back(running) -> None:
    token = running["app"].state.session_token
    client = unauthed(running["app"])
    for path, payload in WRITE_ENDPOINTS:
        response = client.post(path, json=payload, headers={TOKEN_HEADER: "wrong"})
        assert token not in response.text, path
    # 只读响应里也不该出现令牌
    for path in ("/api/health", "/api/blender/status", "/api/params/schema", "/api/presets"):
        assert token not in client.get(path).text, path
    # OpenAPI 文档同样不得泄露
    assert token not in client.get("/openapi.json").text


def test_token_is_injected_only_into_runtime_response(running) -> None:
    token = running["app"].state.session_token
    html = unauthed(running["app"]).get("/").text
    assert f'window.__TOON_TUNER_TOKEN__ = "{token}"' in html

    source = Path("src/web/index.html").read_text(encoding="utf-8")
    assert token not in source
    assert "<!--TOON_TUNER_TOKEN-->" in source, "占位注释必须留在静态文件里"


# -- 不可注入 ---------------------------------------------------------------


def test_preset_endpoint_has_no_path_field(running) -> None:
    for extra in ("path", "dir", "filename", "target", "code", "python", "command"):
        response = running["client"].post(
            "/api/presets",
            json={"name": "x", "draft": {}, extra: "../../evil.json"},
        )
        assert response.status_code == 422, extra


def test_draft_string_cannot_inject_python(running) -> None:
    """草稿里的字符串只会被当作**枚举取值**比对，绝不会拼进 Blender 代码。

    注意断言的口径：非法取值**不得产生任何写入 / 渲染 / 保存调用**。
    预览提交会先读取一次取景上下文（只读闸门，用于「切帧后拒绝预览」），
    那一次只读调用是既有语义的一部分，不是注入面。
    """
    body = running["client"].post("/api/session/baseline").json()
    _wait_job(running["client"], body["job_id"])
    before = len(running["codes"])
    renders_before = running["fake"].render_count

    hostile = "a'); import os; os.system('calc'); ('"
    response = running["client"].post(
        "/api/preview", json={"draft": {"color.view_transform": hostile}}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PARAM_INVALID"

    new_codes = running["codes"][before:]
    writers = [
        code
        for code in new_codes
        if any(
            marker in code
            for marker in (
                "vs.exposure = ",
                "vs.gamma = ",
                "vs.look = ",
                "vs.view_transform = ",
                "_glare_set(",
                "save_as_mainfile",
                "save_mainfile",
                "bpy.ops.render.render",
            )
        )
    ]
    assert writers == [], "非法取值不得产生任何写入 / 渲染 / 保存调用"
    assert running["fake"].render_count == renders_before
    assert "os.system" not in "".join(new_codes)


def test_prepare_rejects_extra_code_fields(running) -> None:
    response = running["client"].post(
        "/api/session/commit/prepare",
        json={"mode": "save_as", "draft": {}, "code": "print(1)"},
    )
    assert response.status_code == 422


def test_save_code_serializes_hostile_path_as_plain_filename(tmp_path: Path) -> None:
    """路径经 ``repr`` 量化：带引号的路径只会被当作普通文件名，不构成注入。"""
    fake = FakeBpy()
    # 单引号在 Windows 文件名里合法，但足以把「裸字符串拼接」的实现打穿
    tricky = tmp_path / "it's tricky.blend"
    tricky.parent.mkdir(parents=True, exist_ok=True)

    code = project_ops.build_save_code(str(tricky), "save_as")
    stdout = run_generated_code(code, fake)
    payload = json.loads(stdout.split(project_ops.JSON_MARKER, 1)[1].splitlines()[0])

    assert payload["saved"] is True
    assert payload["path_after"] == str(tricky)
    assert tricky.is_file(), "路径里的引号只是普通字符"
    assert "os.system" not in code
    assert "eval(" not in code
    # 字面量必须是 repr 形式，而不是裸拼接
    assert repr(str(tricky)) in code


def test_save_code_rejects_unknown_mode() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        project_ops.build_save_code("C:/x.blend", "rm -rf /")
    assert excinfo.value.code == errors.SAVE_TARGET_INVALID
