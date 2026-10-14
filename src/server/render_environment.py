# -*- coding: utf-8 -*-
"""渲染环境只读诊断（PR #6 规格 §5）。

**只读**：本模块不创建、不修改、不删除任何东西（连孤儿锁都只计数不清除），
也不接受前端任何路径参数 —— 路径全部来自服务端自己的配置。

安全红线（规格 §5.4）：
* 响应里**绝不出现本机完整绝对路径**，一律经 ``redact.display_path`` 转成脱敏标签；
* 前端无法通过本接口改路径；
* 配置缺失时给出**具体缺什么**，让上层禁用「开始渲染」并说明原因，
  而不是把用户推到一句「环境未就绪」。

分级：``ok`` / ``warning`` / ``error``。``ready`` 当且仅当没有 error 级检查项 ——
warning（例如模型目录为空、已有活动任务）不阻断渲染，但必须让用户看见。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .redact import display_path

#: 检查项分级。
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

#: 磁盘余量警戒线（GB）：低于 warning，低于 error 视为不可开始。
_DISK_WARN_GB = 5.0
_DISK_ERROR_GB = 1.0


@dataclass(frozen=True)
class EnvironmentCheck:
    """一项检查。``message`` 面向用户，``hint`` 是下一步该做什么。"""

    id: str
    status: str
    message: str
    hint: str | None = None

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass(frozen=True)
class EnvironmentReport:
    ready: bool
    checks: tuple[EnvironmentCheck, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        """error 级检查项的 id（上层据此禁用开始按钮）。"""
        return tuple(c.id for c in self.checks if c.status == STATUS_ERROR)

    def to_public(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": list(self.blockers),
            "checks": [c.to_public() for c in self.checks],
        }


# -- 小工具 ---------------------------------------------------------------


def _label(path: Any, root: Any = None) -> str:
    """路径 → 脱敏展示标签。

    只有 ``display_path`` 产出**占位符形态**（``<LOCALAPPDATA>\\...`` 之类）时才
    原样使用；其余情况一律退到文件名。理由：规格 §5.4 要求响应里不出现本机完整
    绝对路径，而 ``display_path`` 的规则 3 会原样保留与用户名无关的盘符路径
    （如 ``Z:\\models``）—— 那对诊断接口来说仍然是多余的信息。
    """
    if path in (None, ""):
        return "未配置"
    try:
        text = str(display_path(path, root=root))
    except Exception:  # noqa: BLE001 - 展示失败不能让诊断接口挂掉
        text = ""
    if not text:
        return "（不可读）"
    if text.startswith("<"):
        return text
    # 取最后一段；Windows 盘符路径用两种分隔符都试一次。
    tail = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail or "（不可读）"


def _is_readable_dir(path: Any) -> bool:
    return bool(path) and Path(str(path)).is_dir() and os.access(str(path), os.R_OK)


def _writable_dir(path: Any) -> bool:
    """目录是否可写：真写一个临时文件再删掉，而不是只看权限位。"""
    if not path:
        return False
    base = Path(str(path))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if not base.is_dir():
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".probe-", dir=str(base))
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return True


def _free_gb(path: Any) -> float | None:
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return None


def _disk_status(free: float | None) -> str:
    if free is None:
        return STATUS_ERROR
    if free < _DISK_ERROR_GB:
        return STATUS_ERROR
    if free < _DISK_WARN_GB:
        return STATUS_WARNING
    return STATUS_OK


# -- 主入口 ---------------------------------------------------------------


def build_environment_report(
    *,
    blender_status: dict[str, Any] | None,
    blender_exe: Any = None,
    source_blend: Any = None,
    model_root: Any = None,
    output_root: Any = None,
    runtime_dir: Any = None,
    maps_dir: Any = None,
    fixed_script: Any = None,
    jobs: Sequence[Any] = (),
    stale_lock_count: int = 0,
) -> EnvironmentReport:
    """汇总 12 项只读检查（规格 §5.3）。

    ``jobs`` 只需具备 ``state`` / ``model_name`` / ``job_id`` 属性（``RenderJob`` 即满足）。
    """
    checks: list[EnvironmentCheck] = []

    # 1&2. Blender MCP 连接与版本
    status = blender_status or {}
    connected = bool(status.get("ok")) and status.get("status") == "connected"
    target = status.get("target") or "未配置"
    if connected:
        latency = status.get("latency_ms")
        suffix = f"（延迟 {latency} ms）" if isinstance(latency, (int, float)) else ""
        checks.append(
            EnvironmentCheck(
                "blender_mcp",
                STATUS_OK,
                f"Blender MCP 已连接：{target}{suffix}",
            )
        )
    else:
        detail = (status.get("error") or {}).get("message") if isinstance(status.get("error"), dict) else None
        checks.append(
            EnvironmentCheck(
                "blender_mcp",
                STATUS_ERROR,
                "Blender MCP 未连接" + (f"：{detail}" if detail else ""),
                "请打开 Blender 测试工程，在侧边栏 N 面板启动 MCP 服务，确认端口为 9876，然后点「重新检测」。",
            )
        )

    # 3. Blender 可执行文件
    if blender_exe and Path(str(blender_exe)).is_file():
        checks.append(EnvironmentCheck("blender_exe", STATUS_OK, f"Blender 程序可用：{_label(blender_exe)}"))
    elif blender_exe:
        checks.append(
            EnvironmentCheck(
                "blender_exe",
                STATUS_ERROR,
                f"Blender 程序路径无效：{_label(blender_exe)}",
                "请在首次启动配置里重新选择 blender.exe。",
            )
        )
    else:
        checks.append(
            EnvironmentCheck(
                "blender_exe",
                STATUS_ERROR,
                "尚未配置 Blender 可执行文件",
                "点「选择 Blender」定位 blender.exe。",
            )
        )

    # 4. 固定入口脚本
    if fixed_script and Path(str(fixed_script)).is_file():
        checks.append(EnvironmentCheck("fixed_script", STATUS_OK, "固定渲染脚本存在"))
    else:
        checks.append(
            EnvironmentCheck(
                "fixed_script",
                STATUS_ERROR,
                "固定渲染脚本缺失",
                "render_pipeline/开始渲染.py 是唯一入口，缺失说明安装不完整，请重新获取仓库。",
            )
        )

    # 5. 源工程 .blend
    if source_blend and Path(str(source_blend)).is_file():
        checks.append(
            EnvironmentCheck("source_blend", STATUS_OK, f"源工程可用：{_label(source_blend)}")
        )
    elif source_blend:
        checks.append(
            EnvironmentCheck(
                "source_blend",
                STATUS_ERROR,
                f"源工程路径无效：{_label(source_blend)}",
                "请在首次启动配置里重新选择源 .blend。",
            )
        )
    else:
        checks.append(
            EnvironmentCheck(
                "source_blend",
                STATUS_ERROR,
                "尚未配置源工程 .blend",
                "点「选择源工程」指定用于重建场景的 .blend。",
            )
        )

    # 6. 模型目录
    if _is_readable_dir(model_root):
        try:
            count = sum(
                1
                for p in Path(str(model_root)).iterdir()
                if p.is_file() and p.suffix.lower() in (".pmx", ".blend")
            )
        except OSError:
            count = 0
        if count:
            checks.append(
                EnvironmentCheck("model_root", STATUS_OK, f"模型目录可读，含 {count} 个模型")
            )
        else:
            checks.append(
                EnvironmentCheck(
                    "model_root",
                    STATUS_WARNING,
                    "模型目录可读，但还没有 .pmx / .blend 模型",
                    "把模型文件放进模型根目录后点「刷新模型列表」。",
                )
            )
    elif model_root:
        checks.append(
            EnvironmentCheck(
                "model_root",
                STATUS_ERROR,
                f"模型目录不可读：{_label(model_root)}",
                "请确认目录存在且有读取权限。",
            )
        )
    else:
        checks.append(
            EnvironmentCheck(
                "model_root",
                STATUS_ERROR,
                "尚未配置模型根目录",
                "点「选择模型目录」指定存放 .pmx / .blend 的位置。",
            )
        )

    # 7&8&9. 输出 / 运行 / 映射目录可写
    for check_id, path, label, hint in (
        ("output_root", output_root, "输出目录", "点「选择输出目录」指定一个可写位置。"),
        ("runtime_dir", runtime_dir, "运行目录", "运行目录用于锁与任务索引，请在配置里指定可写位置。"),
        ("maps_dir", maps_dir, "材质映射目录", "材质映射按模型指纹存放，请在配置里指定可写位置。"),
    ):
        if _writable_dir(path):
            checks.append(
                EnvironmentCheck(check_id, STATUS_OK, f"{label}可写：{_label(path, root=output_root)}")
            )
        elif path:
            checks.append(
                EnvironmentCheck(check_id, STATUS_ERROR, f"{label}不可写：{_label(path, root=output_root)}", hint)
            )
        else:
            checks.append(
                EnvironmentCheck(check_id, STATUS_ERROR, f"尚未配置{label}", hint)
            )

    # 10. 磁盘余量（系统盘 + 输出盘）
    for check_id, path, label in (
        ("disk_system", "C:/" if os.name == "nt" else "/", "系统盘"),
        ("disk_output", output_root or "C:/", "输出盘"),
    ):
        free = _free_gb(path)
        state = _disk_status(free)
        if free is None:
            checks.append(
                EnvironmentCheck(check_id, STATUS_ERROR, f"{label}余量无法读取", "请检查磁盘是否正常挂载。")
            )
        else:
            message = f"{label}剩余 {free:.1f} GB"
            hint = None
            if state == STATUS_ERROR:
                hint = "空间几乎耗尽，渲染产物可能写不进去。请先清理输出目录或更换输出位置。"
            elif state == STATUS_WARNING:
                hint = "余量偏少，建议清理历史任务（渲染页有「清理历史任务」入口）。"
            checks.append(EnvironmentCheck(check_id, state, message, hint))

    # 11. 孤儿锁
    if stale_lock_count > 0:
        checks.append(
            EnvironmentCheck(
                "stale_locks",
                STATUS_WARNING,
                f"发现 {stale_lock_count} 个遗留渲染锁",
                "上次异常退出可能留下锁文件。重启服务会自动清理，或手动删除运行目录 .locks 下的 *.render.lock。",
            )
        )
    else:
        checks.append(EnvironmentCheck("stale_locks", STATUS_OK, "没有遗留渲染锁"))

    # 12. 活动渲染任务
    active = [j for j in jobs if not getattr(j, "is_terminal", False)]
    if active:
        names = "、".join(str(getattr(j, "model_name", getattr(j, "job_id", "?"))) for j in active[:3])
        checks.append(
            EnvironmentCheck(
                "active_jobs",
                STATUS_WARNING,
                f"当前有 {len(active)} 个进行中的渲染任务（{names}）",
                "同一时间只会执行一个渲染；新任务会排队等待。",
            )
        )
    else:
        checks.append(EnvironmentCheck("active_jobs", STATUS_OK, "当前没有进行中的渲染任务"))

    ready = not any(c.status == STATUS_ERROR for c in checks)
    return EnvironmentReport(ready=ready, checks=tuple(checks))
