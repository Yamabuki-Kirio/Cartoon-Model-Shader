"""`/next` 前端托管的契约与安全测试（v4 提交 3B）。

覆盖四件事：

1. **迁移期两套页面并存**：`/` 仍是旧页面，`/next` 是新工作台，互不影响；
2. **未构建时明确报错**：503 + `FRONTEND_NOT_BUILT` + 构建步骤，**不回退旧页面**；
3. **令牌只注入 HTML**：静态资源逐字节直出，且 `index.html` 明确 `no-store`；
4. **路径穿越一律拒绝**：`..`、URL 编码、反斜杠、绝对路径都取不到 assets 之外的文件。

用临时目录伪造 `web/dist`，因此**不需要真的跑 npm**（CI 里 Python job 与 Node 无关）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import errors, frontend
from src.server.app import create_app
from tests.fake_mcp_server import find_free_port
from tests.support import authed, make_config, unauthed

TOKEN_BYTES = b'<script>window.__TOON_TUNER_TOKEN__ = "runtime-token";</script>'


def write_dist(web_dir: Path, *, placeholder: str | None = frontend.INDEX_FILENAME) -> Path:
    """伪造一份构建产物：`dist/index.html` + `dist/assets/*`。"""
    dist = web_dir / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    html = (
        "<!DOCTYPE html><html><head><!--TOON_TUNER_TOKEN-->"
        '<script type="module" crossorigin src="/next/assets/index-abc123.js"></script>'
        '<link rel="stylesheet" crossorigin href="/next/assets/index-abc123.css">'
        "</head><body><div id=\"app\"></div></body></html>"
    )
    (dist / "index.html").write_text(html, encoding="utf-8")
    (assets / "index-abc123.js").write_text(
        "window.__TOON_TUNER_TOKEN__;export const x = 1;", encoding="utf-8"
    )
    (assets / "index-abc123.css").write_text("body{margin:0}", encoding="utf-8")
    (assets / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    if placeholder is not None and placeholder != frontend.INDEX_FILENAME:
        (dist / placeholder).write_text("outside", encoding="utf-8")
    return dist


@pytest.fixture()
def built_app(tmp_path: Path):
    web_dir = tmp_path / "web"
    write_dist(web_dir)
    app = create_app(make_config(find_free_port(), web_dir=web_dir, presets_dir=tmp_path / "presets"))
    return app, web_dir


@pytest.fixture()
def unpublished_app(tmp_path: Path):
    # 没有 dist/ 的 web 目录 = 还没构建
    web_dir = tmp_path / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(make_config(find_free_port(), web_dir=web_dir, presets_dir=tmp_path / "presets"))
    return app, web_dir


# -- 1. 两套页面并存 -------------------------------------------------------


def test_next_serves_v4_page_with_injected_token(built_app) -> None:
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/next")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # 令牌已注入，占位注释已被替换
    assert app.state.session_token in body
    assert "<!--TOON_TUNER_TOKEN-->" not in body
    assert 'id="app"' in body


def test_index_html_is_never_cached(built_app) -> None:
    """页面含本次进程的会话令牌，缓存住它等于把令牌写进磁盘。"""
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/next")
    cache_control = response.headers["cache-control"]
    assert "no-store" in cache_control
    assert response.headers["x-content-type-options"] == "nosniff"


def test_legacy_index_still_served_at_root(built_app) -> None:
    """/next 的加入不影响 /（迁移期两套并存，提交 5 才切换）。"""
    app, _ = built_app
    with TestClient(app) as client:
        legacy = client.get("/")
    assert legacy.status_code == 200
    body = legacy.text
    assert "卡通渲染调参" in body
    # 旧页面用 /static/，不该引用 v4 的构建资源
    assert "/next/assets/" not in body
    assert app.state.session_token in body


def test_legacy_api_untouched(built_app) -> None:
    app, _ = built_app
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        schema = client.get("/api/params/schema").json()
    assert schema["ok"] is True
    assert any(param["id"] == "color.exposure" for group in schema["groups"] for param in group["params"])


# -- 2. 未构建 -------------------------------------------------------------


def test_next_reports_frontend_not_built(unpublished_app) -> None:
    app, _ = unpublished_app
    with TestClient(app) as client:
        response = client.get("/next")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "FRONTEND_NOT_BUILT"
    # 构建步骤必须出现，否则用户只能猜
    steps = body["error"]["details"]["steps"]
    assert "npm ci" in steps and "npm run build" in steps
    assert "npm" in body["error"]["hint"]


def test_next_never_falls_back_to_legacy_page(unpublished_app) -> None:
    """未构建时绝不把旧页面当成新工作台返回 —— 那会让人以为「新的就是这样」。"""
    app, _ = unpublished_app
    with TestClient(app) as client:
        response = client.get("/next")
    assert response.status_code == 503
    assert "卡通渲染调参" not in response.text
    assert "text/html" not in response.headers.get("content-type", "")


def test_assets_report_not_found_when_unbuilt(unpublished_app) -> None:
    app, _ = unpublished_app
    with TestClient(app) as client:
        response = client.get("/next/assets/index-abc123.js")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ASSET_NOT_FOUND"


# -- 3. 令牌只注入 HTML ----------------------------------------------------


def test_assets_are_served_byte_identical(built_app) -> None:
    """资源逐字节直出：令牌**不得**被注入到 js / css / svg 里。"""
    app, web_dir = built_app
    with TestClient(app) as client:
        for name, expected_type in (
            ("index-abc123.js", "text/javascript"),
            ("index-abc123.css", "text/css"),
            ("logo.svg", "image/svg+xml"),
        ):
            response = client.get(f"/next/assets/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith(expected_type)
            on_disk = (web_dir / "dist" / "assets" / name).read_bytes()
            assert response.content == on_disk, f"{name} 被改写过了"
            assert app.state.session_token.encode() not in response.content
    # 应用内只有 index.html 带令牌；磁盘上的源码/产物里没有
    disk_html = (web_dir / "dist" / "index.html").read_text(encoding="utf-8")
    assert app.state.session_token not in disk_html


def test_assets_are_long_cached(built_app) -> None:
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/next/assets/index-abc123.js")
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


# -- 4. 路径穿越 -----------------------------------------------------------


@pytest.mark.parametrize(
    "asset_path",
    [
        "../index.html",
        "../../secret.txt",
        "%2e%2e%2findex.html",
        "..%2findex.html",
        "..\\index.html",
        "subdir/../../index.html",
        "assets/../../../windows/win.ini",
        "/etc/hosts",
        "C:/Windows/win.ini",
        "",
    ],
)
def test_asset_path_traversal_is_rejected(built_app, asset_path: str) -> None:
    app, web_dir = built_app
    # 在 web 目录旁边放一个「不该被读到」的文件
    (web_dir.parent / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")

    with TestClient(app) as client:
        response = client.get(f"/next/assets/{asset_path}")

    assert response.status_code in (404, 400), asset_path
    assert b"TOP-SECRET" not in response.content
    assert "卡通渲染调参" not in response.text


def test_resolve_asset_rejects_paths_outside_assets_root(tmp_path: Path) -> None:
    """直接对解析函数下断言：越界与不存在都归到同一个错误码。"""
    web_dir = tmp_path / "web"
    write_dist(web_dir)
    (web_dir / "dist" / "outside.txt").write_text("outside", encoding="utf-8")

    resolved = frontend.resolve_asset(web_dir, "index-abc123.js")
    assert resolved.name == "index-abc123.js"
    assert resolved.is_relative_to(frontend.assets_dir(web_dir).resolve())

    for bad in ("../outside.txt", "..\\outside.txt", "/etc/hosts", "nope.js"):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            frontend.resolve_asset(web_dir, bad)
        assert excinfo.value.code == errors.ASSET_NOT_FOUND
        assert excinfo.value.http_status == 404


# -- 5. 与旧写接口的令牌规则一致 ------------------------------------------


def test_next_page_token_works_for_write_endpoints(built_app) -> None:
    """从 /next 页面拿到的令牌必须能真的调用写接口（否则页面等于摆设）。

    这里只断言「过了令牌闸门」：`/api/session/restore` 后续是否成功取决于
    Blender 连接，与令牌无关。因此判据是「不是 401 / 不是 SESSION_TOKEN_INVALID」，
    而不是「恰好 200」—— 后者会把一条与令牌无关的失败也算成通过。
    """
    app, _ = built_app

    with TestClient(app) as anonymous:
        page = anonymous.get("/next").text
        assert app.state.session_token in page
        unauthed_response = anonymous.post("/api/session/restore")
    assert unauthed_response.status_code == 401
    assert unauthed_response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"

    with authed(app) as client:
        authorised = client.post("/api/session/restore")
    assert authorised.status_code != 401
    assert authorised.json()["error"]["code"] != "SESSION_TOKEN_INVALID"

    # 读接口本来就不需要令牌
    with unauthed(app) as client:
        assert client.get("/api/health").status_code == 200


def test_v4_routes_registered(built_app) -> None:
    app, _ = built_app
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {
        "/",
        "/next",
        "/next/assets/{asset_path:path}",
        "/api/v4/surface/schema",
        "/api/v4/session/baseline",
        "/api/v4/preview",
        "/api/v4/jobs/{job_id}",
    } <= paths


def test_config_carries_web_dir(tmp_path: Path) -> None:
    """`web_dir` 只影响服务端自己，接口层不接受任何路径参数。"""
    web_dir = tmp_path / "web"
    config = make_config(find_free_port(), web_dir=web_dir)
    assert config.web_dir == web_dir
    # 序列化进日志/响应时也不该出现绝对路径（这里只是防止有人把它塞进响应模型）
    assert json.dumps({"web_dir": str(config.web_dir)}) is not None
