"""Blender 高层操作绑定：把 ``blender_ops`` 生成的代码下发到 MCP 并解析结果。

这一层是**同步（阻塞）**的，调用方负责放进线程池执行。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import blender_ops, color_looks, errors, framing, project_ops, surface_probe
from .blender_mcp import BlenderMCPClient
from .config import BlenderMCPConfig
from .redact import redact

PREVIEW_DIR_NAME = "toon-tuner-previews"
#: 预览渲染的**长边**像素数（另一条边按工程纵横比等比缩放，绝不改变纵横比）
PREVIEW_LONG_SIDE = 990
#: 工程分辨率不可读时的兜底预览尺寸
PREVIEW_FALLBACK_RESOLUTION = (540, 990, 100)


def preview_dir() -> Path:
    """预览 PNG 的落盘目录（系统临时目录，不进仓库）。"""
    directory = Path(tempfile.gettempdir()) / PREVIEW_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def preview_png_name(job_id: str) -> str:
    return f"preview_{job_id}.png"


def preview_resolution_for(render: dict[str, Any] | None) -> tuple[int, int, int]:
    """按工程分辨率等比缩小到长边 ``PREVIEW_LONG_SIDE``。

    必须保持纵横比：取景诊断（是否完整入画）与实际渲染必须共用同一画面形状，
    否则界面会给出自相矛盾的结论。
    """
    raw = render or {}
    try:
        rx = int(raw.get("resolution_x") or 0)
        ry = int(raw.get("resolution_y") or 0)
        percent = float(raw.get("resolution_percentage") or 100)
    except (TypeError, ValueError):
        return PREVIEW_FALLBACK_RESOLUTION
    if rx <= 0 or ry <= 0 or percent <= 0:
        return PREVIEW_FALLBACK_RESOLUTION

    width = max(16, int(round(rx * percent / 100.0)))
    height = max(16, int(round(ry * percent / 100.0)))
    scale = PREVIEW_LONG_SIDE / max(width, height)
    return (
        max(16, int(round(width * scale))),
        max(16, int(round(height * scale))),
        100,
    )


class BlenderBinder:
    """面向前端的固定动作：读基线、读取景、写草稿、渲染预览、回滚基线。"""

    def __init__(self, config: BlenderMCPConfig) -> None:
        self._config = config

    @property
    def target(self) -> str:
        return self._config.target

    def _client(self) -> BlenderMCPClient:
        return BlenderMCPClient(self._config)

    # -- 只读 -----------------------------------------------------------
    def read_exposure(self) -> dict[str, Any]:
        captured = self._client().execute_code(blender_ops.build_read_code())
        return blender_ops.extract_json(captured)

    def read_framing(self) -> dict[str, Any]:
        """只读取景上下文（帧 / 相机 / 相机动画 / 角色包围盒 / 是否完整入画）。

        返回**归一化后**的上下文，调用方无需再处理 Blender 原始字段。
        """
        captured = self._client().execute_code(framing.build_context_code())
        return framing.normalize_context(framing.parse_context(captured))

    def read_look_capability(self) -> dict[str, Any]:
        """一次性扫出每个 ``view_transform`` 对应的合法 look（建立基线时调用）。

        只读语义：Blender 侧在 ``finally`` 中恢复原 ``view_transform`` / ``look``。
        """
        captured = self._client().execute_code(color_looks.build_sweep_code())
        payload = color_looks.parse_payload(captured)
        restored = payload.get("restored") or {}
        if not restored.get("ok", False):
            raise errors.ToonTunerError(
                errors.BLENDER_SCRIPT_ERROR,
                "look 能力探测未能恢复原 view_transform / look。",
                details={
                    "original": payload.get("original"),
                    "restored": restored,
                },
            )
        return {
            "looks": color_looks.normalize_look_map(payload.get("looks")),
            "raw": payload.get("looks") or {},
            "sources": payload.get("sources") or {},
            "failed": payload.get("failed") or {},
            "view_transforms": payload.get("view_transforms") or [],
            "original": payload.get("original") or {},
        }

    def probe_look_for(
        self, view_transform: str, identifier: str | None = None
    ) -> dict[str, Any]:
        """探测单个 ``view_transform`` 的合法 look（需求 2 的独立只读探针）。"""
        captured = self._client().execute_code(
            color_looks.build_probe_code(view_transform, identifier)
        )
        return color_looks.parse_payload(captured)

    def describe_surface(self) -> dict[str, Any]:
        """只读拓扑描述（v4）：受管节点组 / ColorRamp 结构 / 对象与材质清单。

        探针无入参、只读、不改任何 ``bpy`` 数据；脱敏在服务端侧再做一遍
        （``surface_probe.redact_describe``），不依赖 Blender 侧自觉。
        """
        captured = self._client().execute_code(surface_probe.build_describe_code())
        return surface_probe.parse_describe(captured)

    # -- 写入 -----------------------------------------------------------
    def apply_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """原子应用一组取值。

        Blender 侧失败时**不会**抛异常，而是返回结构化 ``failure``；
        这里把它翻译成稳定错误码（``invalid_dependent_enum`` 绝不会退化成
        通用的 ``BLENDER_SCRIPT_ERROR``）。
        """
        if not values:
            return {}
        captured = self._client().execute_code(blender_ops.build_set_code(values))
        payload = blender_ops.extract_json(captured)
        if payload.get("applied") is False:
            error = blender_ops.failure_to_error(payload.get("failure"))
            if error is not None:
                if payload.get("restored_to"):
                    error.details.setdefault("restored_to", payload["restored_to"])
                if payload.get("restore_ok") is not None:
                    error.details.setdefault("restore_ok", payload["restore_ok"])
                raise error
            raise errors.ToonTunerError(
                errors.BLENDER_SCRIPT_ERROR,
                "应用草稿失败，且 Blender 未给出可识别的失败原因。",
                details={"blender_payload": payload},
            )
        return payload

    # -- 渲染 -----------------------------------------------------------
    def render_preview(
        self,
        job_id: str,
        framing_options: dict[str, Any] | None = None,
        resolution: tuple[int, int, int] | None = None,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """渲染单张预览。

        * ``framing_options.mode`` 为自动取景时，Blender 侧会新建**临时预览相机**，
          渲染结束后在 ``finally`` 里恢复 ``scene.camera`` 并删除该临时相机。
        * ``expected`` 给出基线时的帧与相机名，被外部改动时 Blender 侧直接中止渲染。
        """
        path = preview_dir() / preview_png_name(job_id)
        width, height, percentage = resolution or PREVIEW_FALLBACK_RESOLUTION
        code = framing.build_render_code(
            path.as_posix(), width, height, percentage, framing_options, expected
        )
        captured = self._client().execute_code(code)
        payload = blender_ops.extract_json(captured)

        aborted = payload.get("aborted")
        if isinstance(aborted, dict) and aborted.get("code"):
            code_ = str(aborted["code"])
            message = str(aborted.get("message") or "取景前置条件已变化，预览中止。")
            if code_ == errors.FRAMING_STALE:
                raise errors.ToonTunerError(errors.FRAMING_STALE, message)
            raise errors.ToonTunerError(
                errors.PREVIEW_FAILED, message, details={"abort_code": code_}
            )

        if not payload.get("rendered"):
            raise errors.ToonTunerError(
                errors.PREVIEW_FAILED,
                "Blender 报告渲染未产出文件。",
                details={"path_tail": Path(str(payload.get("path", ""))).name},
            )
        return payload

    # -- 工程读写（保存功能）----------------------------------------------
    def read_project(self) -> dict[str, Any]:
        """只读：当前工程路径 + 是否有未保存改动。不写任何 ``bpy`` 数据。"""
        captured = self._client().execute_code(project_ops.build_project_state_code())
        return project_ops.normalize_project_state(project_ops.parse_payload(captured))

    def save_project(self, target_path: str, mode: str) -> dict[str, Any]:
        """把工程存到 ``target_path``（``mode`` 决定 save_as / save_mainfile）。

        Blender 侧失败不抛异常而是回结构化 ``failure``，这里翻译成稳定错误码
        ``SAVE_FAILED``，并把「失败在哪一步」留在 details 里（不含绝对路径）。
        """
        captured = self._client().execute_code(
            project_ops.build_save_code(target_path, mode)
        )
        payload = project_ops.parse_payload(captured)
        failure = payload.get("failure")
        if payload.get("saved") is not True:
            detail = ""
            if isinstance(failure, dict):
                detail = str(failure.get("message") or failure.get("type") or "")
            raise errors.ToonTunerError(
                errors.SAVE_FAILED,
                f"Blender 保存工程失败：{redact(detail) or '未给出可识别的原因'}",
                details={
                    "mode": mode,
                    "target_name": Path(str(payload.get("target", target_path))).name,
                    "failure_type": (failure or {}).get("type") if isinstance(failure, dict) else None,
                },
            )
        return payload
