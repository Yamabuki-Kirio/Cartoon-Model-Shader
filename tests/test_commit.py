"""「应用到工程」测试：准备 / 确认令牌 / 应用 / 回读 / 备份 / 保存。

覆盖任务书点名的场景：

* 无基线、基线失效、非法目标、非 ``.blend`` 文件、未确认覆盖、未保存工程覆盖；
* 确认令牌过期、复用、篡改、与当前草稿不匹配；
* 覆盖前成功备份；备份失败时禁止覆盖；
* 应用或回读不一致时不得保存、也不得报告成功；
* 写接口缺少正确会话令牌时拒绝执行；
* 请求无法注入任意路径 / 命令 / Python 代码。

所有用例都跑**真实生成的服务端代码**（``bpy`` 换成可执行桩），因此「应用到工程」
到底有没有真的写盘、写到哪个路径，都是可验证的事实而不是自述。
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from src.server import project_ops
from src.server.app import create_app
from src.server.blender_ops import JSON_MARKER
from tests.fake_bpy import BLEND_BYTES, FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed, make_config, unauthed

OLD_CONTENT = b"OLD-BLEND-ON-DISK"
DRAFT = {"color.exposure": 0.4, "glow.threshold": 1.6}


class ApplyInjector:
    """在「应用整份草稿」这类载荷上做**定向**注入，用 ``arm()`` 控制何时生效。

    为什么需要 `arm()`：基线会紧接着产出首张预览任务，它同样会执行若干次写入。
    如果注入一上来就生效，命中的可能是那个后台任务而不是我们要观察的提交 ——
    测出来的就成了竞态。``arm()`` 会把计数清零，于是「第 1 次 / 第 2 次」指的就是
    **本次提交**的应用与否决后的回滚。

    两种注入模式：

    * ``corrupt_readback``：只让**提交那一次**的回读值偏掉 —— 模拟「写入后回读不一致」；
    * ``fail_write``：让**回滚那一次**的写入失败 —— 模拟「回滚失败」。
    """

    MODE_CORRUPT_READBACK = "corrupt_readback"
    MODE_FAIL_WRITE = "fail_write"

    def __init__(self, fake: FakeBpy, *, delta: float = 0.5) -> None:
        self.fake = fake
        self.delta = delta
        self.mode: str | None = None
        self.applies = 0
        self.codes: list[str] = []

    def arm(self, mode: str) -> None:
        self.mode = mode
        self.applies = 0

    def __call__(self, code: str) -> str:
        self.codes.append(code)
        if "'applied'" in code and "vs.exposure = " in code:
            self.applies += 1
            if self.mode == self.MODE_CORRUPT_READBACK and self.applies == 1:
                stdout = run_generated_code(code, self.fake)
                payload = json.loads(stdout.split(JSON_MARKER, 1)[1].splitlines()[0])
                payload["view"]["exposure"] = float(payload["view"]["exposure"]) + self.delta
                return JSON_MARKER + json.dumps(payload, ensure_ascii=False)
            if self.mode == self.MODE_FAIL_WRITE and self.applies >= 2:
                return JSON_MARKER + json.dumps(
                    {
                        "applied": False,
                        "failure": {
                            "kind": "write_failed",
                            "stage": "exposure",
                            "type": "RuntimeError",
                            "message": "注入的回滚写入失败",
                        },
                        "snapshot": {},
                        "restored_to": {},
                        "restore_ok": False,
                        "view": None,
                        "glare": None,
                    },
                    ensure_ascii=False,
                )
        return run_generated_code(code, self.fake)


class FrozenClock:
    """每次 ``now()`` 都往后跳 1 小时。

    若 commit 阶段**重新计算**备份路径，算出来的时间戳必然与 prepare 阶段不同 ——
    这正是「跨时间边界后实际备份路径与用户确认的不一致」那个缺陷的判定条件。
    """

    def __init__(self, base: datetime) -> None:
        self._base = base
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self._base + timedelta(hours=self.calls)


@contextlib.contextmanager
def running_env(
    tmp_path: Path,
    *,
    with_injector: bool = False,
    dirty: bool = False,
) -> Iterator[SimpleNamespace]:
    """跑起来的调参台 + 一个「已经保存过的」工程。"""
    fake = FakeBpy()
    project = fake.set_project(tmp_path / "proj" / "demo.blend", dirty=dirty)
    injector = ApplyInjector(fake) if with_injector else None
    run = injector if injector is not None else (lambda code: run_generated_code(code, fake))
    with FakeMCPServer(executor=run) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            yield SimpleNamespace(
                client=client,
                app=app,
                fake=fake,
                project=project,
                tmp_path=tmp_path,
                server=server,
                injector=injector,
            )


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    with running_env(tmp_path) as running:
        body = running.client.post("/api/session/baseline").json()
        # 必须等基线任务跑完：它会以「恢复基线」收尾，若与测试正文并发，
        # 场景取值会在断言前被回滚，测出来的就成了竞态而不是提交结果。
        _wait_job(running.client, body["job_id"])
        yield running


def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{body}")


def _prepare(client, **payload: Any):
    body = {"mode": "save_as", "draft": DRAFT, **payload}
    return client.post("/api/session/commit/prepare", json=body)


def _commit(client, token: str, **payload: Any):
    body = {"token": token, "mode": "save_as", "draft": DRAFT, **payload}
    return client.post("/api/session/commit", json=body)


def _target(env: SimpleNamespace, name: str = "saved.blend") -> Path:
    directory = env.tmp_path / "out"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


# -- 另存为：正常路径 -------------------------------------------------------


def test_prepare_requires_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        response = _prepare(env.client, target_path=str(_target(env)))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_BASELINE"


def test_save_as_happy_path(env: SimpleNamespace) -> None:
    target = _target(env)
    before = env.client.get("/api/session/baseline").json()["baseline_id"]

    prepared = _prepare(env.client, target_path=str(target))
    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    assert body["ok"] is True
    assert body["mode"] == "save_as"
    assert body["target_path"] == str(target)
    assert body["backup_path"] is None, "目标不存在时无需备份"
    assert body["token"]
    assert body["draft_hash"]

    applied = _commit(env.client, body["token"], target_path=str(target))
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["saved"] is True
    assert result["status"]["applied"] is True
    assert result["status"]["readback_verified"] is True
    assert result["status"]["mismatches"] == []
    assert result["status"]["saved"] is True
    assert result["status"]["backup"]["required"] is False
    assert result["status"]["backup"]["created"] is False
    assert result["status"]["backup"]["path"] is None
    # 保存成功不得触发回滚
    assert result["status"]["rollback"]["attempted"] is False
    assert result["status"]["rollback"]["code"] is None
    assert "应用完整草稿" in result["steps"]
    assert "回读校验" in result["steps"]
    assert "保存工程" in result["steps"]

    # 真的写盘了，且写到了目标路径
    assert target.is_file()
    assert target.read_bytes() == BLEND_BYTES
    assert env.fake.data.filepath == str(target)
    assert env.fake.save_calls[-1]["kind"] == "save_as"
    # check_existing 必须为 False，否则会在 Blender UI 上弹覆盖确认框卡住会话
    assert env.fake.save_calls[-1]["check_existing"] is False

    # 场景确实被改成了草稿值（完整草稿，不只是被改的那一项）
    assert env.fake.view_settings.exposure == pytest.approx(0.4)
    assert env.fake.glare_node.inputs.get("Threshold").default_value == pytest.approx(1.6)  # type: ignore[union-attr]

    # 工程内容已变 → 基线必须重新采集，否则后续预览会恢复到旧基线
    assert result["baseline_refreshed"] is True
    assert result["baseline_id"] != before


def test_commit_applies_entire_draft_not_only_changes(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()
    result = _commit(env.client, prepared["token"], target_path=str(target)).json()

    applied = result["status"]["applied_values"]
    # 草稿只给了 2 项，但「完整草稿」= 基线 + 草稿，10 项都必须落到 Blender
    assert len(applied) == 10
    assert applied["color.exposure"] == pytest.approx(0.4)
    assert applied["color.gamma"] == pytest.approx(1.0)
    assert applied["glow.type"] == "Bloom"


def test_save_as_over_existing_file_requires_confirmation(env: SimpleNamespace) -> None:
    target = _target(env)
    target.write_bytes(OLD_CONTENT)

    refused = _prepare(env.client, target_path=str(target))
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "SAVE_CONFIRM_REQUIRED"
    assert error["details"]["confirm_required"] is True
    # 必须把绝对路径与预计备份路径摊开给用户看
    assert error["details"]["target_path"] == str(target)
    assert error["details"]["backup_path"].startswith(str(target.with_suffix("")))
    assert error["details"]["backup_path"].endswith(".blend")
    assert target.read_bytes() == OLD_CONTENT, "未确认时不得动原文件"

    prepared = _prepare(env.client, target_path=str(target), confirm=True).json()
    assert prepared["backup_path"]
    result = _commit(env.client, prepared["token"], target_path=str(target)).json()
    assert result["saved"] is True

    backup = Path(prepared["backup_path"])
    assert backup.is_file(), "覆盖前必须生成备份"
    assert backup.read_bytes() == OLD_CONTENT, "备份内容必须是覆盖前的版本"
    assert backup.parent == target.parent, "备份必须与原文件同目录"
    assert target.read_bytes() == BLEND_BYTES


# -- 目标路径校验 -----------------------------------------------------------


def test_save_as_rejects_bad_targets(env: SimpleNamespace, tmp_path: Path) -> None:
    cases = {
        "相对路径": "saved.blend",
        "非 blend 扩展名": str(_target(env, "saved.txt")),
        "没有扩展名": str(_target(env, "saved")),
        "目标是目录": str(_target(env).parent),
        "目录不存在": str(tmp_path / "missing-dir" / "x.blend"),
        "空路径": "",
        "只给目录": str(_target(env).parent) + "\\",
    }
    for label, value in cases.items():
        response = _prepare(env.client, target_path=value)
        assert response.status_code == 400, f"{label}: {response.text}"
        assert response.json()["error"]["code"] == "SAVE_TARGET_INVALID", label


def test_prepare_rejects_injected_code_or_extra_fields(env: SimpleNamespace) -> None:
    """请求体里塞代码 / 额外字段一律 422，模型里根本没有这些字段。"""
    for extra in (
        {"code": "import os; os.remove('x')"},
        {"python": "print(1)"},
        {"command": "del /f /q *"},
        {"draft_path": "C:/Windows"},
    ):
        response = env.client.post(
            "/api/session/commit/prepare",
            json={"mode": "save_as", "draft": DRAFT, "target_path": str(_target(env)), **extra},
        )
        assert response.status_code == 422, extra


# -- 覆盖当前工程 -----------------------------------------------------------


def test_overwrite_uses_blender_path_and_needs_confirmation(env: SimpleNamespace) -> None:
    env.project.write_bytes(OLD_CONTENT)

    refused = _prepare(env.client, mode="overwrite", target_path=None)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "SAVE_CONFIRM_REQUIRED"
    assert error["details"]["target_path"] == str(env.project)
    assert error["details"]["warnings"], "覆盖必须给出明确警告"

    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
    result = _commit(
        env.client, prepared["token"], mode="overwrite", target_path=None
    ).json()
    assert result["saved"] is True
    assert result["target_path"] == str(env.project)
    assert env.fake.save_calls[-1]["kind"] == "save_mainfile"
    assert env.project.read_bytes() == BLEND_BYTES
    assert Path(prepared["backup_path"]).read_bytes() == OLD_CONTENT


def test_overwrite_rejects_client_supplied_target(env: SimpleNamespace) -> None:
    response = _prepare(
        env.client, mode="overwrite", target_path=str(_target(env)), confirm=True
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SAVE_TARGET_INVALID"


def test_overwrite_rejects_unsaved_project(env: SimpleNamespace) -> None:
    env.fake.data.filepath = ""
    response = _prepare(env.client, mode="overwrite", target_path=None, confirm=True)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_NOT_SAVED"


def test_overwrite_warns_about_unsaved_changes(tmp_path: Path) -> None:
    """工程带未保存改动时必须明确警告：备份只含磁盘上「上一次保存的版本」。"""
    with running_env(tmp_path, dirty=True) as env:
        env.client.post("/api/session/baseline")
        env.project.write_bytes(OLD_CONTENT)

        refused = _prepare(env.client, mode="overwrite", target_path=None)
        details = refused.json()["error"]["details"]
        joined = " ".join(details["warnings"])
        assert "未保存" in joined
        assert "上一次保存" in joined

        prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
        assert "未保存" in " ".join(prepared["warnings"])
        result = _commit(
            env.client, prepared["token"], mode="overwrite", target_path=None
        ).json()
        # 备份只能备份到磁盘上的旧版本
        assert Path(result["backup_path"]).read_bytes() == OLD_CONTENT


def test_save_as_rejects_non_blend_current_project(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        env.client.post("/api/session/baseline")
        env.fake.data.filepath = str(tmp_path / "weird.txt")
        response = _prepare(env.client, mode="overwrite", target_path=None, confirm=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SAVE_TARGET_INVALID"


# -- 确认令牌 ---------------------------------------------------------------


def test_commit_token_missing_or_unknown(env: SimpleNamespace) -> None:
    response = _commit(env.client, "deadbeef", target_path=str(_target(env)))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMIT_TOKEN_INVALID"

    response = env.client.post(
        "/api/session/commit", json={"mode": "save_as", "draft": DRAFT}
    )
    assert response.status_code == 422, "缺少 token 字段应被模型拒绝"


def test_commit_token_cannot_be_reused(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()
    assert _commit(env.client, prepared["token"], target_path=str(target)).status_code == 200

    again = _commit(env.client, prepared["token"], target_path=str(target))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "COMMIT_TOKEN_USED"
    assert len([call for call in env.fake.save_calls]) == 1, "复用令牌不得再次落盘"


def test_commit_token_expires(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()
    # 直接操纵票据的到期时间：模拟「用户看了很久才点确认」
    env.app.state.commit._tickets[prepared["token"]].expires_at = 0.0  # noqa: SLF001

    response = _commit(env.client, prepared["token"], target_path=str(target))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMIT_TOKEN_EXPIRED"
    assert not target.exists()


def test_commit_token_rejects_tampered_draft(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()

    response = env.client.post(
        "/api/session/commit",
        json={
            "token": prepared["token"],
            "mode": "save_as",
            "target_path": str(target),
            "draft": {**DRAFT, "color.exposure": 3.0},
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"
    assert not target.exists()


def test_commit_token_rejects_tampered_mode(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()
    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=str(target))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"
    assert not target.exists()


def test_commit_token_rejects_tampered_target(env: SimpleNamespace) -> None:
    target = _target(env)
    other = _target(env, "other.blend")
    prepared = _prepare(env.client, target_path=str(target)).json()

    response = _commit(env.client, prepared["token"], target_path=str(other))
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "COMMIT_TOKEN_MISMATCH"
    assert error["details"]["expected"] == str(target)
    assert not other.exists() and not target.exists()


def test_commit_token_rejects_invalid_target_shape(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()
    response = _commit(env.client, prepared["token"], target_path="relative/blend.txt")
    assert response.status_code == 409
    # 非法形态一律按「与准备阶段不一致」拒绝，绝不回显原始输入
    assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"


def test_commit_rejects_stale_baseline(env: SimpleNamespace) -> None:
    target = _target(env)
    prepared = _prepare(env.client, target_path=str(target)).json()

    refreshed = env.client.post("/api/session/baseline").json()
    assert refreshed["baseline_id"] != prepared["baseline_id"]

    response = _commit(env.client, prepared["token"], target_path=str(target))
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "BASELINE_STALE"
    assert error["details"]["current_baseline_id"] == refreshed["baseline_id"]
    assert not target.exists()


def test_commit_write_endpoints_require_token(env: SimpleNamespace) -> None:
    client = unauthed(env.app)
    target = _target(env)
    for path, payload in (
        ("/api/session/commit/prepare", {"mode": "save_as", "draft": DRAFT, "target_path": str(target)}),
        ("/api/session/commit", {"token": "x", "mode": "save_as", "draft": DRAFT}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"
    assert len(env.fake.save_calls) == 0


# -- 失败路径：一律不保存 ---------------------------------------------------


def test_backup_failure_blocks_overwrite(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    env.project.write_bytes(OLD_CONTENT)
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(project_ops.shutil, "copy2", boom)
    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)
    monkeypatch.undo()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "BACKUP_FAILED"
    assert env.project.read_bytes() == OLD_CONTENT, "备份失败就绝不能覆盖原文件"
    assert len(env.fake.save_calls) == 0, "备份失败时不允许调用保存"


def test_readback_mismatch_blocks_save(tmp_path: Path) -> None:
    with running_env(tmp_path, with_injector=True) as env:
        body = env.client.post("/api/session/baseline").json()
        _wait_job(env.client, body["job_id"])
        baseline_exposure = env.fake.view_settings.exposure
        target = _target(env)

        prepared = _prepare(env.client, target_path=str(target)).json()
        env.injector.arm(ApplyInjector.MODE_CORRUPT_READBACK)
        response = _commit(env.client, prepared["token"], target_path=str(target))

        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "APPLY_VERIFY_FAILED"
        status = error["details"]["status"]
        assert status["applied"] is True
        assert status["readback_verified"] is False
        assert status["mismatches"], "必须列出不一致项"
        assert status["saved"] is False
        assert status["draft_preserved"] is True
        assert not target.exists(), "回读不一致时绝不能保存"
        assert len(env.fake.save_calls) == 0

        # 草稿已经写进场景，因此必须回滚到提交前的基线，且回读校验通过
        assert status["rollback"]["attempted"] is True
        assert status["rollback"]["verified"] is True
        assert status["rollback"]["mismatches"] == []
        assert status["rollback"]["code"] is None
        assert env.fake.view_settings.exposure == pytest.approx(baseline_exposure)
        # 文案不得再声称「工程未被改动」这种不属实的说法
        assert "工程未被改动" not in error["message"]
        assert "磁盘上的工程文件未被写入" in error["message"]
        assert "已恢复到提交前的基线" in error["message"]

        # 令牌已被消费：必须重新准备，不能拿旧令牌反复试
        again = _commit(env.client, prepared["token"], target_path=str(target))
        assert again.json()["error"]["code"] == "COMMIT_TOKEN_USED"


def test_save_failure_reports_status_and_keeps_draft(env: SimpleNamespace) -> None:
    env.project.write_bytes(OLD_CONTENT)
    baseline_exposure = env.fake.view_settings.exposure
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()

    env.fake.save_error = "磁盘写满"
    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "SAVE_FAILED"
    status = error["details"]["status"]
    assert status["applied"] is True
    assert status["readback_verified"] is True
    assert status["backup"]["created"] is True, "备份已成功生成，必须如实报告"
    assert status["saved"] is False
    assert status["draft_preserved"] is True, "保存失败必须保留草稿"
    assert env.project.read_bytes() == OLD_CONTENT
    assert not (env.tmp_path / "out").exists() or not list((env.tmp_path / "out").iterdir())

    # 保存失败后场景必须回到提交前的基线（草稿不再残留在 Blender 里）
    assert status["rollback"]["attempted"] is True
    assert status["rollback"]["verified"] is True
    assert status["rollback"]["code"] is None
    assert env.fake.view_settings.exposure == pytest.approx(baseline_exposure)
    assert env.fake.glare_node.inputs.get("Threshold").default_value == pytest.approx(1.1)  # type: ignore[union-attr]


def test_backup_failure_also_rolls_back_baseline(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备份失败 → 不覆盖，且草稿不得残留在场景里。"""
    env.project.write_bytes(OLD_CONTENT)
    baseline_exposure = env.fake.view_settings.exposure
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(project_ops.shutil, "copy2", boom)
    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)
    monkeypatch.undo()

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "BACKUP_FAILED"
    status = error["details"]["status"]
    assert status["saved"] is False
    assert status["rollback"]["attempted"] is True
    assert status["rollback"]["verified"] is True
    assert env.fake.view_settings.exposure == pytest.approx(baseline_exposure)
    assert env.project.read_bytes() == OLD_CONTENT
    assert "已恢复到提交前的基线" in error["message"]


def test_rollback_failure_is_reported_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回滚失败必须明确报出，而不能含糊地说「已回滚」。

    构造方式：备份失败触发回滚路径，同时让**回滚那一次写入**失败。
    注入在基线任务跑完之后才武装，因此只命中本次提交与回滚。
    """
    with running_env(tmp_path, with_injector=True) as env:
        body = env.client.post("/api/session/baseline").json()
        _wait_job(env.client, body["job_id"])

        env.project.write_bytes(OLD_CONTENT)
        prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(5, "拒绝访问")

        monkeypatch.setattr(project_ops.shutil, "copy2", boom)
        env.injector.arm(ApplyInjector.MODE_FAIL_WRITE)
        response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)
        monkeypatch.undo()

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "BACKUP_FAILED", "顶层错误码保留原始失败原因"
    status = error["details"]["status"]
    rollback = status["rollback"]
    assert rollback["attempted"] is True
    assert rollback["verified"] is False
    assert rollback["code"] == "ROLLBACK_FAILED"
    assert rollback["error"], "必须带上回滚失败的原因"
    # 文案必须直说「没恢复成」，并点名 ROLLBACK_FAILED
    assert "ROLLBACK_FAILED" in error["message"]
    assert "请人工确认" in error["message"]
    assert "已恢复到提交前的基线" not in error["message"]
    assert status["saved"] is False
    assert env.project.read_bytes() == OLD_CONTENT


# -- 备份路径：必须使用票据里已确认的那一条 ---------------------------------


def test_commit_uses_prepare_time_backup_path_across_time_boundary(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨时间边界后，实际备份路径必须仍等于用户确认过的那一条。"""
    clock = FrozenClock(datetime(2026, 10, 9, 17, 0, 0))
    monkeypatch.setattr(project_ops, "_dt", SimpleNamespace(datetime=clock))

    env.project.write_bytes(OLD_CONTENT)
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
    confirmed = Path(prepared["backup_path"])
    assert clock.calls == 1, "备份路径只应在 prepare 阶段计算一次"

    result = _commit(
        env.client, prepared["token"], mode="overwrite", target_path=None
    ).json()

    assert clock.calls == 1, "commit 不得重新计算备份路径"
    assert result["backup_path"] == str(confirmed)
    assert confirmed.is_file()
    assert confirmed.read_bytes() == OLD_CONTENT
    # 目录里不得出现「按当前时间算出来」的第二条备份
    backups = sorted(env.project.parent.glob("*.bak-*.blend"))
    assert [path.name for path in backups] == [confirmed.name]


def test_prepare_computes_backup_path_exactly_once(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prepare 自身也不得把备份路径算两次。

    「二次确认」里展示给用户的预计路径，与随后绑进票据的路径必须是同一个字符串。
    各算一次的话，跨过整秒边界就会得到两个名字 —— 用户确认的并不是真正会用的那一条。
    """
    clock = FrozenClock(datetime(2026, 10, 9, 17, 0, 0))
    monkeypatch.setattr(project_ops, "_dt", SimpleNamespace(datetime=clock))
    env.project.write_bytes(OLD_CONTENT)

    # 第一次：不确认 → 只展示，不签发令牌
    clock.calls = 0
    refused = _prepare(env.client, mode="overwrite", target_path=None)
    assert refused.status_code == 409
    shown = refused.json()["error"]["details"]["backup_path"]
    assert clock.calls == 1, f"prepare 只应计算一次备份路径，实际 {clock.calls} 次"

    # 第二次：确认 → 签发令牌，且票据里的路径与上面展示的那条一致
    clock.calls = 0
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
    assert clock.calls == 1, f"prepare 只应计算一次备份路径，实际 {clock.calls} 次"
    assert prepared["backup_path"] == shown


def test_commit_refuses_when_confirmed_backup_path_is_taken(
    env: SimpleNamespace,
) -> None:
    """已确认的备份路径上已有文件 → 拒绝覆盖（不销毁已有备份，也不重算路径）。"""
    env.project.write_bytes(OLD_CONTENT)
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
    confirmed = Path(prepared["backup_path"])
    confirmed.write_bytes(b"EXISTING-BACKUP")

    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "BACKUP_FAILED"
    assert confirmed.read_bytes() == b"EXISTING-BACKUP", "不得覆盖已有备份"
    assert env.project.read_bytes() == OLD_CONTENT
    assert len(env.fake.save_calls) == 0
    assert error["details"]["status"]["rollback"]["verified"] is True


def test_commit_refuses_when_target_vanishes_after_confirmation(
    env: SimpleNamespace,
) -> None:
    """确认之后原文件消失：无法按已确认路径备份 → 拒绝继续，且场景未被写入。"""
    env.project.write_bytes(OLD_CONTENT)
    prepared = _prepare(env.client, mode="overwrite", target_path=None, confirm=True).json()
    env.project.unlink()

    response = _commit(env.client, prepared["token"], mode="overwrite", target_path=None)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "BACKUP_FAILED"
    status = error["details"]["status"]
    assert status["applied"] is False, "前提复核应在写场景之前"
    assert status["rollback"]["attempted"] is False
    assert "无需回滚" in status["rollback"]["reason"]
    assert "无需回滚" in error["message"]
    assert len(env.fake.save_calls) == 0


def test_commit_requires_reconfirm_when_target_appears_after_confirmation(
    env: SimpleNamespace,
) -> None:
    """确认时目标不存在（无需备份），确认之后才出现 → 必须重新确认（会先生成备份）。"""
    target = _target(env)
    assert not target.exists()
    prepared = _prepare(env.client, target_path=str(target)).json()
    assert prepared["backup_path"] is None

    target.write_bytes(OLD_CONTENT)
    response = _commit(env.client, prepared["token"], target_path=str(target))

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SAVE_CONFIRM_REQUIRED"
    assert target.read_bytes() == OLD_CONTENT, "未重新确认前不得覆盖"
    status = error["details"]["status"]
    assert status["applied"] is False
    assert status["rollback"]["attempted"] is False
    assert len(env.fake.save_calls) == 0


# -- 复合场景 ---------------------------------------------------------------


def test_save_as_then_overwrite_round_trip(env: SimpleNamespace) -> None:
    """先另存为到新文件，再覆盖该文件；两步都过回读校验，且备份链完整。"""
    target = _target(env)

    first = _prepare(env.client, target_path=str(target)).json()
    assert _commit(env.client, first["token"], target_path=str(target)).json()["saved"] is True

    second_draft = {"color.exposure": -1.25}
    prepared = env.client.post(
        "/api/session/commit/prepare",
        json={"mode": "save_as", "draft": second_draft, "target_path": str(target), "confirm": True},
    ).json()
    result = env.client.post(
        "/api/session/commit",
        json={
            "token": prepared["token"],
            "mode": "save_as",
            "target_path": str(target),
            "draft": second_draft,
        },
    ).json()

    assert result["saved"] is True
    assert Path(result["backup_path"]).is_file()
    assert env.fake.view_settings.exposure == pytest.approx(-1.25)
