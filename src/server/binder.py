"""Blender 高层操作绑定：把 ``blender_ops`` 生成的代码下发到 MCP 并解析结果。

这一层是**同步（阻塞）**的，调用方负责放进线程池执行。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import blender_ops, errors
from .blender_mcp import BlenderMCPClient
from .config import BlenderMCPConfig

PREVIEW_DIR_NAME = "toon-tuner-previews"
#: 预览渲染使用的分辨率档（按参数面清单建议：预览 540×990）
PREVIEW_RESOLUTION = (540, 990, 100)


def preview_dir() -> Path:
    """预览 PNG 的落盘目录（系统临时目录，不进仓库）。"""
    directory = Path(tempfile.gettempdir()) / PREVIEW_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def preview_png_name(job_id: str) -> str:
    return f"preview_{job_id}.png"


class BlenderBinder:
    """面向前端的四个固定动作：读基线、写草稿、渲染预览、回滚基线。"""

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

    # -- 写入 -----------------------------------------------------------
    def apply_values(self, values: dict[str, Any]) -> dict[str, Any]:
        if not values:
            return {}
        captured = self._client().execute_code(blender_ops.build_set_code(values))
        return blender_ops.extract_json(captured)

    # -- 渲染 -----------------------------------------------------------
    def render_preview(self, job_id: str) -> dict[str, Any]:
        path = preview_dir() / preview_png_name(job_id)
        code = blender_ops.build_render_code(path.as_posix(), *PREVIEW_RESOLUTION)
        captured = self._client().execute_code(code)
        payload = blender_ops.extract_json(captured)
        if not payload.get("rendered"):
            raise errors.ToonTunerError(
                errors.PREVIEW_FAILED,
                "Blender 报告渲染未产出文件。",
                details={"path_tail": Path(str(payload.get("path", ""))).name},
            )
        return payload
