"""依赖枚举（``view_transform`` → ``look``）的测试。

覆盖需求里点名的每一条，重点是三类真实故障：
1. 把 OCIO 的**全局** look 名单当成当前视图的合法集合，写下 Blender 拒绝的值
   （``enum not found`` → 旧实现落成通用的 ``BLENDER_SCRIPT_ERROR``）；
2. 把**显示标签**当成 value 写进 Blender；
3. 写入顺序错误 / 半应用状态（``view_transform`` 已改、``look`` 没改）。

这些测试**真的执行服务端生成的 Python 代码**（``bpy`` 换成会校验枚举的桩），
所以能抓到生成逻辑本身的问题，而不只是响应格式。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.server import blender_ops, color_looks, errors, params, session
from src.server.app import create_app
from src.server.binder import BlenderBinder
from src.server.blender_ops import JSON_MARKER, build_set_code
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from src.server.session import PreviewService
from tests.fake_bpy import (
    GENERIC_LOOKS,
    LEGACY_LOOK_CAPABILITY,
    LOOK_CAPABILITY,
    FakeBpy,
    extract_marker,
    run_generated_code,
)
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed

LOOK_MARKER = color_looks.LOOK_MARKER


# =============================================================================
#  工具
# =============================================================================


def _binder(server: FakeMCPServer) -> BlenderBinder:
    return BlenderBinder(
        BlenderMCPConfig(
            host=server.host,
            port=server.port,
            connect_timeout_seconds=1.0,
            response_timeout_seconds=5.0,
        )
    )


def _wait(service: PreviewService, job_id: str, timeout: float = 10.0):
    async def _poll():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = service.get_job(job_id)
            if job is not None and job.status in ("done", "failed", "superseded"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"任务未在 {timeout}s 内结束：{job_id}")

    return _poll


def _run(code: str, fake: FakeBpy) -> dict[str, Any]:
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    return payload


def _sweep(fake: FakeBpy) -> dict[str, Any]:
    return extract_marker(
        run_generated_code(color_looks.build_sweep_code(), fake), LOOK_MARKER
    )


def _probe(fake: FakeBpy, view_transform: str, identifier: str | None = None) -> dict[str, Any]:
    return extract_marker(
        run_generated_code(
            color_looks.build_probe_code(view_transform, identifier), fake
        ),
        LOOK_MARKER,
    )


def make_app(server: FakeMCPServer):
    config = AppConfig(
        blender_mcp=BlenderMCPConfig(
            host=server.host,
            port=server.port,
            connect_timeout_seconds=1.0,
            response_timeout_seconds=5.0,
        ),
        server=ServerConfig(),
    )
    return create_app(config)


def wait_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.05)
    return body


# =============================================================================
#  1. 能力探测：拿到的是「当前视图真正接受」的集合，且完整恢复状态
# =============================================================================


def test_sweep_returns_per_view_look_sets_and_restores_state() -> None:
    """一次扫出每个 view_transform 的合法 look；原状态必须逐字段恢复。"""
    fake = FakeBpy()
    original = (fake.view_settings.view_transform, fake.view_settings.look)

    payload = _sweep(fake)

    assert payload["original"] == {"view_transform": original[0], "look": original[1]}
    assert payload["restored"]["ok"] is True
    assert (fake.view_settings.view_transform, fake.view_settings.look) == original

    looks = payload["looks"]
    # AgX 只接受带前缀的档位 —— 这正是「写 'High Contrast' 会被拒绝」的原因
    assert looks["AgX"][0] == "None"
    assert "AgX - High Contrast" in looks["AgX"]
    assert "High Contrast" not in looks["AgX"]
    # Standard 反过来只接受通用档位
    assert looks["Standard"][0] == "None"
    assert "High Contrast" in looks["Standard"]
    assert "AgX - High Contrast" not in looks["Standard"]
    # 通用档位**不可**跨视图用一个固定列表糊过去
    assert set(looks["AgX"]) != set(looks["Standard"])
    # 来源：白嫖 Blender 自己的枚举报错
    assert set(payload["sources"].values()) == {"rna"}


def test_sweep_falls_back_to_assignment_probe_when_error_is_unparsable() -> None:
    """RNA 报错不可解析时退化为逐个赋值探测，结果同样正确且状态完整恢复。"""
    fake = FakeBpy()
    fake.view_settings.enum_error_style = "bare"
    original = (fake.view_settings.view_transform, fake.view_settings.look)

    payload = _sweep(fake)

    assert payload["sources"]["AgX"] == "probe"
    assert set(payload["looks"]["AgX"]) == set(LOOK_CAPABILITY["AgX"])
    assert set(payload["looks"]["Standard"]) == set(LOOK_CAPABILITY["Standard"])
    assert payload["restored"]["ok"] is True
    assert (fake.view_settings.view_transform, fake.view_settings.look) == original


def test_sweep_still_works_without_ocio() -> None:
    """OCIO 不可用时不应崩：哨兵解析路径不依赖候选全集。"""
    fake = FakeBpy()
    fake.ocio_available = False
    original = (fake.view_settings.view_transform, fake.view_settings.look)

    payload = _sweep(fake)

    # 没有 OCIO 就只能探到当前视图，但探到的集合必须准确
    assert set(payload["looks"].keys()) == {original[0]}
    assert set(payload["looks"][original[0]]) == set(LOOK_CAPABILITY[original[0]])
    assert payload["restored"]["ok"] is True
    assert (fake.view_settings.view_transform, fake.view_settings.look) == original


def test_single_view_probe_reports_validity_and_restores() -> None:
    """单视图探针：顺带判定某个 identifier 是否合法，且必定恢复原状态。"""
    fake = FakeBpy()
    original = (fake.view_settings.view_transform, fake.view_settings.look)

    ok = _probe(fake, "AgX", "AgX - High Contrast")
    assert ok["probe"] == {"identifier": "AgX - High Contrast", "allowed": True}
    assert ok["restored"]["ok"] is True

    bad = _probe(fake, "AgX", "High Contrast")
    assert bad["probe"]["allowed"] is False
    assert bad["restored"]["ok"] is True

    assert (fake.view_settings.view_transform, fake.view_settings.look) == original


def test_probe_without_identifier_does_not_probe_the_string_none() -> None:
    """回归：identifier 省略时必须传 Python None，不能退化成字符串 "None"。"""
    fake = FakeBpy()
    payload = _probe(fake, "Standard")
    assert payload["probe"] == {"identifier": None, "allowed": None}
    assert "None" in payload["allowed"]


def test_parse_enum_error_handles_real_blender_message() -> None:
    """真实 Blender 5.2.1 的报错文本必须能解析出允许列表。"""
    text = (
        'bpy_struct: item.attr = val: enum "AgX - Punchy" not found in '
        "('None', 'AgX - Punchy', 'AgX - High Contrast')"
    )
    assert color_looks._parse_enum_error_text(text) == [  # noqa: SLF001
        "None",
        "AgX - Punchy",
        "AgX - High Contrast",
    ]
    assert color_looks._parse_enum_error_text("完全不相关的报错") is None  # noqa: SLF001


# =============================================================================
#  2. value / label 分离
# =============================================================================


def test_look_label_composes_only_for_family_prefix_views() -> None:
    # 已是族前缀形式 -> 原样
    assert color_looks.look_label("AgX", "AgX - High Contrast") == "AgX - High Contrast"
    # 通用档位 + 族前缀视图 -> 组合（这正是需求里 value/label 不同的情形）
    assert color_looks.look_label("AgX", "High Contrast") == "AgX - High Contrast"
    assert color_looks.look_label("False Color", "Low Contrast") == "False Color - Low Contrast"
    # 非族前缀视图 -> 不组合，避免编出 OCIO 里并不存在的名字
    assert color_looks.look_label("Standard", "High Contrast") == "High Contrast"
    assert color_looks.look_label("Filmic", "High Contrast") == "High Contrast"
    assert color_looks.look_label("Raw", "None") == "None"


def test_schema_options_separate_value_and_label() -> None:
    """schema 的枚举项必须是 {value, label}，且 value 是 Blender 真实 identifier。"""
    schema = params.public_schema(
        {
            "view.view_transform": [{"value": "AgX", "label": "AgX"}],
            "view.look": [{"value": "High Contrast", "label": "AgX - High Contrast"}],
        }
    )
    look_spec = next(
        p for g in schema["groups"] for p in g["params"] if p["id"] == "color.look"
    )
    assert look_spec["options"] == [{"value": "High Contrast", "label": "AgX - High Contrast"}]
    assert look_spec["depends_on"] == "color.view_transform"

    # 静态枚举（辉光类型）同样输出 {value,label}
    glow_spec = next(
        p for g in schema["groups"] for p in g["params"] if p["id"] == "glow.type"
    )
    assert {"value": "Bloom", "label": "Bloom"} in glow_spec["options"]


def test_set_code_writes_identifier_not_label() -> None:
    """写进 Blender 的只能是 value；label 不能出现在生成的代码里。"""
    fake = FakeBpy(capability=LEGACY_LOOK_CAPABILITY)
    fake.view_settings.view_transform = "AgX"
    code = build_set_code(
        {"color.view_transform": "AgX", "color.look": "High Contrast"}
    )
    assert "AgX - High Contrast" not in code
    payload = _run(code, fake)
    assert payload["applied"] is True
    assert payload["view"]["look"] == "High Contrast"
    assert ("look", "High Contrast") in fake.view_settings.write_log


# =============================================================================
#  3. 规范化与迁移
# =============================================================================


def _look_map_from(fake: FakeBpy) -> dict[str, list[dict[str, str]]]:
    return color_looks.normalize_look_map(_sweep(fake)["looks"])


def test_resolve_look_migrates_prefix_and_short_forms() -> None:
    look_map = _look_map_from(FakeBpy())
    # Blender 5.2 的 AgX 只接受带前缀的 identifier：短名要能补全
    outcome = color_looks.resolve_look("High Contrast", "AgX", look_map)
    assert outcome["ok"] and outcome["value"] == "AgX - High Contrast" and outcome["migrated"]
    # 反过来，带前缀的名字落到通用档位视图上要能去掉前缀
    outcome = color_looks.resolve_look("AgX - High Contrast", "Standard", look_map)
    assert outcome["ok"] and outcome["value"] == "High Contrast" and outcome["migrated"]
    # 标签同样能对回 value
    legacy = color_looks.normalize_look_map(_sweep(FakeBpy(capability=LEGACY_LOOK_CAPABILITY))["looks"])
    outcome = color_looks.resolve_look("AgX - High Contrast", "AgX", legacy)
    assert outcome["ok"] and outcome["value"] == "High Contrast"


def test_resolve_look_rejects_unrelated_value() -> None:
    look_map = _look_map_from(FakeBpy())
    outcome = color_looks.resolve_look("AgX - Punchy", "Standard", look_map)
    assert outcome["ok"] is False
    assert outcome["reason"] == "not_allowed_for_view_transform"
    assert "High Contrast" in outcome["allowed"]


def test_remap_for_view_transform_falls_back_to_none() -> None:
    """切换视图后旧 look 没有等价项 -> 回退 None（不把旧值发给 Blender）。"""
    look_map = _look_map_from(FakeBpy())
    # AgX 有 "Punchy"，Standard 没有 —— 必须回退
    outcome = color_looks.remap_for_view_transform(look_map, "AgX - Punchy", "Standard")
    assert outcome["value"] == "None" and outcome["migrated"] is True
    # 有等价项则迁移
    outcome = color_looks.remap_for_view_transform(look_map, "AgX - High Contrast", "Standard")
    assert outcome["value"] == "High Contrast"


def test_migrate_values_handles_legacy_preset_names() -> None:
    """旧预设里存的是显示标签 -> 迁移成 value，并保留 label。"""
    look_map = _look_map_from(FakeBpy(capability=LEGACY_LOOK_CAPABILITY))
    records = color_looks.migrate_values(
        {"color.view_transform": "AgX", "color.look": "AgX - High Contrast"},
        look_map,
    )
    look_record = records["color.look"]
    assert look_record["configured_value"] == "High Contrast"
    assert look_record["effective_value"] == "High Contrast"
    assert look_record["display_label"] == "AgX - High Contrast"
    assert look_record["migrated_from"] == "AgX - High Contrast"


def test_migrate_values_falls_back_to_none_with_warning() -> None:
    look_map = _look_map_from(FakeBpy())
    records = color_looks.migrate_values(
        {"color.view_transform": "Standard", "color.look": "AgX - Punchy"}, look_map
    )
    assert records["color.look"]["effective_value"] == "None"
    assert records["color.look"]["warning"]


# =============================================================================
#  4. 写入顺序与原子性
# =============================================================================


def test_set_code_orders_view_transform_before_look() -> None:
    """顺序固定：view_transform → look → exposure → gamma → 其他。"""
    code = build_set_code(
        {
            "color.exposure": 1.0,
            "color.gamma": 1.1,
            "color.look": "None",
            "color.view_transform": "Standard",
            "glow.strength": 2.0,
        }
    )
    order = [
        line.strip()
        for line in code.splitlines()
        if line.strip().startswith("_stage = '") and "init" not in line
    ]
    assert order == [
        "_stage = 'view_transform'",
        "_stage = 'look'",
        "_stage = 'exposure'",
        "_stage = 'gamma'",
        "_stage = 'others'",
    ]
    # 值也确实按这个顺序落下去
    assert code.index("vs.view_transform = 'Standard'") < code.index("vs.look = _want_look")


def test_view_transform_then_full_draft_succeeds() -> None:
    """切到 Standard 后提交完整草稿：view_transform 与通用档位一起生效。"""
    fake = FakeBpy()
    payload = _run(
        build_set_code(
            {
                "color.view_transform": "Standard",
                "color.look": "High Contrast",
                "color.exposure": 0.5,
                "color.gamma": 1.2,
            }
        ),
        fake,
    )
    assert payload["applied"] is True
    assert payload["view"]["view_transform"] == "Standard"
    assert payload["view"]["look"] == "High Contrast"
    assert fake.view_settings.view_transform == "Standard"


def test_look_write_failure_restores_view_transform() -> None:
    """写 look 失败时，view_transform（与曝光/Gamma）必须回到本次应用前的值。"""
    fake = FakeBpy()
    fake.view_settings.view_transform = "Filmic"
    fake.view_settings.look = "Low Contrast"
    fake.view_settings.exposure = 0.3
    fake.view_settings.gamma = 1.4
    before = fake.snapshot_exposure()

    fake.view_settings.fail_on_look = "High Contrast"
    payload = _run(
        build_set_code(
            {
                "color.view_transform": "Standard",
                "color.look": "High Contrast",
                "color.exposure": 2.0,
                "color.gamma": 0.5,
            }
        ),
        fake,
    )

    assert payload["applied"] is False
    assert payload["failure"]["kind"] == "write_failed"
    assert payload["failure"]["stage"] == "look"
    assert payload["restore_ok"] is True
    # 没有半应用状态
    assert fake.snapshot_exposure() == before


def test_invalid_look_is_reported_not_raised_and_state_restored() -> None:
    """Blender 侧的第二道闸门：非法 look 走结构化失败，且状态回滚。"""
    fake = FakeBpy()
    before = fake.snapshot_exposure()
    payload = _run(
        build_set_code({"color.view_transform": "Standard", "color.look": "AgX - Punchy"}),
        fake,
    )
    assert payload["applied"] is False
    failure = payload["failure"]
    assert failure["kind"] == "invalid_dependent_enum"
    assert failure["parameter"] == "color.look"
    assert failure["value"] == "AgX - Punchy"
    assert failure["depends_on"] == {"color.view_transform": "Standard"}
    assert "High Contrast" in failure["allowed"]
    assert fake.snapshot_exposure() == before

    # 且能被翻译成稳定错误码，而不是通用的 BLENDER_SCRIPT_ERROR
    error = blender_ops.failure_to_error(failure)
    assert error is not None
    assert error.code == errors.INVALID_DEPENDENT_ENUM


# =============================================================================
#  5. 会话层：基线能力表 / 提交前校验 / 恢复基线
# =============================================================================


def test_session_baseline_carries_look_map() -> None:
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        service = PreviewService(_binder(server))

        async def scenario():
            await service.start()
            try:
                baseline = await service.capture_baseline()
                look_map = baseline["look_map"]
                # look_map 存的是 {value,label} 选项，比对时取 value
                assert [opt["value"] for opt in look_map["AgX"]] == list(LOOK_CAPABILITY["AgX"])
                assert [opt["value"] for opt in look_map["Standard"]] == list(
                    LOOK_CAPABILITY["Standard"]
                )
                # value 与 label 必须分开：AgX 下的通用档位带族前缀标签
                agx_labels = {opt["value"]: opt["label"] for opt in look_map["AgX"]}
                assert agx_labels["AgX - High Contrast"] == "AgX - High Contrast"
                # 探测期间不许改动工程状态
                assert fake.view_settings.view_transform == "AgX"
                assert fake.view_settings.look == "AgX - High Contrast"
                # 当前视图的候选直接落到 options 上
                assert baseline["options"]["view.look"] == look_map["AgX"]
            finally:
                await service.stop()

        asyncio.run(scenario())


def test_session_accepts_short_look_name_for_agx() -> None:
    """AgX + High Contrast 必须成功（服务端把它规范化成 AgX - High Contrast）。"""
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                job = await service.submit({"color.look": "High Contrast"})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                assert finished.result is not None
                record = finished.result["parameters"]["color.look"]
                assert record["configured_value"] == "High Contrast"
                assert record["effective_value"] == "AgX - High Contrast"
                assert record["display_label"] == "AgX - High Contrast"
                assert record["migrated"] is True
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_rejects_missing_equivalent_with_stable_error() -> None:
    """Standard 下没有 Punchy 档位：提交前就拒，且是 INVALID_DEPENDENT_ENUM。"""
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                with pytest.raises(errors.ToonTunerError) as exc:
                    await service.submit(
                        {"color.view_transform": "Standard", "color.look": "AgX - Punchy"}
                    )
                assert exc.value.code == errors.INVALID_DEPENDENT_ENUM
                payload = exc.value.to_payload()["error"]
                # 需求 6 要求的字段必须平铺在顶层
                assert payload["parameter"] == "color.look"
                assert payload["value"] == "AgX - Punchy"
                assert payload["depends_on"] == {"color.view_transform": "Standard"}
                assert "High Contrast" in payload["allowed"]
                # 拒绝时**不产生任务**，也没碰 Blender
                assert fake.view_settings.view_transform == "AgX"
                assert fake.view_settings.look == "AgX - High Contrast"
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_previews_full_draft_after_view_transform_switch() -> None:
    """切视图 + 完整草稿（含合法 look）可以正常出预览，并逐项还原。"""
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                job = await service.submit(
                    {
                        "color.view_transform": "Standard",
                        "color.look": "High Contrast",
                        "color.exposure": 0.8,
                        "color.gamma": 1.1,
                    }
                )
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                assert finished.result is not None
                assert finished.result["applied"]["color.view_transform"] == "Standard"
                assert finished.result["applied"]["color.look"] == "High Contrast"
                assert finished.result["restore_verified"] is True
                # 渲染完必须回到基线
                assert fake.view_settings.view_transform == "AgX"
                assert fake.view_settings.look == "AgX - High Contrast"
                assert fake.view_settings.exposure == pytest.approx(0.0)
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_restore_baseline_matches_every_field() -> None:
    """恢复基线后两者逐项一致，且走的是同一套依赖映射。"""
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                baseline = await service.capture_baseline()
                # 先把工程改乱
                _run(
                    build_set_code({"color.view_transform": "Standard", "color.look": "None"}),
                    fake,
                )
                assert fake.view_settings.view_transform == "Standard"

                result = await service.restore_baseline()
                assert result["verified"] is True, result["mismatches"]
                assert fake.view_settings.view_transform == baseline["values"]["color.view_transform"]
                assert fake.view_settings.look == baseline["values"]["color.look"]
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_restore_normalizes_look_when_no_longer_valid() -> None:
    """恢复基线也要过映射：旧 look 在新能力表下没有等价项时回退而不是盲写。"""
    fake = FakeBpy()
    look_map = {"AgX": [{"value": "None", "label": "None"}]}
    values = {"color.view_transform": "AgX", "color.look": "AgX - High Contrast"}
    normalized, notes = session.normalize_values(values, look_map)
    assert normalized["color.look"] == "None"
    assert notes and notes[0]["warning"]


# =============================================================================
#  6. API 层
# =============================================================================


def test_api_color_looks_endpoint() -> None:
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with authed(make_app(server)) as client:
            body = client.get("/api/color/looks").json()
            assert body["ok"] is True
            assert body["look_map"] == {}  # 还没建基线

            client.post("/api/session/baseline", json={})
            body = client.get("/api/color/looks").json()
            assert body["source"] == "baseline"
            assert body["current_view_transform"] == "AgX"
            values = [opt["value"] for opt in body["options"]]
            assert "AgX - High Contrast" in values
            assert all("value" in opt and "label" in opt for opt in body["options"])
            # 全量映射都在，前端切视图通常无需再问 Blender
            assert set(body["look_map"]) == set(LOOK_CAPABILITY)

            # 指定视图 + 校验 identifier：走单视图探针
            probed = client.get(
                "/api/color/looks",
                params={"view_transform": "Standard", "identifier": "High Contrast"},
            ).json()
            assert probed["probe"] == {"identifier": "High Contrast", "allowed": True}
            assert probed["restored"]["ok"] is True

            refreshed = client.post("/api/color/looks/refresh").json()
            assert refreshed["ok"] is True
            assert fake.view_settings.view_transform == "AgX"


def test_api_rejects_invalid_dependent_enum() -> None:
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with authed(make_app(server)) as client:
            client.post("/api/session/baseline", json={})
            response = client.post(
                "/api/preview",
                json={
                    "draft": {
                        "color.view_transform": "Standard",
                        "color.look": "AgX - Punchy",
                    }
                },
            )
            assert response.status_code == 400
            error = response.json()["error"]
            assert error["code"] == errors.INVALID_DEPENDENT_ENUM
            assert error["parameter"] == "color.look"
            assert error["depends_on"] == {"color.view_transform": "Standard"}
            assert error["retryable"] is False
            assert error["hint"]
            # 校验发生在调用 Blender 之前：工程状态一字未动
            assert fake.view_settings.view_transform == "AgX"
            assert fake.view_settings.look == "AgX - High Contrast"


def test_api_accepts_legacy_look_label_and_records_migration() -> None:
    """旧预设里的 "AgX - High Contrast" 在 AgX 下就是合法 identifier，直接可用；
    在只提供通用档位的 Blender 上会被规范化为 High Contrast。"""
    fake = FakeBpy(capability=LEGACY_LOOK_CAPABILITY)
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with authed(make_app(server)) as client:
            client.post("/api/session/baseline", json={})
            response = client.post(
                "/api/preview", json={"draft": {"color.look": "AgX - High Contrast"}}
            )
            assert response.status_code == 200, response.text
            job = wait_job(client, response.json()["job_id"])
            assert job["status"] == "done", job.get("error")
            record = job["result"]["parameters"]["color.look"]
            assert record["configured_value"] == "AgX - High Contrast"
            assert record["effective_value"] == "High Contrast"
            assert record["display_label"] == "AgX - High Contrast"


def test_api_job_result_records_configured_effective_and_label() -> None:
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with authed(make_app(server)) as client:
            client.post("/api/session/baseline", json={})
            response = client.post(
                "/api/preview",
                json={"draft": {"color.exposure": 1.25, "color.look": "High Contrast"}},
            )
            assert response.status_code == 200, response.text
            job = wait_job(client, response.json()["job_id"])
            assert job["status"] == "done", job.get("error")
            records = job["result"]["parameters"]
            for key, record in records.items():
                assert set(record) >= {
                    "configured_value",
                    "effective_value",
                    "display_label",
                }, key
            # 未提交的参数也要有记录（configured = 基线值）
            assert records["color.gamma"]["configured_value"] == 1.0
            assert records["color.exposure"]["effective_value"] == pytest.approx(1.25)


# =============================================================================
#  7. 前端契约
# =============================================================================


def _app_js() -> str:
    return Path("src/web/app.js").read_text(encoding="utf-8")


def test_frontend_separates_value_from_label() -> None:
    js = _app_js()
    assert "function optionValue(" in js and "function optionLabel(" in js
    # 写进 select 的 value 必须是 option.value，显示文本才是 label
    assert "opt.value = optionValue(option);" in js
    assert "opt.textContent = optionLabel(option);" in js
    # 收集草稿时取的是 select.value（即 value）
    assert 'draft[paramId] = entry.spec.type === "float" ? Number(entry.input.value) : entry.input.value;' in js
    # 依赖声明要用于界面提示
    assert "spec.depends_on" in js


def test_frontend_refreshes_look_list_on_view_transform_change() -> None:
    js = _app_js()
    assert "VIEW_TRANSFORM_PARAM_ID" in js and "LOOK_PARAM_ID" in js
    assert "function onViewTransformChange(" in js
    assert "function refreshLookOptions(" in js
    assert "function normalizeLook(" in js
    # 切换视图变换时必须先走联动分支，再触发预览
    assert "onViewTransformChange();" in js
    # 不能把旧值直接送去 Blender
    assert "function setSelectOptions(" in js


def test_frontend_guards_against_stale_look_requests() -> None:
    """快速连续切换视图时，旧候选请求不得覆盖新列表。"""
    js = _app_js()
    assert "var lookRequestToken = 0;" in js
    assert "var token = ++lookRequestToken;" in js
    assert "if (token !== lookRequestToken) {" in js


def test_frontend_surfaces_invalid_dependent_enum_details() -> None:
    js = _app_js()
    assert "INVALID_DEPENDENT_ENUM" in js
    assert "depends_on" in js
    assert "allowed" in js
