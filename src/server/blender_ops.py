"""由白名单生成 Blender 侧代码，并解析其结构化回读。

约束
----
* 本模块**只接受已通过 ``params`` 校验的取值**，不接受任何客户端传入的代码。
* 生成的所有字面量都经过 ``repr()`` / 类型强制，杜绝字符串拼接注入。
* 每次回读都以 ``__TOON_JSON__`` 前缀输出一行 JSON，便于稳定解析。

写入顺序（不可随意调换）
------------------------
``view_transform`` 决定 ``look`` 的合法取值集合，因此顺序固定为：

    view_transform → 重新取 allowed_looks → look → exposure → gamma → 其他

整个「应用完整草稿」是**原子**的：任一步失败都会把 ``view_transform`` /
``look`` / ``exposure`` / ``gamma`` 恢复成本次应用前的值，绝不留半应用状态。
"""

from __future__ import annotations

import json
from typing import Any

from . import color_looks, errors, params

JSON_MARKER = "__TOON_JSON__"

#: 各绑定键 -> 生成赋值语句所用到的表达式
_VIEW_BINDINGS = {
    "view.exposure": "exposure",
    "view.gamma": "gamma",
    "view.view_transform": "view_transform",
    "view.look": "look",
}

#: 写入阶段顺序（需求 5）。``view_transform`` 必须在 ``look`` 之前，
#: 且两者之间要重新取得该视图的 allowed_looks。
_STAGE_VIEW_TRANSFORM = "view_transform"
_STAGE_LOOK = "look"
_STAGE_EXPOSURE = "exposure"
_STAGE_GAMMA = "gamma"
_STAGE_OTHERS = "others"

_ORDERED_STAGES: tuple[str, ...] = (
    _STAGE_VIEW_TRANSFORM,
    _STAGE_LOOK,
    _STAGE_EXPOSURE,
    _STAGE_GAMMA,
    _STAGE_OTHERS,
)

_BINDING_TO_STAGE = {
    "view.view_transform": _STAGE_VIEW_TRANSFORM,
    "view.look": _STAGE_LOOK,
    "view.exposure": _STAGE_EXPOSURE,
    "view.gamma": _STAGE_GAMMA,
}


def _float_literal(value: float) -> str:
    return repr(float(value))


def _str_literal(value: str) -> str:
    return repr(str(value))


def _stage_of(binding: str) -> str:
    if binding in _BINDING_TO_STAGE:
        return _BINDING_TO_STAGE[binding]
    if binding.startswith("glare."):
        return _STAGE_OTHERS
    raise errors.ToonTunerError(errors.PARAM_INVALID, f"未知绑定：{binding}")


# -- 只读 ---------------------------------------------------------------


def build_read_code() -> str:
    """读取曝光/辉光现值 + ``view_transform`` 候选 + 渲染设置。

    **这里刻意不再给出 look 候选**：look 是依赖 ``view_transform`` 的枚举，
    全局的 OCIO ``getLookNames()`` 会返回大量当前视图并不接受的名字，
    正是旧实现报 ``BLENDER_SCRIPT_ERROR`` 的根因。look 候选一律改由
    ``color_looks`` 的按视图探测提供。
    """
    return f"""
import bpy, json

scene = bpy.context.scene
vs = scene.view_settings
ng = bpy.data.node_groups.get({params.COMPOSITOR_GROUP_NAME!r})
glare = ng.nodes.get({params.GLARE_NODE_NAME!r}) if ng is not None else None


def _ocio_views():
    try:
        import PyOpenColorIO as ocio

        cfg = ocio.GetCurrentConfig()
        display = scene.display_settings.display_device
        return [str(v) for v in cfg.getViews(display)]
    except Exception:
        return []


def _rna_options(prop_owner, prop_name):
    try:
        prop = prop_owner.bl_rna.properties[prop_name]
        return [i.identifier for i in prop.enum_items if i.identifier != "NONE"]
    except Exception:
        return []


def _dedupe(values):
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _glare_value(name):
    if glare is None:
        return None
    socket = glare.inputs.get(name)
    if socket is None:
        return None
    value = getattr(socket, "default_value", None)
    if isinstance(value, str):
        return value
    try:
        return list(value)
    except TypeError:
        return value


views = _ocio_views()
if not views:
    views = _rna_options(vs, "view_transform")

r = scene.render
payload = {{
    "blender": bpy.app.version_string,
    "view": {{
        "exposure": vs.exposure,
        "gamma": vs.gamma,
        "view_transform": vs.view_transform,
        "look": vs.look,
    }},
    "view_options": {{
        "view.view_transform": _dedupe(views),
    }},
    "glare_present": glare is not None,
    "glare": {{
        "Type": _glare_value("Type"),
        "Quality": _glare_value("Quality"),
        "Threshold": _glare_value("Threshold"),
        "Smoothness": _glare_value("Smoothness"),
        "Strength": _glare_value("Strength"),
        "Size": _glare_value("Size"),
    }},
    "render": {{
        "engine": r.engine,
        "resolution_x": r.resolution_x,
        "resolution_y": r.resolution_y,
        "resolution_percentage": r.resolution_percentage,
        "film_transparent": r.film_transparent,
    }},
}}
print({JSON_MARKER!r} + json.dumps(payload, ensure_ascii=False, default=str))
""".strip()


# -- 写入 ---------------------------------------------------------------


def _write_line(binding: str, value: Any) -> str:
    if binding in _VIEW_BINDINGS:
        attr = _VIEW_BINDINGS[binding]
        if binding in ("view.view_transform", "view.look"):
            return f"vs.{attr} = {_str_literal(value)}"
        return f"vs.{attr} = {_float_literal(value)}"

    if binding.startswith("glare."):
        socket = binding.split(".", 1)[1]
        if socket in ("Type", "Quality"):
            return f"_glare_set({socket!r}, {_str_literal(value)})"
        return f"_glare_set({socket!r}, {_float_literal(value)})"

    raise errors.ToonTunerError(errors.PARAM_INVALID, f"未知绑定：{binding}")


def build_set_code(values: dict[str, Any]) -> str:
    """生成「一次性原子应用完整草稿」的代码。

    顺序固定为 view_transform → 校验 look → look → exposure → gamma → 其他；
    任一步抛错都会回滚 ``view_transform`` / ``look`` / ``exposure`` / ``gamma``，
    并在结构化输出里如实报告 ``failure`` 与 ``restored_to``。

    look 的**第一道**闸门是服务端（调用本代码之前就查能力表）；这里是**第二道**：
    在真正写 look 之前用 ``_allowed_looks()`` 复查一次当前视图的合法集合，
    兜住「服务端校验通过但 Blender 侧 OCIO 配置已变」这类竞态。
    """
    staged: dict[str, list[str]] = {name: [] for name in _ORDERED_STAGES}
    for param_id in sorted(values):
        spec = params.get(param_id)
        if spec is None:  # 理论上不会发生（已在 API 层校验）
            raise errors.ToonTunerError(errors.PARAM_INVALID, f"未知参数：{param_id}")
        value = values[param_id]
        if value is None:
            # None 只表示「这一项没有可写的值」（例：look 在当前视图下无等价项），
            # 绝不把它降级成字符串 "None" 写进 Blender。
            continue
        staged[_stage_of(spec.binding)].append(_write_line(spec.binding, value))

    look_value = _look_literal(values)

    body: list[str] = [
        "import bpy, json",
        "",
        "scene = bpy.context.scene",
        "vs = scene.view_settings",
        f"ng = bpy.data.node_groups.get({params.COMPOSITOR_GROUP_NAME!r})",
        f"glare = ng.nodes.get({params.GLARE_NODE_NAME!r}) if ng is not None else None",
        "",
        color_looks.LOOK_HELPERS,
        "",
        "def _glare_set(name, value):",
        "    if glare is None:",
        f"        raise RuntimeError('合成器节点组缺少 {params.GLARE_NODE_NAME}（辉光节点）')",
        "    socket = glare.inputs.get(name)",
        "    if socket is None:",
        "        raise RuntimeError('辉光节点缺少插座：' + str(name))",
        "    socket.default_value = value",
        "",
        "",
        "def _glare_value(name):",
        "    if glare is None:",
        "        return None",
        "    socket = glare.inputs.get(name)",
        "    if socket is None:",
        "        return None",
        "    value = getattr(socket, 'default_value', None)",
        "    if isinstance(value, str):",
        "        return value",
        "    try:",
        "        return list(value)",
        "    except TypeError:",
        "        return value",
        "",
        "",
        "class _InvalidLook(Exception):",
        "    def __init__(self, value, allowed):",
        "        Exception.__init__(self, 'invalid look: ' + str(value))",
        "        self.value = value",
        "        self.allowed = list(allowed)",
        "",
        "",
        "# 应用前的快照：任一步失败都要回到这里",
        "_snapshot = {",
        "    'view_transform': vs.view_transform,",
        "    'look': vs.look,",
        "    'exposure': vs.exposure,",
        "    'gamma': vs.gamma,",
        "}",
        "",
        "",
        "def _restore():",
        "    try:",
        "        vs.view_transform = _snapshot['view_transform']",
        "    except Exception:",
        "        pass",
        "    try:",
        "        vs.look = _snapshot['look']",
        "    except Exception:",
        "        pass",
        "    for _name in ('exposure', 'gamma'):",
        "        try:",
        "            setattr(vs, _name, _snapshot[_name])",
        "        except Exception:",
        "            pass",
        "",
        "",
        "_stage = 'init'",
        "_applied = False",
        "_failure = None",
        "_active_view = vs.view_transform",
        "try:",
    ]

    def _emit(stage: str, indent: str = "    ") -> None:
        lines = staged[stage]
        if not lines:
            return
        body.append(f"{indent}_stage = {stage!r}")
        for line in lines:
            body.append(f"{indent}{line}")
        if stage == _STAGE_VIEW_TRANSFORM:
            body.append(f"{indent}_active_view = vs.view_transform")

    _emit(_STAGE_VIEW_TRANSFORM)

    body.append(f"    _stage = {_STAGE_LOOK!r}")
    if look_value is None:
        body.append("    pass  # 草稿未包含 color.look")
    else:
        body.extend(
            [
                f"    _want_look = {look_value}",
                "    if _want_look is not None:",
                f"        _allowed, _how = _allowed_looks(vs, {color_looks.SENTINEL_LOOK!r})",
                "        if _want_look not in _allowed:",
                "            raise _InvalidLook(_want_look, _allowed)",
                "        vs.look = _want_look",
            ]
        )

    _emit(_STAGE_EXPOSURE)
    _emit(_STAGE_GAMMA)
    _emit(_STAGE_OTHERS)
    body.append("    _applied = True")

    body.extend(
        [
            "except _InvalidLook as exc:",
            "    _restore()",
            "    _failure = {",
            "        'kind': 'invalid_dependent_enum',",
            "        'parameter': 'color.look',",
            "        'value': exc.value,",
            "        'depends_on': {'color.view_transform': _active_view},",
            "        'allowed': exc.allowed,",
            "        'stage': _stage,",
            "    }",
            "except Exception as exc:",
            "    _restore()",
            "    _failure = {",
            "        'kind': 'write_failed',",
            "        'stage': _stage,",
            "        'type': type(exc).__name__,",
            "        'message': str(exc),",
            "    }",
            "",
            "_restored_to = {",
            "    'view_transform': vs.view_transform,",
            "    'look': vs.look,",
            "    'exposure': vs.exposure,",
            "    'gamma': vs.gamma,",
            "}",
            "",
            "out = {",
            "    'applied': _applied,",
            "    'failure': _failure,",
            "    'snapshot': dict(_snapshot),",
            "    'restored_to': _restored_to,",
            "    'restore_ok': _restored_to == _snapshot,",
            "    'view': {",
            "        'exposure': vs.exposure,",
            "        'gamma': vs.gamma,",
            "        'view_transform': vs.view_transform,",
            "        'look': vs.look,",
            "    },",
            "    'glare': {",
            "        'Threshold': _glare_value('Threshold'),",
            "        'Strength': _glare_value('Strength'),",
            "        'Size': _glare_value('Size'),",
            "        'Type': _glare_value('Type'),",
            "        'Quality': _glare_value('Quality'),",
            "        'Smoothness': _glare_value('Smoothness'),",
            "    } if glare is not None else None,",
            "}",
            f"print({JSON_MARKER!r} + json.dumps(out, ensure_ascii=False, default=str))",
        ]
    )
    return "\n".join(body)


def _look_literal(values: dict[str, Any]) -> str | None:
    """取出草稿里的 look 字面量（**必须是 value，不是 label**）。"""
    for param_id, value in values.items():
        spec = params.get(param_id)
        if spec is not None and spec.binding == "view.look":
            return None if value is None else _str_literal(value)
    return None


# -- 渲染预览 -----------------------------------------------------------
# 渲染代码已统一由 ``framing.build_render_code`` 生成（它同时负责取景模式、
# 临时预览相机的建立与恢复、以及中止判断），此处不再保留第二份实现。


# -- 解析 ---------------------------------------------------------------


def extract_json(captured_stdout: str) -> dict[str, Any]:
    """从捕获的 stdout 中取出最后一行 ``__TOON_JSON__`` 载荷。"""
    payload: str | None = None
    for line in captured_stdout.splitlines():
        if line.startswith(JSON_MARKER):
            payload = line[len(JSON_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse(
            "探针输出中未找到结构化结果行。",
            details={"stdout_tail": captured_stdout[-500:]},
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "探针输出的结构化结果不是合法 JSON。",
            details={"stdout_tail": captured_stdout[-500:]},
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("探针输出的结构不是对象。")
    return parsed


def failure_to_error(failure: dict[str, Any] | None) -> errors.ToonTunerError | None:
    """把 Blender 侧的结构化失败整理成稳定错误码。

    关键：``invalid_dependent_enum`` **绝不能**落成通用的
    ``BLENDER_SCRIPT_ERROR``（需求 6）。
    """
    if not isinstance(failure, dict) or not failure.get("kind"):
        return None
    if failure.get("kind") == "invalid_dependent_enum":
        return errors.ToonTunerError(
            errors.INVALID_DEPENDENT_ENUM,
            f"color.look = {failure.get('value')!r} 在当前 视图变换 下不合法。"
            f"允许取值：{failure.get('allowed')}",
            details={
                "parameter": failure.get("parameter", "color.look"),
                "value": failure.get("value"),
                "depends_on": failure.get("depends_on") or {},
                "allowed": failure.get("allowed") or [],
                "stage": failure.get("stage"),
            },
        )
    stage = failure.get("stage")
    detail = failure.get("message") or failure.get("type") or "未知写入错误"
    code = errors.PREVIEW_FAILED if stage == _STAGE_OTHERS else errors.BLENDER_SCRIPT_ERROR
    return errors.ToonTunerError(
        code,
        f"应用草稿在「{stage}」阶段失败：{detail}",
        details={"stage": stage, "blender_failure": failure},
    )
