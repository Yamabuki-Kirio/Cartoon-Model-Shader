"""API 响应与错误模型（Pydantic）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    hint: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: ErrorDetail


class HealthResponse(BaseModel):
    ok: Literal[True] = True
    service: str
    version: str


class BlenderStatusResponse(BaseModel):
    """`/api/blender/status`：始终 200，用 status 字段区分具体状态。"""

    ok: bool
    status: str
    target: str
    checked_at: str
    latency_ms: float | None = None
    error: ErrorDetail | None = None


class BlenderInfo(BaseModel):
    version: str = "unknown"
    file_name: str | None = None
    file_path: str = ""
    is_saved: bool = False


class SceneInfo(BaseModel):
    name: str = ""
    render_engine: str = "unknown"
    resolution: list[int] = Field(default_factory=list)
    frame_current: int = 0
    camera: str | None = None


class ObjectStats(BaseModel):
    total: int = 0
    mesh_count: int = 0
    visible_mesh_count: int = 0
    light_count: int = 0
    camera_count: int = 0


class RoleCandidate(BaseModel):
    name: str
    polygons: int = 0
    material_slots: int = 0
    visible: bool = False
    hide_render: bool = False


class SceneResponse(BaseModel):
    ok: Literal[True] = True
    status: str = "connected"
    target: str
    checked_at: str
    protocol: str
    blender: BlenderInfo
    scene: SceneInfo
    objects: ObjectStats
    role_candidates: list[RoleCandidate] = Field(default_factory=list)


# -- MVP-02：曝光调参 -------------------------------------------------------


class ParamSpecModel(BaseModel):
    id: str
    group: str
    label: str
    type: Literal["float", "enum"]
    target: str
    unit: str = ""
    note: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: list[str] | None = None
    options_dynamic: bool | None = None


class ParamGroup(BaseModel):
    name: str
    params: list[ParamSpecModel]


class ParamSchemaResponse(BaseModel):
    ok: Literal[True] = True
    schema_version: str
    groups: list[ParamGroup]


class BaselineResponse(BaseModel):
    """基线采集结果 + 紧跟其后的「基线预览」任务。

    需求：``POST /api/session/baseline`` 成功后**立即**用基线参数创建一次预览，
    因此响应里同时带上该任务的 ``job_id``。前端据它轮询，图片 load 成功后才
    显示「基线已建立，可开始调参」。
    """

    ok: Literal[True] = True
    baseline_id: str
    captured_at: str
    blender: str
    glare_present: bool
    values: dict[str, Any]
    options: dict[str, list[str]] = Field(default_factory=dict)
    render: dict[str, Any] = Field(default_factory=dict)
    #: 建立基线时锁定的取景快照（帧 / 相机 / transform / lens / shift / 角色包围盒）
    framing: dict[str, Any] = Field(default_factory=dict)
    preview_resolution: list[int] = Field(default_factory=list)
    job_id: str | None = None
    job_status: str | None = None
    preview_url: str | None = None


class RestoreResponse(BaseModel):
    ok: Literal[True] = True
    baseline_id: str
    verified: bool
    mismatches: list[dict[str, Any]] = Field(default_factory=list)
    readback: dict[str, Any]


# -- 取景（framing） --------------------------------------------------------


class FramingOptions(BaseModel):
    """预览取景方式与安全边距。

    ``mode`` 只允许四个白名单取值；``margin`` 为安全边距（0–0.4，0.15 = 四周各留 15%）。
    多余字段一律拒绝（422），不做静默兜底。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal[
        "current_camera", "auto_full_body", "auto_upper_body", "auto_headshot"
    ] = "current_camera"
    margin: float = Field(default=0.15, ge=0.0, le=0.4)


class BaselineRequest(BaseModel):
    """建立基线时可选地指定「首张预览」的取景方式。"""

    framing: FramingOptions | None = None


class PreviewSubmitRequest(BaseModel):
    """只接受「参数 id -> 取值」与取景方式；不接受任何代码。"""

    draft: dict[str, Any] = Field(default_factory=dict, description="完整草稿：参数 id 到取值的映射")
    framing: FramingOptions | None = Field(
        default=None, description="预览取景方式；缺省时用「当前相机」+ 15% 安全边距"
    )


class PreviewSubmitResponse(BaseModel):
    ok: Literal[True] = True
    job_id: str
    seq: int
    status: str
    framing: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    ok: bool = True
    job_id: str
    seq: int
    status: str
    created_at: str
    updated_at: str
    superseded: bool = False
    steps: list[str] = Field(default_factory=list)
    framing_request: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: ErrorDetail | None = None


class FramingContextResponse(BaseModel):
    """`/api/framing/context`：当前帧 / 相机 / 相机动画 / 角色是否完整入画。"""

    ok: Literal[True] = True
    protocol: str = "toon-tuner-framing/1"
    frame_current: int = 0
    frame_start: int | None = None
    frame_end: int | None = None
    camera: str | None = None
    camera_info: dict[str, Any] | None = None
    cameras: list[dict[str, Any]] = Field(default_factory=list)
    character: dict[str, Any] = Field(default_factory=dict)
    fit: dict[str, Any] | None = None
    resolution: list[int] = Field(default_factory=list)
    baseline: dict[str, Any] = Field(default_factory=dict)
    framing_modes: dict[str, Any] = Field(default_factory=dict)
