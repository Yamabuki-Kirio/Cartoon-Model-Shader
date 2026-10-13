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
    #: 建立基线那一刻的工程脏标记（只有 ``dirty`` / ``file_name``，**不含绝对路径**）。
    #: 与预览结果里的 ``project`` 对应，供页面判断「原本干净、预览后才变脏」。
    #: **新增的可选字段**：旧页面不看它，因此 L0 响应契约不变。
    project: dict[str, Any] = Field(default_factory=dict)
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
    """预览取景方式、安全边距与隔离材质。

    ``mode`` 只允许四个白名单取值；``margin`` 为安全边距（0–0.4，0.15 = 四周各留 15%）；
    ``isolate_material`` 给出材质名时做隔离预览（渲染时临时隐藏不使用该材质的网格对象，
    渲染后逐个写回原值）；``None`` / 空串表示不隔离。
    多余字段一律拒绝（422），不做静默兜底。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal[
        "current_camera", "auto_full_body", "auto_upper_body", "auto_headshot"
    ] = "current_camera"
    margin: float = Field(default=0.15, ge=0.0, le=0.4)
    isolate_material: str | None = Field(
        default=None,
        max_length=200,
        description="隔离预览的材质名；None / 空串表示不隔离。",
    )


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


# -- MVP-03：本地预设 -------------------------------------------------------


class PresetParameter(BaseModel):
    """单个参数在预设里的记录（与 run 记录同构）。

    * ``configured_value`` —— 配置值（可能来自旧预设的显示标签）；
    * ``effective_value``  —— 真正写进 Blender 的真实 identifier；
    * ``active``           —— 是否参与应用；``false`` 只作记录。
    """

    model_config = ConfigDict(extra="forbid")

    configured_value: Any = None
    effective_value: Any = None
    active: bool = True


class PresetSaveRequest(BaseModel):
    """新建 / 覆盖保存预设。

    ``parameters`` 的值可以是 ``PresetParameter`` 三元组，也可以是**裸取值**
    （此时视作 ``configured = effective = 该值`` 且 ``active = true``）。

    ``schema`` 可带可不带：带了就用它判定版本（旧版本会得到明确的
    ``PRESET_SCHEMA_UNSUPPORTED``，而不是被字段校验挡成笼统的 422）；
    不带则由服务端写入当前版本。字段名用 ``schema_version`` 避开 Pydantic
    基类上的 ``schema`` 属性，对外仍以 ``schema`` 出现。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    schema_version: str | None = Field(default=None, alias="schema")
    pipeline_mode: str = "faithful"
    framing_mode: str = "current_camera"
    framing_margin: float = 0.15
    preview_quality: str = "standard"
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: v4（可选）：参数面草稿（``v4 参数 id -> 裸取值``）。
    #: 含 Cel 组名与材质名这类**工程事实**，因此没有 id 白名单，只做结构校验；
    #: 旧客户端不带这个字段，行为完全不变。
    surface: dict[str, Any] = Field(default_factory=dict)


class PresetRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class PresetDuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None


class PresetSummary(BaseModel):
    preset_id: str
    name: str
    schema_version: str = Field(alias="schema", serialization_alias="schema")
    created_at: str
    updated_at: str
    pipeline_mode: str
    framing_mode: str
    framing_margin: float
    preview_quality: str
    parameter_count: int
    active_count: int
    #: v4 参数面草稿条数（0 = 这份预设不含 Cel / 材质草稿）。
    surface_count: int = 0

    model_config = ConfigDict(populate_by_name=True)


class PresetDetail(PresetSummary):
    parameters: dict[str, PresetParameter] = Field(default_factory=dict)
    #: v4 参数面草稿：``参数 id -> 裸取值``（没有 configured/effective 三元组 ——
    #: 它的取值在写入时就是最终值，不存在需要迁移的显示标签）。
    surface: dict[str, Any] = Field(default_factory=dict)


class PresetSkip(BaseModel):
    file: str
    code: str
    message: str


class PresetListResponse(BaseModel):
    """``GET /api/presets``：预设列表 + 运行设置白名单。

    ``storage`` 是**未展开**的展示写法（如 ``%LOCALAPPDATA%\\CartoonModelShader\\presets``），
    绝不含本机绝对路径。
    """

    ok: Literal[True] = True
    protocol: str = "toon-tuner-presets/1"
    storage: str = ""
    presets: list[PresetSummary] = Field(default_factory=list)
    skipped: list[PresetSkip] = Field(default_factory=list)
    quality_tiers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pipeline_modes: list[str] = Field(default_factory=list)
    framing_modes: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)


class PresetDetailResponse(BaseModel):
    ok: Literal[True] = True
    preset: PresetDetail


class PresetDeleteResponse(BaseModel):
    ok: Literal[True] = True
    deleted: PresetSummary


# -- 应用到工程（commit）----------------------------------------------------


class CommitPrepareRequest(BaseModel):
    """准备保存：只校验，不写盘。默认「另存为」。

    ``extra="forbid"``：备份路径等落盘细节**只由服务端**在准备阶段算出并冻结，
    客户端提交 ``backup_path`` 一律 422 拒绝 —— 用户要确认的必须是服务端真的
    会写到的那条路径，不能是请求里带来的另一条。
    """

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
    #: v4（可选）：第一次 prepare 返回的确认单号。第二次确认**必须**原样携带：
    #: 服务端据此沿用第一次已展示给用户的备份路径（两步之间跨过整秒边界也不会漂移），
    #: 并复核基线 / 草稿 / 模式 / 目标都没变。缺失或对不上即拒绝。
    confirmation_id: str | None = None
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
    #: 本次准备的确认单号。确认覆盖时必须随第二次 prepare 回传，服务端据此
    #: 沿用已展示的备份路径并复核各项未变。不需要确认的流程也会给出（值为当次单号）。
    confirmation_id: str = ""


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


# -- 渲染工作流（HTTP 编排层） -------------------------------------------


class RenderSettingsRequest(BaseModel):
    """提交渲染任务：只接受 opaque model_id + 白名单设置；绝不接受路径/脚本/命令。

    ``mode`` 仅 faithful | enhanced；``auto_frame`` 是否自动构图。其余字段一律 422 拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    mode: Literal["faithful", "enhanced"] = "faithful"
    auto_frame: bool = False


class ModelInfoResponse(BaseModel):
    ok: Literal[True] = True
    models: list[dict[str, Any]] = Field(default_factory=list)


class RenderJobResponse(BaseModel):
    ok: Literal[True] = True
    job_id: str
    model_id: str
    model_name: str
    fingerprint: str
    state: str
    step: int
    stage: str
    step_count: int
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    is_terminal: bool = False
    materials: list[dict[str, Any]] = Field(default_factory=list)
    confirm_url: str | None = None
    confirmed_count: int = 0
    required_count: int = 0
    #: 资源治理：是否被用户固定、占用字节数、保留状态。
    pinned: bool = False
    disk_bytes: int = 0
    retention_state: str = "kept"
    #: 运行终态（run_contract 词表：success / rejected / diagnostic / failed）。
    #: 终态任务才有值；取消（cancelled）不是正式终态，故为 None。
    #: 前端据此区分四种终态 —— 不再把 rejected/diagnostic 显示成「渲染失败」。
    outcome: str | None = None
    #: 是否可交付。**只有 success 为 True**（规格 §3.3 硬约束）。
    shippable: bool = False
    #: 稳定错误码（非成功时）。前端据此给出可操作提示，不展示裸 traceback。
    error_code: str | None = None
    #: 兼容降级判定：未收到 render.done，但退出码 0 且确有成品图（规格 §3.2）。
    degraded: bool = False
    #: 取消原因（仅 cancelled 有值；供结果区说明为什么被取消）。
    cancel_reason: str | None = None
    logs: list[str] | None = None
    artifacts: list[dict[str, Any]] | None = None


class RenderMaterialsResponse(BaseModel):
    ok: Literal[True] = True
    job_id: str
    materials: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_count: int = 0
    required_count: int = 0
    confirm_url: str | None = None
    waiting: bool = False


class ArtifactListResponse(BaseModel):
    ok: Literal[True] = True
    job_id: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class RenderEnvironmentResponse(BaseModel):
    """渲染环境只读诊断（规格 §5）。

    ``ready`` 为 False 时 ``blockers`` 列出 error 级检查项 id，前端据此禁用
    「开始渲染」并逐项说明原因。**响应里不含任何本机绝对路径。**
    """

    ok: Literal[True] = True
    ready: bool = False
    blockers: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)


class RenderLogResponse(BaseModel):
    ok: Literal[True] = True
    job_id: str
    logs: list[str] = Field(default_factory=list)
