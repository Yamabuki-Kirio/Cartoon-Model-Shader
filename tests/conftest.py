"""pytest 公共 fixture。

两个约定在这里集中落地，避免每个测试各写一份：

1. **预设目录永远指向 ``tmp_path``**：预设功能一旦漏改，绝不允许写到开发者真实的
   ``%LOCALAPPDATA%`` 里去。
2. **写接口经时统一携带会话令牌**（见 ``tests/support.py``）；需要验证拒绝路径时
   用 ``unauthed`` 显式构造不带令牌的客户端。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import make_config


@pytest.fixture()
def preset_dir(tmp_path: Path) -> Path:
    """隔离的预设目录。"""
    directory = tmp_path / "presets"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture()
def preset_config_factory(preset_dir: Path):
    """返回 ``make_config`` 的偏函数：默认把预设目录钉在临时目录。"""

    def factory(port: int, **kwargs):
        kwargs.setdefault("presets_dir", preset_dir)
        return make_config(port, **kwargs)

    return factory
