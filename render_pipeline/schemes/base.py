# -*- coding: utf-8 -*-
"""
渲染方案接口、参数定义与执行结果（PR #8 规格 §一）。

本模块**不导入 bpy**。灯光方案的实现体（``classic.py`` / ``volume_fog.py``）需要
``bpy``，但它们只在 Blender 进程内被调用；注册表、参数归一化与描述生成必须能在
纯 Python（服务端 / pytest）里工作 —— 否则 Web 层为了列个方案清单就得起 Blender。

因此这里的约定是：

* ``ParamSpec``   —— 一个可调参数的**服务端权威定义**（id / kind / 范围 / 默认值）。
* ``SchemeDescription`` —— 方案的只读描述，可直接序列化成 ``GET /api/render/schemes``
  的响应元素。**注意它不含任何路径 / 模块名 / 函数名。**
* ``Scheme`` —— 方案接口。子类实现 ``apply(scene, targets, parameters)`` 与
  ``restore(snapshot)``；参数校验走基类的 ``normalize``，子类只声明 ``PARAMS``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# -------------------------------------------------------------------------------------
#  稳定错误码（PR #8 规格 §一 / §三）
# -------------------------------------------------------------------------------------

#: 未知 / 非法方案 id。规格点名要求。
ERROR_INVALID_SCHEME = "RENDER_SCHEME_INVALID"
#: 参数集合、类型或范围不合法。
ERROR_INVALID_PARAM = "RENDER_SCHEME_PARAM_INVALID"
#: 恢复场景失败（规格 §三：必须报告这个稳定码，不允许静默）。
ERROR_RESTORE_FAILED = "RENDER_SCHEME_RESTORE_FAILED"
#: 应用方案失败。
ERROR_APPLY_FAILED = "RENDER_SCHEME_APPLY_FAILED"


class SchemeError(Exception):
    """方案层的稳定错误。

    ``code`` 是前端可依赖的**稳定错误码**（不随文案变化）；``message`` 面向用户。
    """

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


# -------------------------------------------------------------------------------------
#  参数定义
# -------------------------------------------------------------------------------------

#: 支持的参数类型。控制在三种 —— 前端据此渲染控件（规格 §六：number→数字框+滑块，
#: boolean→开关，enum→下拉框）。只读参数用 `readonly=True` 另标，不另开类型。
PARAM_KINDS = ("number", "boolean", "enum")


@dataclass(frozen=True)
class ParamSpec:
    """一个可调参数的权威定义。

    所有字段由**服务端**给出。客户端只能提交 ``id → 值``，提交不了这里任何一个字段。

    ``readonly=True`` 表示该参数只用于**展示说明**，不参与提交：
    本 PR 第一版里体积雾的三个灯光强度项就是只读的 —— 它们尚未经过真实 Blender
    标定，按规格「不得凭空开放」。
    """

    id: str
    kind: str
    label: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    readonly: bool = False

    def __post_init__(self) -> None:
        if self.kind not in PARAM_KINDS:
            raise SchemeError(
                ERROR_INVALID_PARAM,
                "参数 %r 的类型 %r 不在支持范围 %s" % (self.id, self.kind, PARAM_KINDS),
            )
        if self.kind == "enum" and not self.choices:
            raise SchemeError(
                ERROR_INVALID_PARAM, "enum 参数 %r 必须给出 choices" % self.id
            )
        if self.kind == "number":
            if self.minimum is None or self.maximum is None:
                raise SchemeError(
                    ERROR_INVALID_PARAM,
                    "number 参数 %r 必须给出 minimum 与 maximum（不凭空虚设范围就明说）"
                    % self.id,
                )
            if float(self.minimum) > float(self.maximum):
                raise SchemeError(
                    ERROR_INVALID_PARAM,
                    "参数 %r 的 minimum 大于 maximum" % self.id,
                )
        if not self.readonly:
            self._assert_default_in_range()

    def _assert_default_in_range(self) -> None:
        """默认值必须落在声明范围内 —— 否则「缺省得到固定默认值」这条就不成立。"""
        if self.kind == "number":
            v = float(self.default)
            if not (float(self.minimum) <= v <= float(self.maximum)):
                raise SchemeError(
                    ERROR_INVALID_PARAM,
                    "参数 %r 的默认值 %s 不在 [%s, %s] 内"
                    % (self.id, v, self.minimum, self.maximum),
                )
        elif self.kind == "enum":
            if self.default not in self.choices:
                raise SchemeError(
                    ERROR_INVALID_PARAM,
                    "参数 %r 的默认值 %r 不在 %s 内" % (self.id, self.default, self.choices),
                )

    # -- 校验与归一化 -------------------------------------------------------------

    def coerce(self, raw: Any) -> Any:
        """把一个客户端值转成该参数的规范类型；不合法则抛 ``SchemeError``。

        **不做隐式截断**：越界的数字一律拒绝，而不是夹到边界 —— 静默夹取会让用户
        以为参数生效了，实际跑的是另一个值。
        """
        if self.kind == "number":
            if isinstance(raw, bool) or raw is None:
                raise SchemeError(
                    ERROR_INVALID_PARAM, "参数 %r 需要数字，收到 %r" % (self.id, raw)
                )
            # ★ 只接受**真正的数字**，不接受数字字符串。
            #   规格要求「不合法就拒绝」，而不是尽力猜用户意图：把 "0.02" 悄悄
            #   转成 0.02 会掩盖调用方的类型错误，也让「提交体形状」不可信。
            if not isinstance(raw, (int, float)):
                raise SchemeError(
                    ERROR_INVALID_PARAM, "参数 %r 需要数字，收到 %r" % (self.id, raw)
                )
            v = float(raw)
            if v != v or v in (float("inf"), float("-inf")):
                raise SchemeError(
                    ERROR_INVALID_PARAM, "参数 %r 需要有限数字，收到 %r" % (self.id, raw)
                )
            if not (float(self.minimum) <= v <= float(self.maximum)):
                raise SchemeError(
                    ERROR_INVALID_PARAM,
                    "参数 %r 超出范围 [%s, %s]，收到 %s"
                    % (self.id, self.minimum, self.maximum, v),
                )
            return v

        if self.kind == "boolean":
            if isinstance(raw, bool):
                return raw
            # 只认明确的字面量，不认 "anything truthy" —— 避免 "false" 被当 True。
            if isinstance(raw, str):
                t = raw.strip().lower()
                if t in ("true", "1", "yes", "on"):
                    return True
                if t in ("false", "0", "no", "off", ""):
                    return False
            raise SchemeError(
                ERROR_INVALID_PARAM, "参数 %r 需要布尔值，收到 %r" % (self.id, raw)
            )

        # enum
        v = str(raw)
        if v not in self.choices:
            raise SchemeError(
                ERROR_INVALID_PARAM,
                "参数 %r 只接受 %s，收到 %r" % (self.id, list(self.choices), raw),
            )
        return v

    def to_public(self) -> dict[str, Any]:
        """服务端生成的公开描述（规格 §一 的 JSON 结构）。

        字段名沿用规格里的 JSON 键（``id`` / ``kind`` / ``label`` / ``minimum`` …），
        因为前端就按这套键渲染控件。
        """
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "default": self.default,
            "description": self.description,
            "readonly": bool(self.readonly),
        }
        if self.kind == "number":
            out["minimum"] = self.minimum
            out["maximum"] = self.maximum
            if self.step is not None:
                out["step"] = self.step
        if self.kind == "enum":
            out["choices"] = list(self.choices)
        return out


def param_specs_by_id(specs: Sequence[ParamSpec]) -> dict[str, ParamSpec]:
    """按 id 建索引；**重复 id 直接报错**（规格 §八：重复 ID 必须启动失败）。"""
    index: dict[str, ParamSpec] = {}
    for spec in specs:
        if spec.id in index:
            raise SchemeError(
                ERROR_INVALID_PARAM, "参数 id 重复：%r" % spec.id
            )
        index[spec.id] = spec
    return index


# -------------------------------------------------------------------------------------
#  执行结果与快照
# -------------------------------------------------------------------------------------


@dataclass
class SchemeResult:
    """应用一个方案的结果。

    ``lights`` 是本次**临时创建**的灯光对象清单（供结束后精确删除）；
    ``created_objects`` / ``created_materials`` 同理覆盖雾对象与材质。
    ``snapshot`` 交给 ``Scheme.restore`` 回滚。
    """

    scheme_id: str
    version: int
    parameters: dict[str, Any] = field(default_factory=dict)
    snapshot: Any = None
    lights: list[Any] = field(default_factory=list)
    created_objects: list[Any] = field(default_factory=list)
    created_materials: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        """进入任务响应的最小字段集（规格 §五「任务响应」）。"""
        return {
            "id": self.scheme_id,
            "version": self.version,
            "parameters": dict(self.parameters),
        }


# -------------------------------------------------------------------------------------
#  方案接口
# -------------------------------------------------------------------------------------


class Scheme:
    """一个固定灯光方案。

    子类**只**做两件事：声明 ``PARAMS``，实现 ``apply`` / ``restore``。
    参数校验、默认值填充、公开描述一律由基类统一处理 —— 这样「客户端不能自定义
    参数定义」是结构性保证，而不是每个方案各自记得做对。
    """

    #: 方案的稳定 id（注册表键）。
    id: str = ""
    #: 方案版本。**进入任务指纹**：改灯光数值但不改版本号，会让旧任务被错误复用。
    version: int = 1
    #: 展示名与说明（给 Web 方案卡片用）。
    label: str = ""
    description: str = ""
    #: 前端方案卡片上「推荐用途」一行。
    recommended_use: str = ""
    #: 是否包含体积雾（前端展示「是否包含体积雾」）。
    has_volume_fog: bool = False
    #: 性能影响提示（前端展示「性能影响」）。
    performance_note: str = ""
    #: 参数定义。顺序即前端展示顺序。
    PARAMS: tuple[ParamSpec, ...] = ()

    # -- 只读元信息 ---------------------------------------------------------------

    @classmethod
    def param_index(cls) -> dict[str, ParamSpec]:
        return param_specs_by_id(cls.PARAMS)

    @classmethod
    def adjustable_params(cls) -> tuple[ParamSpec, ...]:
        """可提交的参数（readonly 的只用于展示，不进提交集合）。"""
        return tuple(p for p in cls.PARAMS if not p.readonly)

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """缺省参数 —— 规格 §八 要求「缺省参数得到固定默认值」。"""
        out: dict[str, Any] = {}
        for spec in cls.PARAMS:
            if spec.readonly:
                # 只读项也给出展示值，但**不进**提交体。
                continue
            out[spec.id] = spec.default
        return out

    @classmethod
    def describe(cls, *, recommended: bool = False) -> SchemeDescription:
        return SchemeDescription(
            id=cls.id,
            version=cls.version,
            label=cls.label,
            description=cls.description,
            recommended=recommended,
            parameters=tuple(cls.PARAMS),
            recommended_use=cls.recommended_use,
            has_volume_fog=cls.has_volume_fog,
            performance_note=cls.performance_note,
            adjustable_count=len(cls.adjustable_params()),
        )

    # -- 参数归一化 ---------------------------------------------------------------

    @classmethod
    def normalize(cls, raw: Mapping[str, Any] | None) -> dict[str, Any]:
        """把客户端提交的原始参数归一化成**完整、合法**的参数字典。

        规则（规格 §五「校验顺序」第 4–5 步）：

        1. 未知参数**一律拒绝**，不能忽略；
        2. 只读参数不接受提交（它们没标定，不能凭空开放）；
        3. 缺省参数填固定默认值；
        4. 逐个按类型与范围校验，越界不夹取。
        """
        raw = dict(raw or {})
        index = cls.param_index()
        unknown = sorted(set(raw) - set(index))
        if unknown:
            raise SchemeError(
                ERROR_INVALID_PARAM,
                "方案 %s 不接受参数：%s" % (cls.id, unknown),
                details={"scheme": cls.id, "unknown": unknown},
            )
        readonly_given = sorted(
            k for k in raw if index[k].readonly
        )
        if readonly_given:
            raise SchemeError(
                ERROR_INVALID_PARAM,
                "方案 %s 的参数 %s 当前为只读（未经真实 Blender 标定，不开放调整）"
                % (cls.id, readonly_given),
                details={"scheme": cls.id, "readonly": readonly_given},
            )
        out: dict[str, Any] = {}
        for spec in cls.PARAMS:
            if spec.readonly:
                continue
            if spec.id in raw:
                out[spec.id] = spec.coerce(raw[spec.id])
            else:
                out[spec.id] = spec.default
        return out

    # -- 场景生命周期（子类实现） --------------------------------------------------

    def apply(self, scene: Any, targets: Sequence[Any], parameters: Mapping[str, Any]) -> SchemeResult:
        raise NotImplementedError

    def restore(self, snapshot: Any) -> None:
        raise NotImplementedError


# -------------------------------------------------------------------------------------
#  方案描述（对外结构）
# -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemeDescription:
    """``GET /api/render/schemes`` 里的一项。

    **绝不含**：本机路径、脚本路径、Python 模块路径、可执行命令（规格 §五）。
    连实现类名都不给 —— 前端不需要知道 ``ClassicScheme`` 还是别的。
    """

    id: str
    version: int
    label: str
    description: str
    recommended: bool
    parameters: tuple[ParamSpec, ...]
    recommended_use: str = ""
    has_volume_fog: bool = False
    performance_note: str = ""
    adjustable_count: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "recommended": bool(self.recommended),
            # 前端方案卡片的展示项（规格 §六）。
            "recommended_use": self.recommended_use,
            "has_volume_fog": bool(self.has_volume_fog),
            "performance_note": self.performance_note,
            "adjustable_count": int(self.adjustable_count),
            "parameters": [p.to_public() for p in self.parameters],
        }
