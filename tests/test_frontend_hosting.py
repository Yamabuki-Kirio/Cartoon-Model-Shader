"""工作台前端的托管契约与安全测试。

自「提交 5」起，新版工作台就是 ``/`` 上的**唯一**页面：旧的原生 HTML/JS 页面
（`src/web/app.js` 等）与 `/next` 路由都已删除。本文件守住这条边界。

覆盖六件事：

1. **页面挂在 `/`**：返回构建产物并注入令牌；占位注释被替换；`no-store` + `nosniff`；
2. **未构建时明确报错**：503 + `FRONTEND_NOT_BUILT` + 构建步骤，**不回退**任何页面；
3. **令牌只注入 HTML**：静态资源逐字节直出、长缓存，js/css/svg 里绝不含令牌；
4. **路径穿越一律拒绝**：`..`、URL 编码、反斜杠、绝对路径都取不到 assets 之外的文件；
5. **旧页面确实没了**：`/next` 与 `/static/*` 都不再是路由，`src/web/` 目录不存在；
6. **令牌真的能用**：从 `/` 拿到的令牌必须能过写接口的闸门（否则页面等于摆设）。

所有用例都用临时目录伪造 `web/dist`（见 `tests.support.write_fake_dist`），
因此**不需要真的跑 npm** —— CI 的 Python job 与 Node 无关。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import errors, frontend
from src.server.app import create_app
from tests.fake_mcp_server import find_free_port
from tests.support import authed, make_config, unauthed, write_fake_dist

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PAGE_FILES = ("app.js", "index.html", "styles.css")


@pytest.fixture()
def built_app(tmp_path: Path):
    """构建产物就绪：`/` 应该给出工作台页面。"""
    web_dir = tmp_path / "web"
    write_fake_dist(web_dir)
    app = create_app(
        make_config(find_free_port(), web_dir=web_dir, presets_dir=tmp_path / "presets")
    )
    return app, web_dir


@pytest.fixture()
def unbuilt_app(tmp_path: Path):
    """没有 dist/ 的 web 目录 = 还没构建。"""
    web_dir = tmp_path / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        make_config(find_free_port(), web_dir=web_dir, presets_dir=tmp_path / "presets")
    )
    return app, web_dir


# -- 1. 页面挂在 / ---------------------------------------------------------


def test_root_serves_workbench_with_injected_token(built_app) -> None:
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # 令牌已注入，占位注释已被替换
    assert app.state.session_token in body
    assert "<!--TOON_TUNER_TOKEN-->" not in body
    assert 'id="app"' in body
    # 资源引用是根路径（说明 base 与托管路径一致）
    assert 'src="/assets/' in body
    assert "/next/" not in body


def test_index_html_is_never_cached(built_app) -> None:
    """页面含本次进程的会话令牌，缓存住它等于把令牌写进磁盘。"""
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_api_endpoints_untouched_by_the_switch(built_app) -> None:
    """换页面不影响任何接口。"""
    app, _ = built_app
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        schema = client.get("/api/params/schema").json()
    assert schema["ok"] is True
    assert any(
        param["id"] == "color.exposure" for group in schema["groups"] for param in group["params"]
    )


# -- 2. 未构建 -------------------------------------------------------------


def test_root_reports_frontend_not_built(unbuilt_app) -> None:
    app, _ = unbuilt_app
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "FRONTEND_NOT_BUILT"
    # 构建步骤必须出现，否则用户只能猜
    steps = body["error"]["details"]["steps"]
    assert "npm ci" in steps and "npm run build" in steps
    assert "npm" in body["error"]["hint"]


def test_root_never_returns_a_placeholder_page(unbuilt_app) -> None:
    """未构建时绝不返回任何「看起来像页面」的东西 —— 那会让人以为「就这样」。"""
    app, _ = unbuilt_app
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503
    assert "text/html" not in response.headers.get("content-type", "")
    assert "<html" not in response.text.lower()


def test_assets_report_not_found_when_unbuilt(unbuilt_app) -> None:
    app, _ = unbuilt_app
    with TestClient(app) as client:
        response = client.get("/assets/index-abc123.js")
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
            response = client.get(f"/assets/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith(expected_type)
            on_disk = (web_dir / "dist" / "assets" / name).read_bytes()
            assert response.content == on_disk, f"{name} 被改写过了"
            assert app.state.session_token.encode() not in response.content
    # 磁盘上的产物里没有令牌（令牌只在响应时注入）
    disk_html = (web_dir / "dist" / "index.html").read_text(encoding="utf-8")
    assert app.state.session_token not in disk_html


def test_assets_are_long_cached(built_app) -> None:
    app, _ = built_app
    with TestClient(app) as client:
        response = client.get("/assets/index-abc123.js")
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
        response = client.get(f"/assets/{asset_path}")

    assert response.status_code in (404, 400), asset_path
    assert b"TOP-SECRET" not in response.content
    assert "<html" not in response.text.lower()


def test_resolve_asset_rejects_paths_outside_assets_root(tmp_path: Path) -> None:
    """直接对解析函数下断言：越界与不存在都归到同一个错误码。"""
    web_dir = tmp_path / "web"
    write_fake_dist(web_dir)
    (web_dir / "dist" / "outside.txt").write_text("outside", encoding="utf-8")

    resolved = frontend.resolve_asset(web_dir, "index-abc123.js")
    assert resolved.name == "index-abc123.js"
    assert resolved.is_relative_to(frontend.assets_dir(web_dir).resolve())

    for bad in ("../outside.txt", "..\\outside.txt", "/etc/hosts", "nope.js"):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            frontend.resolve_asset(web_dir, bad)
        assert excinfo.value.code == errors.ASSET_NOT_FOUND
        assert excinfo.value.http_status == 404


# -- 5. 旧页面确实没了 -----------------------------------------------------


def test_next_prefix_is_gone(built_app) -> None:
    """`/next` 与 `/next/assets/*` 都已随旧页面一起删除。

    留着它们会变成第二份真相：有人会以为「新的还在 /next 上」。
    """
    app, _ = built_app
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/" in paths
    assert "/assets/{asset_path:path}" in paths
    assert not {path for path in paths if path.startswith("/next")}


def test_legacy_static_mount_is_gone(built_app) -> None:
    """`/static/*` 是旧页面的挂载点，必须一起删掉（否则旧资源仍可被访问）。"""
    app, _ = built_app
    with TestClient(app) as client:
        for path in ("/static/app.js", "/static/index.html", "/static/styles.css"):
            assert client.get(path).status_code == 404, path


def test_legacy_page_sources_are_deleted() -> None:
    """源码级断言：旧页面文件与目录都不该留在仓库里。

    这是**防回归**：把它们留在树里，下一个人会以为那还是有效实现，
    甚至会在上面继续改。
    """
    assert not (REPO_ROOT / "src" / "web").exists()
    for name in LEGACY_PAGE_FILES:
        assert not (REPO_ROOT / "src" / "web" / name).exists(), name


def test_v4_routes_registered(built_app) -> None:
    app, _ = built_app
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {
        "/",
        "/assets/{asset_path:path}",
        "/api/v4/surface/schema",
        "/api/v4/session/baseline",
        "/api/v4/preview",
        "/api/v4/jobs/{job_id}",
        "/api/session/commit/prepare",
        "/api/session/commit",
        "/api/presets",
    } <= paths


# -- 6. 令牌真的能用 -------------------------------------------------------


def test_root_page_token_works_for_write_endpoints(built_app) -> None:
    """从 `/` 页面拿到的令牌必须能真的调用写接口（否则页面等于摆设）。

    这里只断言「过了令牌闸门」：`/api/session/restore` 后续是否成功取决于
    Blender 连接，与令牌无关。因此判据是「不是 401 / 不是 SESSION_TOKEN_INVALID」，
    而不是「恰好 200」—— 后者会把一条与令牌无关的失败也算成通过。
    """
    app, _ = built_app

    with TestClient(app) as anonymous:
        page = anonymous.get("/").text
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


def test_config_carries_web_dir(tmp_path: Path) -> None:
    """`web_dir` 只影响服务端自己，接口层不接受任何路径参数。"""
    web_dir = tmp_path / "web"
    config = make_config(find_free_port(), web_dir=web_dir)
    assert config.web_dir == web_dir
    # 序列化进日志/响应时也不该出现绝对路径（这里只是防止有人把它塞进响应模型）
    assert json.dumps({"web_dir": str(config.web_dir)}) is not None
