"""渲染工作流测试（HTTP 编排层）。

覆盖：
* 状态机合法迁移 / 取消 / 重试判定；
* 模型注册表：opaque id、不泄漏真实路径；
* 适配器：只按 main 的真实接口调用固定脚本；退出码 0 但无产物 ⇒ 不算成功；
* API：写接口需令牌、不泄漏路径、任务去重、产物列举。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import errors, render_state
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, RenderConfig, ServerConfig
from src.server.models_scan import ModelRegistry, model_id_from_fingerprint
from src.server.render_jobs import RenderJobManager
from src.server.render_pipeline_adapter import RenderPipelineService, validate_settings
from tests.support import FakeBlenderGateway, authed, unauthed

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_OK = Path(__file__).resolve().parent / "fake_render_ok.py"
FAKE_NO_OUTPUT = Path(__file__).resolve().parent / "fake_render_no_output.py"
FAKE_FAILED_TOKEN = Path(__file__).resolve().parent / "fake_render_failed_token.py"
FAKE_SLOW = Path(__file__).resolve().parent / "fake_render_slow.py"
FAKE_ARGV = Path(__file__).resolve().parent / "fake_render_argv.py"


# -- 状态机 ---------------------------------------------------------------


def test_state_machine_transitions() -> None:
    assert render_state.can_transition("queued", "preflight")
    assert render_state.can_transition("analyzing_materials", "waiting_confirmation")
    assert render_state.can_transition("rendering", "verifying")
    assert render_state.can_transition("verifying", "success")
    # 非法
    assert not render_state.can_transition("queued", "rendering")
    assert not render_state.can_transition("success", "queued")


def test_state_helpers() -> None:
    assert render_state.is_terminal("success")
    assert render_state.is_terminal("failed")
    assert render_state.can_cancel("queued")
    assert render_state.can_cancel("waiting_confirmation")
    assert not render_state.can_cancel("rendering")
    assert render_state.is_retryable("failed")
    assert not render_state.is_retryable("success")
    # 6 步
    assert render_state.stage_info("waiting_confirmation")[0] == 2


# -- 模型注册表 -----------------------------------------------------------


def test_registry_opaque_ids_no_path_leak(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "a.pmx").write_bytes(b"pmx-a")
    (model_dir / "b.blend").write_bytes(b"blend-b")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")

    models = registry.scan()
    assert len(models) == 2
    for item in models:
        # 公开字段绝不含真实路径
        assert "path" not in item
        assert set(item) == {"model_id", "name", "fingerprint", "material_count"}
        assert item["model_id"].startswith("model-")

    # 可凭 id 解析回真实路径（服务端私有）
    first = models[0]
    path, fp, name = registry.resolve(first["model_id"])
    assert Path(path).is_file()
    assert fp.startswith(first["fingerprint"])
    assert name == first["name"]


def test_registry_missing_dir_returns_empty(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path / "nope", tmp_path / ".reg.json")
    assert registry.scan() == []


def test_model_id_stable(tmp_path: Path) -> None:
    p = tmp_path / "x.pmx"
    p.write_bytes(b"data")
    from src.server.models_scan import fingerprint_of

    fp = fingerprint_of(p)
    assert model_id_from_fingerprint(fp) == "model-" + fp[:16]


# -- 设置白名单 -----------------------------------------------------------


def test_validate_settings_rejects_extra_keys() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_settings({"mode": "faithful", "script": "/evil.py"})
    assert excinfo.value.code == errors.PARAM_INVALID


def test_validate_settings_rejects_bad_mode() -> None:
    with pytest.raises(errors.ToonTunerError):
        validate_settings({"mode": "hax"})


def test_validate_settings_ok() -> None:
    """PR #8：设置归一必须落到固定方案（默认 volume_fog），并镜像旧字段。"""
    assert validate_settings({"mode": "enhanced", "auto_frame": True}) == {
        "mode": "enhanced",
        "auto_frame": True,
        "scheme": "volume_fog",
        "scheme_version": 1,
        "scheme_parameters": {"fog_density": 0.018},
        "lighting": "volume_fog",
        "fog_density": 0.018,
    }


def test_validate_settings_lighting_and_glow_ok() -> None:
    """2026-10-11 体积雾灯光与辉光增量：合法值原样通过（辉光项可缺省）。

    2026-10-12 PR #8：旧的 ``lighting=legacy`` 翻译为 ``scheme=classic``；
    classic 方案不接受 fog_density 调参，故 scheme_parameters 为空，
    但旧字段仍按 deprecated 语义原样回填，并附带一条弃用提示。
    """
    out = validate_settings({
        "mode": "enhanced",
        "lighting": "legacy",
        "glow_threshold": 0.6,
        "glow_strength": 0.9,
        "glow_size": 0.7,
        "glow_smoothness": 0.4,
        "glow_quality": "Low",
    })
    assert out["mode"] == "enhanced"
    assert out["auto_frame"] is False
    assert out["scheme"] == "classic"
    assert out["scheme_version"] == 1
    # classic 无动态参数：雾密度不能被悄悄塞进方案参数
    assert out["scheme_parameters"] == {}
    # 旧字段原样保留（只标弃用，不删除）
    assert out["lighting"] == "legacy"
    assert out["fog_density"] == 0.018
    assert out["glow_threshold"] == 0.6
    assert out["glow_strength"] == 0.9
    assert out["glow_size"] == 0.7
    assert out["glow_smoothness"] == 0.4
    assert out["glow_quality"] == "Low"
    assert "弃用" in out["deprecation"]


def test_validate_settings_rejects_bad_lighting() -> None:
    """PR #8：非法旧字段值归一到方案层，报稳定码 RENDER_SCHEME_INVALID。"""
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_settings({"lighting": "neon"})
    assert excinfo.value.code == errors.RENDER_SCHEME_INVALID


def test_validate_settings_rejects_fog_density_out_of_range() -> None:
    """PR #8：雾密度越界由方案参数校验拦下，报 RENDER_SCHEME_PARAM_INVALID。"""
    for bad in (-0.01, 0.11):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            validate_settings({"fog_density": bad})
        assert excinfo.value.code == errors.RENDER_SCHEME_PARAM_INVALID


def test_validate_settings_rejects_bad_glow_quality() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_settings({"glow_quality": "Ultra"})
    assert excinfo.value.code == errors.PARAM_INVALID


def test_validate_settings_rejects_non_numeric_glow() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_settings({"glow_threshold": "bright"})
    assert excinfo.value.code == errors.PARAM_INVALID


# -- 适配器：调用固定脚本 -------------------------------------------------


def _make_service(tmp_path: Path, script: Path, model_dir: Path):
    (model_dir / "m.pmx").write_bytes(b"model")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=script,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
    )
    return manager, service, registry


def test_adapter_success_when_artifacts_present(tmp_path: Path) -> None:
    import asyncio

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_OK, model_dir)
    model_id = registry.to_public()[0]["model_id"]

    job, created = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})
    assert created is True
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    assert final.state == "success"
    assert final.list_artifacts(), "应至少产出一个产物文件"


def test_adapter_passes_lighting_and_glow_flags(tmp_path: Path) -> None:
    """体积雾灯光与辉光增量：适配器必须把设置透传成固定脚本的命令行开关。

    2026-10-12 PR #8：灯光部分改为固定方案 ``--scheme`` + ``--scheme-params``；
    旧的 ``--lighting`` / ``--fog-density`` 不再由适配器下发。
    """
    import asyncio
    import json

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_ARGV, model_dir)
    model_id = registry.to_public()[0]["model_id"]

    job, created = service.create_job(model_id, {
        "mode": "enhanced",
        "scheme": "volume_fog",
        "scheme_parameters": {"fog_density": 0.05},
        "glow_threshold": 0.6,
        "glow_quality": "Low",
    })
    assert created is True
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    assert final.state == "success"
    argv = json.loads((Path(final.output_dir) / "argv.json").read_text(encoding="utf-8"))
    assert argv["scheme"] == "volume_fog"
    assert json.loads(argv["scheme_params"])["fog_density"] == 0.05
    # 旧灯光开关已从命令链路移除，避免双写
    assert argv["lighting"] == "volume_fog"  # 替身默认值，未被适配器覆盖
    assert argv["glow_threshold"] == 0.6
    assert argv["glow_quality"] == "Low"
    # 未设置的辉光项不传（None），让主脚本用默认修复值。
    assert argv["glow_strength"] is None
    assert argv["glow_size"] is None
    assert "--lighting" not in argv


def test_adapter_passes_explicit_model_path(tmp_path: Path) -> None:
    """适配器必须显式传解析出的模型路径（--pmx），不能再依赖注册表指纹反查。

    Web 注册表指纹是「大小 + 前 1MB」哈希，入口脚本 locate() 按整文件 sha256
    反查 —— 两套口径不同，传 --fingerprint 会对任何模型都「找不到模型」。
    """
    import asyncio
    import json

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_ARGV, model_dir)
    entry = registry.to_public()[0]
    model_id = entry["model_id"]
    real_path, _fp, _name = registry.resolve(model_id)

    job, created = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})
    assert created is True
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    assert final.state == "success"
    argv = json.loads((Path(final.output_dir) / "argv.json").read_text(encoding="utf-8"))
    assert argv["pmx"] == str(real_path)
    assert argv["blend"] is None


def test_adapter_blend_model_uses_blend_flag(tmp_path: Path) -> None:
    """.blend 模型必须走 --blend（驱动 --pmx/--blend 二选一），且不再要求源工程。

    口径与 2026-10-14 凌晨 91.blend 真机三连跑一致：--blend <自身> --resolve <自身>。
    """
    import asyncio
    import json

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "scene.blend").write_bytes(b"BLENDER-scene-bytes")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=FAKE_ARGV,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
    )
    model_id = registry.to_public()[0]["model_id"]
    real_path, _fp, _name = registry.resolve(model_id)
    assert real_path.endswith("scene.blend")

    job, created = service.create_job(model_id, {"mode": "enhanced", "auto_frame": False})
    assert created is True
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    assert final.state == "success"
    argv = json.loads((Path(final.output_dir) / "argv.json").read_text(encoding="utf-8"))
    assert argv["blend"] == str(real_path)
    assert argv["pmx"] is None


def test_adapter_zero_exit_without_artifacts_is_failure(tmp_path: Path) -> None:
    import asyncio

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_NO_OUTPUT, model_dir)
    model_id = registry.to_public()[0]["model_id"]

    job, _created = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    # 退出码 0 但无产物 ⇒ 必须失败（规格 §11）
    assert final.state == "failed"


def test_adapter_render_done_failed_is_failure_despite_artifacts(tmp_path: Path) -> None:
    import asyncio

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_FAILED_TOKEN, model_dir)
    model_id = registry.to_public()[0]["model_id"]

    job, _created = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})
    asyncio.run(service.start(job.job_id))

    final = manager.get(job.job_id)
    assert final is not None
    # 退出码 0 + 目录里有图片，但 render.done=FAILED ⇒ 必须失败
    assert final.state == "failed"


def test_adapter_unknown_model_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _manager, service, _registry = _make_service(tmp_path, FAKE_OK, model_dir)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        service.create_job("model-doesnotexist", {"mode": "faithful"})
    assert excinfo.value.code == errors.MODEL_NOT_FOUND


def test_job_manager_dedupe_active(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    a, created_a = manager.create("j1", "model-a", "fp1", "m", {}, str(tmp_path / "o1"))
    b, created_b = manager.create("j2", "model-a", "fp1", "m", {}, str(tmp_path / "o2"))
    # 同指纹且仍有活跃任务 ⇒ 复用（created=False），不新建
    assert created_a is True
    assert created_b is False
    assert a.job_id == b.job_id == "j1"


def test_job_manager_cancel_and_retry(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    _ = manager.create("j1", "model-a", "fp1", "m", {}, str(tmp_path / "o"))
    manager.set_state("j1", "preflight")
    manager.set_state("j1", "analyzing_materials")
    cancelled = manager.cancel("j1")
    assert cancelled.state == "cancelled"
    # 取消后不可再取消
    with pytest.raises(ValueError):
        manager.cancel("j1")
    # 规格 §8：cancelled **不可**重试。
    with pytest.raises(ValueError):
        manager.retry("j1")

    # failed / rejected / diagnostic 才可重试。
    _ = manager.create("j2", "model-b", "fp2", "m2", {}, str(tmp_path / "o2"))
    manager.set_state("j2", "preflight")
    manager.fail("j2", "failed")
    retried = manager.retry("j2")
    assert retried.state == "queued"


def test_append_log_redacts(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "model-a", "fp1", "m", {}, str(tmp_path / "o"))
    manager.append_log("j1", "saved to C:\\Users\\someone\\secret\\file.blend")
    job = manager.get("j1")
    assert job is not None
    joined = "\n".join(job.logs)
    assert "C:\\Users\\someone" not in joined


# -- API ------------------------------------------------------------------


def _config(tmp_path: Path, script: Path, model_dir: Path) -> AppConfig:
    return AppConfig(
        blender_mcp=BlenderMCPConfig(host="127.0.0.1", port=1, connect_timeout_seconds=0.01, response_timeout_seconds=0.01),
        server=ServerConfig(host="127.0.0.1", port=8765),
        render=RenderConfig(
            model_root=model_dir,
            runtime_dir=tmp_path / "runtime",
            output_dir=tmp_path / "out",
            pipeline_script=script,
        ),
    )


def _render_app(tmp_path: Path, script: Path, model_dir: Path, *, gateway=None):
    """渲染工作流专用装配：注入**已连接**的假 MCP。

    规格 §5.4 起，创建渲染任务前会校验 Blender MCP 是否连接。测试里没有真
    Blender，因此统一经这里注入 ``FakeBlenderGateway``；需要验证「未连接被拒」
    的用例显式传 ``gateway=FakeBlenderGateway(connected=False)``。
    """
    return create_app(
        _config(tmp_path, script, model_dir),
        gateway=gateway or FakeBlenderGateway(),
    )


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/render/jobs/{job_id}").json()
        if body["is_terminal"]:
            return body
        time.sleep(0.1)
    raise AssertionError("任务未在超时内到达终态")


def test_api_render_workflow_end_to_end(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = _render_app(tmp_path, FAKE_OK, model_dir)

    # 必须进入上下文管理器，lifespan 才会启动常驻渲染 worker。
    with authed(app) as client:
        models = client.get("/api/render/models").json()
        assert models["ok"] is True
        assert len(models["models"]) == 1
        model_id = models["models"][0]["model_id"]
        # 前端拿不到真实路径
        assert "path" not in models["models"][0]

        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["job_id"]

        final = _wait_terminal(client, job_id)
        assert final["state"] == "success"
        assert final["artifacts"], "成功后应有产物"

        artifacts = client.get(f"/api/render/jobs/{job_id}/artifacts").json()
        assert artifacts["artifacts"]


def test_api_write_endpoints_require_token(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = _render_app(tmp_path, FAKE_OK, model_dir)
    client = unauthed(app)

    response = client.post(
        "/api/render/models/refresh", json={}
    )
    assert response.status_code == 401


def test_api_unknown_model_404(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, FAKE_OK, model_dir)
    client = authed(app)
    response = client.post(
        "/api/render/jobs", json={"model_id": "model-nope", "mode": "faithful", "auto_frame": False}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == errors.MODEL_NOT_FOUND


def test_api_rejects_extra_settings(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = _render_app(tmp_path, FAKE_OK, model_dir)
    client = authed(app)
    models = client.get("/api/render/models").json()
    model_id = models["models"][0]["model_id"]
    # 前端若试图夹带脚本路径，必须 422 拒绝
    response = client.post(
        "/api/render/jobs",
        json={"model_id": model_id, "mode": "faithful", "script": "/evil.py"},
    )
    assert response.status_code == 422


# -- P1：复用任务不得重复入队 ---------------------------------------------


def test_api_concurrent_same_model_reuses_active_job(tmp_path: Path) -> None:
    """同一模型并发下两次请求：第二次必须复用，且**只入队一次**。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = _render_app(tmp_path, FAKE_SLOW, model_dir)

    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        body = {"model_id": model_id, "mode": "faithful", "auto_frame": False}

        first = client.post("/api/render/jobs", json=body)
        second = client.post("/api/render/jobs", json=body)
        assert first.status_code == 200 and second.status_code == 200
        # 复用：两个响应必须是同一个 job_id
        assert first.json()["job_id"] == second.json()["job_id"]

        # 复用任务绝不重复入队：队列里要么空、要么只承载一次该任务。
        pipeline = app.state.pipeline
        queue = pipeline._queue
        assert queue is not None
        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        assert queued.count(first.json()["job_id"]) <= 1


def test_manager_reuse_returns_created_false(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    _a, created_a = manager.create("j1", "model-a", "fp1", "m", {}, str(tmp_path / "o1"))
    _b, created_b = manager.create("j2", "model-a", "fp1", "m", {}, str(tmp_path / "o2"))
    assert created_a is True and created_b is False


def test_reuse_does_not_create_extra_output_dir(tmp_path: Path) -> None:
    """复用活跃任务时不得再创建新的空输出目录。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _manager, service, registry = _make_service(tmp_path, FAKE_OK, model_dir)
    model_id = registry.to_public()[0]["model_id"]

    job1, created1 = service.create_job(model_id, {"mode": "faithful"})
    assert created1 is True
    dirs_after_first = sorted((tmp_path / "out").rglob("*"))
    count_after_first = sum(1 for p in dirs_after_first if p.is_dir())

    # 再次请求（活跃任务仍在）：复用，且**不应**产生新的输出目录
    job2, created2 = service.create_job(model_id, {"mode": "faithful"})
    assert created2 is False
    assert job2.job_id == job1.job_id

    count_after_second = sum(1 for p in (tmp_path / "out").rglob("*") if p.is_dir())
    assert count_after_second == count_after_first, "复用不应新增空目录"


# -- P2：任务 ID 唯一性 ---------------------------------------------------


def test_job_id_unique_across_creates(tmp_path: Path) -> None:
    """不同任务（不同模型/指纹）的 ID 必须互不相同。"""
    manager = RenderJobManager(tmp_path / "out")
    j1 = manager.create("rj-fp-1", "ma", "fpA", "m", {}, str(tmp_path / "oa"))[0]
    j2 = manager.create("rj-fp-2", "mb", "fpB", "m", {}, str(tmp_path / "ob"))[0]
    assert j1.job_id != j2.job_id


def test_output_dir_uses_unique_stamp(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_OK, model_dir)
    model_id = registry.to_public()[0]["model_id"]
    job1, _ = service.create_job(model_id, {"mode": "faithful"})
    # 让第一个任务进入终止态，从而允许同模型再次新建
    manager.fail(job1.job_id, "failed")
    job2, created2 = service.create_job(model_id, {"mode": "faithful"})
    assert created2 is True
    assert job1.output_dir != job2.output_dir


# -- P1：关闭时终止子进程 -------------------------------------------------


def test_shutdown_terminates_active_subprocess(tmp_path: Path) -> None:
    """服务关闭应先终止仍在渲染的子进程，再释放锁。

    注意：退出 lifespan 后 `active_procs()` 会被 `detach_proc` 清空，`all([])` 恒为
    True，属于**空集合假通过**。因此这里必须在 `with` 内**保存真实 proc 对象**，
    退出后对同一个对象断言 `returncode is not None`。
    """
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = _render_app(tmp_path, FAKE_SLOW, model_dir)

    saved: list = []
    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        # 等子进程起来
        deadline = time.time() + 10
        while time.time() < deadline and not app.state.jobs.active_procs():
            time.sleep(0.1)
        saved = list(app.state.jobs.active_procs())
        assert saved, "子进程应已启动"
        for proc in saved:
            assert proc.returncode is None, "启动时子进程应仍在运行"

    # 退出 with 触发 lifespan shutdown：之前保存的**同一个** proc 必须已退出。
    assert saved, "测试前提：必须持有真实 proc 句柄"
    for proc in saved:
        assert proc.returncode is not None, "关闭后子进程必须已被回收（returncode 非空）"


def test_shutdown_forced_kill_path(tmp_path: Path) -> None:
    """terminate 超时后必须 kill 并等待回收（覆盖强杀路径）。"""
    import asyncio

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    manager, service, registry = _make_service(tmp_path, FAKE_SLOW, model_dir)
    model_id = registry.to_public()[0]["model_id"]
    job, _ = service.create_job(model_id, {"mode": "faithful"})

    async def scenario() -> None:
        await service.start_worker()
        service.enqueue(job.job_id)
        # 等子进程启动
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not manager.active_procs():
            await asyncio.sleep(0.05)
        procs = manager.active_procs()
        assert procs, "子进程应已启动"
        # grace_seconds=0 强制走 kill 分支
        unresolved = await service.terminate_active_processes(grace_seconds=0.0)
        assert unresolved == [], "kill 后应能回收，不应留 unresolved"
        for proc in procs:
            assert proc.returncode is not None
        await service.stop_worker()

    asyncio.run(scenario())


# -- P1：产物路径边界 -----------------------------------------------------


def test_artifact_lookup_rejects_sibling_prefix_and_traversal(tmp_path: Path) -> None:
    """artifact_id 只能命中真实产物；sibling 前缀 / .. / 绝对路径一律拒绝。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    out_root = tmp_path / "out"
    # 造一个与输出目录同前缀的 sibling 目录与文件，旧 startswith 判断会被它绕过
    sibling = tmp_path / "out-evil"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "secret.png").write_bytes(b"secret")

    app = _render_app(tmp_path, FAKE_OK, model_dir)
    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        job_id = created.json()["job_id"]
        _wait_terminal(client, job_id)

        for bad in [
            "art-..%2F..%2Fout-evil%2Fsecret.png",
            "../out-evil/secret.png",
            "art-../../out-evil/secret.png",
            "/etc/passwd",
            "art-C:\\Windows\\win.ini",
        ]:
            response = client.get(f"/api/render/jobs/{job_id}/artifacts/{bad}")
            assert response.status_code in (404, 502), f"{bad} 不应被放行：{response.status_code}"

        # 真实产物仍可取
        arts = client.get(f"/api/render/jobs/{job_id}/artifacts").json()["artifacts"]
        assert arts
        ok = client.get(f"/api/render/jobs/{job_id}/artifacts/{arts[0]['artifact_id']}")
        assert ok.status_code == 200


# -- 语义：required/confirmed 只统计需人工确认的材质 ----------------------


def test_required_and_confirmed_counts_only_user_materials(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    _ = manager.create("j1", "model-a", "fp1", "m", {}, str(tmp_path / "o"))
    manager.set_materials(
        "j1",
        [
            {"material": "a", "requires_user": True, "confirmed": True},
            {"material": "b", "requires_user": True, "confirmed": False},
            {"material": "c", "requires_user": False, "confirmed": True},
        ],
    )
    job = manager.get("j1")
    assert job is not None
    # 只统计需人工确认的：共 2 项，已确认 1 项
    assert job.required_count == 2
    assert job.confirmed_count == 1
