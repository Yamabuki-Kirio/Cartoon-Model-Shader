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
    #: 枚举项一律 ``{"value": <Blender 真实 identifier>, "label": <显示文本>}``
    options: list[dict[str, str]] | None = None
    options_dynamic: bool | None = None
    #: 本参数合法取值所依赖的参数 id（例：color.look 依赖 color.view_transform）
    depends_on: str | None = None


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
    options: dict[str, list[Any]] = Field(default_factory=dict)
    render: dict[str, Any] = Field(default_factory=dict)
    #: 建立基线时锁定的取景快照（帧 / 相机 / transform / lens / shift / 角色包围盒）
    framing: dict[str, Any] = Field(default_factory=dict)
    #: ``view_transform`` -> ``[{value, label}]``：Blender 真实接受的 look 档位
    look_map: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    preview_resolution: list[int] = Field(default_factory=list)
    #: v4 参数面基线（只读拓扑探针 + 递归 schema + 三层指纹）。
    #: **新增的可选字段**：旧页面不看它，因此 L0 响应契约不变。
    surface: dict[str, Any] | None = None
    job_id: str | None = None
    job_status: str | None = None
    preview_url: str | None = None


class RestoreResponse(BaseModel):
    ok: Literal[True] = True
    baseline_id: str
    verified: bool
    mismatches: list[dict[str, Any]] = Field(default_factory=list)
    readback: dict[str, Any]
    #: 恢复过程中发生的依赖迁移（例：look 因视图变换变化而被规范化）
    normalized: list[dict[str, Any]] = Field(default_factory=list)


# -- 依赖枚举（view_transform -> look） -------------------------------------


class ColorLookOption(BaseModel):
    value: str
    label: str


class ColorLooksResponse(BaseModel):
    """``/api/color/looks``：某个视图变换下 Blender **真正接受**的 look 档位。"""

    ok: Literal[True] = True
    protocol: str = "toon-tuner-color-looks/1"
    source: str = "baseline"
    current_view_transform: str | None = None
    view_transform: str | None = None
    options: list[ColorLookOption] = Field(default_factory=list)
    look_map: dict[str, list[ColorLookOption]] = Field(default_factory=dict)
    probe: dict[str, Any] | None = None
    restored: dict[str, Any] | None = None


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


class V4PreviewRequest(BaseModel):
    """v4 预览：草稿里可以同时出现 L0 参数 id 与 Cel 参数 id。

    * L0（``color.*`` / ``glow.*``）走既有校验路径；
    * Cel（``cel.*``）走递归 schema + 通用执行器；
    * 两者编译进**同一个任务**，因此只渲染一次、一起回滚。

    ``expected_structure_hash`` 是客户端手里的结构指纹：与当前基线不一致时在
    提交阶段就拒绝（不产生任务）。真正的身份/结构闸门在任务里再跑一遍。
    """

    model_config = ConfigDict(extra="forbid")

    draft: dict[str, Any] = Field(default_factory=dict, description="完整草稿：参数 id 到取值的映射")
    framing: FramingOptions | None = None
    expected_structure_hash: str | None = None


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
    #: v4 任务标识（纯 L0 任务不带此字段）
    kind: str | None = None
    #: 值层外部改动：只报告，不作废草稿与令牌
    external_changes: list[dict[str, Any]] = Field(default_factory=list)
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


# -- 预设（presets）---------------------------------------------------------
# 注意：这两个模型**没有任何路径字段**。预设目录与文件名一律由服务端决定，
# 因此「路径穿越」在接口契约层面就不存在，而不是靠运行期过滤。


class PresetSaveRequest(BaseModel):
    """保存预设：只需要名称 + 完整草稿。多余字段一律 422 拒绝。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="预设名称，支持中文；只作为显示名与去重键，不参与路径拼接")
    draft: dict[str, Any] = Field(
        default_factory=dict, description="完整草稿：参数 id 到取值的映射"
    )
    framing: FramingOptions | None = Field(
        default=None, description="保存时使用的取景方式，仅供复现预览构图"
    )


class PresetItem(BaseModel):
    id: str
    name: str
    created_at: str | None = None
    updated_at: str | None = None
    blender: str | None = None
    baseline_id: str | None = None
    parameter_count: int | None = None
    framing: dict[str, Any] = Field(default_factory=dict)


class PresetListResponse(BaseModel):
    """预设列表。

    刻意**不回传预设目录的绝对路径**：仓库的脱敏约定是「对外响应不含本机绝对路径」，
    而预设目录属于本机实现细节（保存路径的绝对路径只出现在「应用到工程」的确认信息里，
    那是任务书明确要求展示的例外）。
    """

    ok: Literal[True] = True
    protocol: str = "toon-tuner-presets/1"
    presets: list[PresetItem] = Field(default_factory=list)


class PresetSaveResponse(BaseModel):
    ok: Literal[True] = True
    id: str
    name: str
    created_at: str
    updated_at: str
    parameter_count: int
    updated: bool = Field(description="True = 覆盖了同名预设；False = 新建")


# -- 应用到工程（commit）----------------------------------------------------


class CommitPrepareRequest(BaseModel):
    """准备保存：只校验，不写盘。默认「另存为」。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["save_as", "overwrite"] = "save_as"
    draft: dict[str, Any] = Field(default_factory=dict)
    target_path: str | None = Field(
        default=None,
        description="仅 save_as 使用：绝对路径且以 .blend 结尾；overwrite 由 Blender 当前工程决定",
    )
    confirm: bool = Field(
        default=False, description="覆盖已有文件必须为 True（先看清绝对路径与备份路径再确认）"
    )
    #: v4（可选）：参数面草稿。传了就一并绑进确认令牌，篡改即拒绝。
    surface_draft: dict[str, Any] | None = None
    #: v4（可选）：客户端持有的结构指纹。与基线不一致时在 prepare 阶段就拒绝。
    structure_hash: str | None = None


class CommitPrepareResponse(BaseModel):
    ok: Literal[True] = True
    token: str = Field(description="一次性短效确认令牌，绑定基线/草稿/模式/目标路径")
    mode: str
    target_path: str
    backup_path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    baseline_id: str
    draft_hash: str
    parameter_count: int = 0
    #: v4：绑定的参数面草稿条数（0 = 本次没绑）
    surface_parameter_count: int = 0
    #: v4：绑定的结构指纹（None = 本次没绑）
    structure_hash: str | None = None
    normalized: list[dict[str, Any]] = Field(default_factory=list)
    expires_at: str
    expires_in_seconds: int
    confirmation_required: bool = False


class CommitRequest(BaseModel):
    """执行保存：消费令牌，逐项比对绑定。"""

    model_config = ConfigDict(extra="forbid")

    token: str
    mode: Literal["save_as", "overwrite"] = "save_as"
    draft: dict[str, Any] = Field(default_factory=dict)
    target_path: str | None = None
    surface_draft: dict[str, Any] | None = None
    structure_hash: str | None = None


class CommitResponse(BaseModel):
    ok: Literal[True] = True
    mode: str
    target_path: str
    backup_path: str | None = None
    saved: bool
    status: dict[str, Any] = Field(default_factory=dict)
    steps: list[str] = Field(default_factory=list)
    normalized: list[dict[str, Any]] = Field(default_factory=list)
    baseline_id: str | None = None
    baseline_refreshed: bool = False
    saved_at: str
    warnings: list[str] = Field(default_factory=list)
