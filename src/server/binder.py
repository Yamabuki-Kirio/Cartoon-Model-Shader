"""Blender 高层操作绑定：把 ``blender_ops`` 生成的代码下发到 MCP 并解析结果。

这一层是**同步（阻塞）**的，调用方负责放进线程池执行。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import blender_ops, errors, framing
from .blender_mcp import BlenderMCPClient
from .config import BlenderMCPConfig

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

    # -- 写入 -----------------------------------------------------------
    def apply_values(self, values: dict[str, Any]) -> dict[str, Any]:
        if not values:
            return {}
        captured = self._client().execute_code(blender_ops.build_set_code(values))
        return blender_ops.extract_json(captured)

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
