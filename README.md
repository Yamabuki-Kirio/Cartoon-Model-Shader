# Cartoon-Model-Shader

> PMX 材质适配与一键渲染组件见 [`render_pipeline/`](render_pipeline/README.md)。该组件与浏览器调参工具保持独立边界，共享同一仓库维护。

> **自用项目，不开源。**

做 MMD 模型三渲二的时候下载了很多插件 —— Autocel 智能卡通化着色器、Cycles Render Engine、glTF 2.0 format、MCP for Blender、MiaoboxNode、MikuMikuRig、MMD Tools、MMD Tools Append、MMD 自动换头、mmd_kafei_tools、Pose Library、卡渲秘咒。顺序繁多，每次从零开始做出来的效果都不一样，所以做了个脚本自动把模型渲染成我理想中的情况，现在再做可视化界面方便调参数。

MMD 刚入门，渲染水平很低，见谅。只用于自用。

> 需求全文见 [`docs/需求文档.md`](docs/需求文档.md)。

---

## 关键设计约束

| 约束 | 说明 |
|---|---|
| 平台 | 仅 Windows |
| 界面 | 普通浏览器打开本地 Web UI，不封装桌面程序 |
| Blender 通信 | 直接复用现有 Blender MCP `127.0.0.1:9876` TCP JSON 接口（请求以换行结束；**响应不带分隔符，需累积缓冲 + 增量解析**），**不另装通信插件** |
| 常驻会话 | 复用同一个 Blender 会话，参数改动只触发重渲，不重启进程（冷启动约 5 秒是大头） |
| 无污染预览 | 每次预览**从内存基线恢复后应用完整草稿**，不在上一次结果上叠加 |
| 等待方式 | **不使用固定 `sleep`**；以任务 ID + 状态查询 / 推送等待 Blender 完成 |
| 首版范围 | 当前场景中的**一个主要角色**；角色须处于管线支持的基准坐标 |
| 参数面 | 界面由**静态 schema 与运行时能力探测共同决定**（不设固定项数）；MVP 先开放高价值参数（**当前已开放 10 项 L0**，见下文） |
| 预设存储 | 落在用户目录 `%LOCALAPPDATA%\CartoonModelShader\presets`，**不进仓库**；写入前拒绝任何本机路径、模型/贴图路径与凭据（不做静默清洗） |

## 三层架构

```
┌─────────────────────┐   HTTP + WebSocket    ┌──────────────────────────┐
│   浏览器 Web UI      │ ◀───────────────────▶ │   本地控制服务            │
│  HTML / CSS / JS     │                       │  · 校验 schema/类型/范围   │
│  · 参数面板           │                       │  · 任务排队 / 取消 / 去抖  │
│  · 中央预览 / A-B     │                       │  · 预设 / 缩略图 / 运行记录│
│  · Cel 色阶编辑器     │                       │  · 推送状态与最新预览图    │
└─────────────────────┘                       └────────────┬─────────────┘
                                                           │ TCP JSON
                                                           │ 127.0.0.1:9876
                                              ┌────────────▼─────────────┐
                                              │   Blender（常驻会话）      │
                                              │  · 读场景 / 建管线 / 采参数│
                                              │  · 基线恢复 / 应用参数集   │
                                              │  · 预览 / 正式渲染         │
                                              └──────────────────────────┘
```

- **前端**：本地 Web 界面，参数界面由静态 schema 与运行时能力探测共同生成，而非把控件写死。
- **本地控制服务**：管理连接、校验参数、排队去抖、保存预设与运行记录、推送状态。默认仅监听 localhost，启动时生成随机令牌校验请求。
- **Blender 桥接层**：在现有 MCP 接口之上增加会话协议层（协议版本、任务 ID、能力查询、错误码、状态查询）。接口若不能主动推送，则由控制服务短间隔**非阻塞轮询**。

## 重算层级

| 层级 | 内容 | 目标反馈 | 触发方式 |
|---|---|---|---|
| **L0** | 辉光、曝光、Gamma、View Transform、Look、Tonemapping | 0.75–1.5 s | 停止调整 250–400 ms 后**自动预览**（**已实现**：10 项，Tonemapping 该组内不存在故未开放） |
| **L1** | Cel 色阶、Emission、描边宽度/颜色、世界强度、采样、阴影 | 2–3 s | 滑块释放 / 颜色确认后 |
| **L2** | 材质归类、基础贴图、色彩空间、分辨率 | 约 5 s | 「应用并预览」，带进度 |
| **L3** | 管线模式、重新追加资源、灯光装置、相机策略 | 7–8 s | 独立「重建管线」操作 |

## 状态模型

`原始工程状态` → 用户点「初始化管线」→ `工具基线状态` → 调参产生 `草稿参数` → 预览从基线恢复 + 应用完整草稿得 `预览状态` → 用户点「应用到工程」得 `已提交状态`。

`保存预设` 与 `应用到工程` **已实现**（保存功能落地）：另存为是默认方式；覆盖当前 `.blend` 时
必须看清绝对路径、二次确认，并在同目录先生成带时间戳的可恢复备份。不管哪种方式，**回读逐项校验
通过之前绝不写盘**。

> ⚠ **自动测试已完成，真实 Blender 验收仍是待办**：本功能的验收依赖
> 「真实 Blender + 真实工程」的人工步骤（见下文「真实浏览器验收」与 `docs/接口文档.md`），
> 目前只在假 `bpy` 桩上验证过；覆盖真实工程前请先自行备份。

## 目录结构

```
Cartoon-Model-Shader/
├── docs/                            设计与需求文档
├── docs/                            设计与需求文档
│   ├── 需求文档.md
│   ├── 技术设计.md
│   ├── 技术方案_v4.md                 v4 参数面技术方案（含 §8.4 提交 3A 落地记录）
│   ├── 接口文档.md                    全部接口的唯一权威（含令牌、保存流程、v4 过渡接口）
│   ├── 安全检查.md
│   └── AI交付_第一步实施方案.md        MVP-01 任务书
├── reference/                       参考数据（参数面 schema 等）
│   ├── 参数面清单.json
│   ├── 一键卡通渲染.py
│   ├── 一键渲染_通用驱动.py
│   └── 审计_本次执行.md
├── src/
│   ├── server/                      本地控制服务（FastAPI）
│   │   ├── app.py                   路由 + 静态托管 + 生命周期
│   │   ├── config.py                非敏感配置（仅回环）
│   │   ├── blender_mcp.py           TCP JSON 客户端（累积缓冲 + 增量解析）
│   │   ├── scene_probe.py           只读场景探针模板 + 解析
│   │   ├── params.py                L0 参数白名单（类型/范围/绑定）
│   │   ├── framing.py               预览取景：只读构图诊断 + 临时预览相机 + 基线失效比对
│   │   ├── color_looks.py           依赖枚举：view_transform → 合法 look 的探测/迁移/校验
│   │   ├── presets.py               本地预设：存储 / 校验 / 敏感内容扫描 / 加载成草稿
│   │   ├── blender_ops.py           由白名单生成只读/写入/渲染代码
│   │   ├── binder.py                高层绑定：读基线 / 写草稿 / 渲染预览 / 通用执行器入口
│   │   ├── session.py               内存基线 + 任务队列（旧任务自动作废）+ v4 任务路径
│   │   ├── security.py              本机会话令牌（所有写接口的统一闸门）
│   │   ├── presets.py               预设存储（原子写入；文件名由服务端派生）
│   │   ├── project_ops.py           工程读/写固定模板 + 同目录时间戳备份
│   │   ├── commit.py                应用到工程：确认令牌 / 回读校验 / 备份 / 保存
│   │   ├── frontend.py              /next 构建产物托管（令牌只注入 HTML、越界 404）
│   │   ├── surface_probe.py         只读拓扑探针（脱敏；v4 的探测原料）
│   │   ├── surface_service.py       v4 参数面状态：身份 + 结构指纹 + schema + 基线值
│   │   ├── surface/                 v4 参数面（纯函数式）
│   │   │   ├── binding.py           结构化 binding + 字段白名单（注入防线）
│   │   │   ├── identity.py          身份/结构/值三层指纹与比对
│   │   │   ├── schema.py            递归节点模型
│   │   │   ├── cel.py               Cel 色阶适配器（能力驱动，探不到即降级）
│   │   │   ├── draft.py             完整草稿校验（唯一入口）
│   │   │   └── executor.py          由固定模板生成写入/回读代码（原子、可回滚）
│   │   ├── models.py                API 模型
│   │   ├── errors.py                稳定错误码
│   │   └── redact.py                诊断脱敏
│   └── web/                         旧页面（原生 HTML/CSS/JS；提交 5 才会下线）
├── web/                             v4 工作台（Vite + Preact + TypeScript）
│   ├── index.html                   构建入口（含 <!--TOON_TUNER_TOKEN--> 占位）
│   ├── tooling/                     构建侧工具与断言（**不叫 build/**：`.gitignore` 忽略 `build/`）
│   │   ├── token-placeholder.ts     令牌占位插件 + 「令牌形态」判据
│   │   ├── dev-token-bridge.ts      仅开发期：/__dev/token 把后端注入的令牌转交页面
│   │   ├── dev-token-bridge.test.ts 令牌桥：提取/失败原因/不缓存
│   │   └── dist-artifacts.test.ts   产物断言 + 仓库卫生（文件不得被 .gitignore 忽略）
│   ├── scripts/verify-dist.mjs      构建产物校验（npm run build 的最后一步）
│   ├── src/{api,schema,state,components,features,styles,testing}/
│   │   ├── api/dev-token.ts         仅开发期：把 dev server 的令牌装进与生产同一个全局变量
│   │   └── testing/setup-dom.ts     测试环境保真：补 jsdom 缺失的 onpointer* IDL 属性
│   └── dist/                        构建产物（**不入库**）
├── tests/                           假 MCP + 假 bpy 桩 + pytest 用例
│   ├── fake_bpy.py                  可执行 bpy 桩（复刻 view_frame / ops.wm 保存等真实行为）
│   ├── support.py                   公共测试支撑：统一携带会话令牌的 TestClient
│   ├── conftest.py                  公共 fixture（预设目录一律指向 tmp_path）
│   ├── test_framing.py              取景/构图/临时相机/基线失效用例
│   ├── test_color_looks.py          依赖枚举：能力探测/value-label/写入顺序/原子化/迁移用例
│   ├── test_presets.py              本地预设：目录解析/文件名净化/校验/敏感内容/CRUD/API 用例
│   ├── test_presets.py              预设：名称校验/更新/越界/依赖枚举/路径穿越/原子写入失败
│   ├── test_commit.py               保存：目标校验/确认令牌/备份/回读/失败不落盘
│   ├── test_security_token.py       写接口令牌矩阵 + 不可注入契约
│   ├── test_surface_probe.py        只读拓扑探针：脱敏 + 结构签名
│   ├── test_surface_identity.py     三层指纹：身份/结构/值
│   ├── test_surface_cel.py          Cel 适配器 + 通用执行器（整体写入/回滚）
│   ├── test_v4_surface_api.py       v4 竖切端到端：基线/预览/失效/失败恢复/保存绑定
│   ├── test_next_frontend.py        /next 托管：并存迁移/未构建诊断/令牌只进 HTML/路径穿越
│   └── browser_check.mjs            真实浏览器验收（本机 Chrome/Edge + CDP）
├── .github/workflows/test.yml       CI：pytest（3.11/3.12）与 web（Node 22）两个独立 job
├── config.example.json
├── requirements.txt
├── pytest.ini
└── README.md
```

## MVP 最小闭环

```
连接 9876 → 读取当前场景 → 建立基线 → 调整曝光/辉光
  → 自动预览 → 恢复基线 → 保存预设
```

## 本地运行

需要 Python 3.11+。

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn src.server.app:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>（旧页面）或 <http://127.0.0.1:8765/next>（v4 工作台）。

### v4 工作台（`/next`）需要先构建

`/next` 由 FastAPI 托管 **Vite 构建产物**。构建产物**不入库**（`web/dist` 在 `.gitignore` 里），
因此拉下代码后要先构建一次；未构建时 `/next` 会返回 `503 FRONTEND_NOT_BUILT`
并附上构建步骤（**不会**悄悄回退到旧页面）：

```powershell
cd web
npm ci
npm run build
```

开发时可以用 Vite dev server（`npm run dev`，默认 5173，`/api` 已代理到 8765），
但**生产入口始终是 FastAPI**：`dist` 里的 `index.html` 只有 `<!--TOON_TUNER_TOKEN-->` 占位，
令牌由服务端在响应时注入，因此磁盘上的产物里永远没有令牌。

开发期有个绕不开的落差：页面由 dev server 提供，而令牌只存在于 FastAPI 的响应里，
于是**所有写请求都会 401**。为此 dev server 暴露了一个 `apply: "serve"` 的令牌桥
（`GET /__dev/token`）：它代取一次 FastAPI 页面、把注入的令牌转交前端，
前端仍然只从 `window.__TOON_TUNER_TOKEN__` 这一处读 —— 生产与开发**共用同一条读取路径**，
所以「开发能用、生产 401」这类差异不会藏起来。该插件不进构建产物（`dist` 里搜不到它）。

多一条关于缓存的约定：`index.html` 明确 `no-store`（它含本次进程的会话令牌），
而 `assets/` 下的文件带内容哈希、可以长期缓存。

`/next`（v4 工作台）建立基线的流程与旧页面一致但更严格：`POST /api/session/baseline`
顺带创建「首张基线预览」任务，前端**轮询到该任务结束**才把图挂上 —— 响应里虽然已经带了
`preview_url`，但那一刻文件还没落盘。因此「基线已建立」（可调参）与「基线图可用」是两个
独立状态：生成中显示「正在生成基线预览…」，失败 / 被取代则明确说「基线已建立，但首张画面
未生成」，不会挂一张 404 的图，也不会谎称还在生成。

### 已实现范围

**MVP-01 · 只读连接闭环**：在浏览器里确认能连上 Blender，并只读展示当前工程与场景摘要。不修改场景、不调参、不渲染、不保存、不导入。

**MVP-02 · L0 曝光/辉光调参与无污染预览**：开放 10 项 L0「即时」参数；建立基线后**立即出首张预览**，之后改参即自动重渲。

- 曝光组：`曝光(EV)`、`Gamma`、`视图变换`、`Look`
- 辉光组：`阈值`、`强度`、`尺寸`、`类型`、`质量`、`平滑度`

**MVP-02 补充 · 预览取景**：顶部取景栏 + 四种取景方式 + 临时预览相机自动取景 + 基线失效保护（详见下文「预览取景（构图）」）。

**MVP-02 修正 · 依赖枚举联动**：`视图变换` 与 `Look` 不再当独立参数 —— 切换视图变换会立即刷新 Look 候选、按规范化名称迁移旧值，后端在下发脚本前完成依赖校验（详见下文「依赖枚举（视图变换 → Look）」）。

**MVP-03 第一批 · Windows CI + 本地预设存储后端**：建立自动闸门，并让预设落到用户目录
（`%LOCALAPPDATA%\CartoonModelShader\presets`，**不进仓库**）—— 保存/读取/重命名/复制/删除全部可用，
写入前扫描并**拒绝**任何本机路径、模型/贴图路径与凭据。
**本批次不含**：预设的界面入口、A/B 对比、质量档位的实际应用、会话恢复（见「本地预设（存储后端）」）。

**本阶段只输出临时预览 PNG**：不保存工程、不覆盖 `.blend`、不做正式渲染、不导入模型。

### 调参流程

1. 页面顶部确认已连接 Blender（绿点）。
2. 连接成功后面板自动**建立内存基线**并**立刻渲染一张基线预览**；图片加载成功后状态显示
   「基线已建立，可开始调参」（也可点「建立 / 刷新基线」重建并重渲）。
3. 拖动滑块或切换下拉 —— 停止操作 **300 ms** 后自动触发预览（在需求要求的 250–400 ms 窗口内）。
4. 右侧显示预览图与任务状态；每次预览都严格遵循
   **先恢复内存基线 → 再一次性应用整份草稿 → 渲染 → 自动回滚基线并逐项校验**，
   因此不会在上一次结果上累积修改。
5. 「恢复基线」可随时手动回滚到基线并重新显示基线预览图；「控件复位」把面板恢复成基线取值。
6. 页面顶部取景栏确认人物**完整入画**；若显示「否（已出画）」，把「预览取景方式」切到
   `auto_full_body`（自动全身）或点「重新取景」。切帧/动相机后若出现失效横幅，点「建立 / 刷新基线」即可恢复。
7. 切换「视图变换」时，**Look 下拉框会立即刷新**成该视图真正接受的档位；旧档位若有等价项会自动迁移
   （`AgX - High Contrast` ⇄ `High Contrast`），没有等价项则回退 `None`，并在控件下方说明原因。

> 预览图一律经 `/api/preview/{job_id}` 这个 HTTP 端点提供 —— 落盘目录在系统临时目录
> （`%TEMP%/toon-tuner-previews`），那是本机路径，**绝不直接交给浏览器当 `img.src`**。
> 图片 URL 会带 `?v=<job_id>` 防缓存，确保每次预览都拿到新图而不是浏览器缓存的旧图。

> 预览分辨率按工程分辨率**等比缩放到长边 990**（保持纵横比），渲染结束后分辨率会**必定**还原。
> 本项目工程 1080×1980 → 540×990。取景诊断与实际渲染共用同一画面形状，否则「是否入画」的判断会不准。

### 预览取景（构图）

**为什么要做**：预览默认沿用工程当前相机。本工程的相机带关键帧动画且构图极近，渲染出来人物是**出画**的
（实测最坏角点超出画面 713%），很容易被误读成「调参调坏了」。

页面顶部有一条**取景栏**，只读显示四项事实：**当前帧 / 当前相机 / 相机是否带动画 / 角色是否完整入画**（出画时给出左右下上的越界百分比）。

「预览取景方式」有四种：

| 取值 | 含义 |
|---|---|
| `current_camera` | 沿用工程当前相机（**默认**） |
| `auto_full_body` | 自动全身 |
| `auto_upper_body` | 自动半身 |
| `auto_headshot` | 自动头像 |

自动取景使用**独立的临时预览相机**：**不改**用户原相机的任何属性、**不写**任何关键帧、**不保存**进工程、
渲染后（含渲染抛错时）**必定**恢复 `scene.camera` 并清理临时对象。画面来源会在预览面板上明确标注
「**当前相机预览**」或「**临时自动取景 · 不改动用户相机**」。

另有「**重新取景**」按钮与「**安全边距**」参数（0–40%，默认 15%）：边距越大，人物越小、留白越多。

**基线失效保护**：建立基线时会一并锁住当前帧与相机状态。若之后你（或别的脚本）**切了帧或动了相机**，
工具会**停止自动预览**、弹出「当前帧/相机已变化，请刷新基线」横幅并禁用「重新取景」，
**不会**把新构图当成参数效果端上来。点「建立 / 刷新基线」即可重新锁定并恢复自动预览。
角色自身漂移与相机动画本身只产生**软告警**，不阻断预览。

**参数恢复与「有未保存的修改」**：每次预览都从内存基线出发 —— 参数值、节点、相机与输出设置
在预览结束后都会**恢复到原值**；本工具**不会保存、也不会覆盖**你的 `.blend` 文件。但 Blender 的
`bpy.data.is_dirty` 是**粘性**标记：把值写回去也不会自动清除它。因此预览之后 Blender 仍可能显示
「有未保存的修改」—— **那不代表工程内容有差异**，也不是缺陷，工具更不会用自动保存或重开工程去
「修」这个标记。基线建立时工程本来是干净的，页面会就此给出一次说明（`project.dirty_flagged`）。

### 依赖枚举（视图变换 → Look）

**为什么要做**：`Look` **不是独立参数**，它的合法取值由 `视图变换` 决定。旧实现把 OCIO 的**全局** look 名单
（`getLookNames()`）当成候选，于是把 AgX 专属档位写进了只接受通用档位的视图，预览直接报
`BLENDER_SCRIPT_ERROR`。另一个看似可用的来源 `bl_rna.properties['look'].enum_items` 在无 UI 上下文里
只返回 `NONE`（Blender 5.2.1 实测 `items_count == 1`），同样不可用。

现在按下面的方式工作：

1. **建立基线时一次性扫出全量能力表** —— 对每个 `view_transform` 探出它真正接受的 look 集合。
   合法性不是我猜的：写一个必然非法的哨兵值，让 Blender 自己报出
   `enum "X" not found in ('None', 'High Contrast', ...)`，再从这段**报错文本**里解析出权威允许列表。
   探针全程只读，`finally` 恢复原 `view_transform` / `look`；恢复失败就直接报错，绝不当作成功。
2. **value 与 label 分离** —— 下拉框每一项是 `{"value", "label"}`。`value` 是 Blender 真实 identifier，
   **只有它会被写进 Blender**；`label` 只用于显示（族前缀视图下形如 `AgX - High Contrast`）。
3. **前端联动** —— 切换视图变换立即刷新 Look 列表并按规范化名称迁移旧值；**刷新成功前不提交任何草稿**，
   所以不会出现「拿着旧档位去试」的中间态。快速连续切换时用代次守卫丢弃过期响应，旧结果不会覆盖新列表。
4. **后端在下发脚本前校验依赖** —— 非法组合返回稳定错误 `INVALID_DEPENDENT_ENUM`（含
   `parameter` / `value` / `depends_on` / `allowed`），**不会**退化成笼统的 `BLENDER_SCRIPT_ERROR`，
   也不会白跑一次 Blender。
5. **写入顺序固定且原子** —— `view_transform` → 复核 `allowed_looks` → `look` → `exposure` → `gamma` → 其他。
   任一阶段失败就整体回滚到本次应用前的四值，不留「视图变了、Look 没变」的半应用状态；恢复基线走同一套映射，
   不盲写旧字符串。
6. **记录三类值** —— 任务结果里每个参数都记 `configured_value`（你配置的）/ `effective_value`（真正写进 Blender 的）/
   `display_label`（界面显示标签），旧预设里的老名字加载时自动迁移。

### 本地预设（存储后端）

预设是**用户的资产**，所以落在文件系统而不是浏览器 `localStorage`（清缓存即丢、无法备份分享）：

```
%LOCALAPPDATA%\CartoonModelShader\presets\
```

可用环境变量 `TOON_TUNER_PRESET_DIR` 整体覆盖。**默认路径绝不在仓库内**（有测试钉住这一点）。

一份预设长这样：

```json
{
  "schema": "toon-tuner-preset/1",
  "preset_id": "15f92ca1b396",
  "name": "柔和辉光",
  "created_at": "2026-10-09T10:58:47+08:00",
  "updated_at": "2026-10-09T10:58:47+08:00",
  "pipeline_mode": "enhanced",
  "framing_mode": "auto_full_body",
  "framing_margin": 0.15,
  "preview_quality": "standard",
  "parameters": {
    "color.exposure": { "configured_value": 0.2, "effective_value": 0.2, "active": true }
  }
}
```

几个关键点：

1. **身份与文件名分开**。文件名由 `name` 净化而来（方便直接浏览/备份目录，中文原样保留）；
   稳定身份是文件内的 `preset_id`。重命名只改名字与文件名，**身份不变**，引用不会失效。
2. **禁止保存**：`.blend` 绝对路径、用户目录、模型名或贴图路径、MCP Token（及任何凭据）、
   临时预览路径。命中即**拒绝写入**，不做静默清洗 —— 清洗会让你以为存下了、其实被改过。
3. **旧 schema 明确报错**。只接受 `toon-tuner-preset/1`；其它版本抛 `PRESET_SCHEMA_UNSUPPORTED`，
   **不自动迁移、不猜版本** —— 猜错就是静默改变取值。
4. **枚举参数不做本机绑定**。浮点参数按 `ParamSpec` 的上下限校验；枚举只做结构性校验 ——
   预设要能跨 Blender 版本使用，真正的合法性留给提交预览时对着**当时的**能力表判定。
5. **目录里的坏文件要说出来**。读不动/版本不对/`preset_id` 重复的文件会进列表响应的 `skipped`
   并附错误码与原因，不会被静默丢掉。
6. **接口不回本机路径**。`storage` 只在目录等于默认位置时给 `%LOCALAPPDATA%\...` 写法，
   其余一律回 `<自定义预设目录>`；连相对部分都不给 —— 那部分可能嵌套用户名。
7. **预设接口完全不依赖 Blender**（有一条测试把配置指向必然连不上的端口，增删改查照常工作）。

`materialize_draft()`（预设 → 可直接提交的草稿）已实现并测试：只取 `active=true` 的参数、
优先用 `effective_value`、并复用依赖枚举那套规则规范化 `look`。判得了视图就**严格校验**
（非法则抛 `INVALID_DEPENDENT_ENUM`），判不了就带出原值并明确说明「延后到提交预览时校验」。

`preview_quality` 的三个档位已能保存与校验（快速 360×660/4、标准 540×990/8、高质量 1080×1980/16），
但**实际应用到渲染**属于下一批 —— 届时按工程纵横比等比缩放到档位长边，不强行套用名义宽高。

> 本批次只做了后端存储与接口，**界面上还没有预设入口**。
### 保存（预设 / 应用到工程）

**写盘能力是这一版新增的**，因此安全门槛也一并落在这一层。

#### 会话令牌：所有写接口的统一闸门

只监听回环并**不足以**保护写接口 —— 浏览器里的任意网页都能向 `http://127.0.0.1:8765`
发起不需要预检的跨站请求 (页面所在主机名不是 `127.0.0.1` 时就是跨站)。所以：

* 进程启动时生成一个随机会话令牌（`secrets.token_urlsafe(32)`）；
* 令牌只通过 `GET /` 的**页面响应**注入（`window.__TOON_TUNER_TOKEN__`），
  `src/web/index.html` 里只有一个占位注释，**磁盘上没有带令牌的文件**，日志与响应里也从不出现它；
* **全部 POST 接口**都必须带请求头 `X-Toon-Tuner-Token`，缺失或错误一律 `401 SESSION_TOKEN_INVALID`；
  比较用 `hmac.compare_digest`（常量时间）。

#### 保存预设

把当前整份草稿存成本地预设，落在 `%LOCALAPPDATA%\CartoonModelShader\presets`。
名称支持中文，但**名称只用于显示与去重，绝不参与路径拼接**：文件名由服务端从名称派生，
接口里根本没有路径字段 —— 路径穿越在结构上就不可能发生。写入用「同目录临时文件 +
`fsync` + `os.replace`」，失败时清掉临时文件，不会留下半截 JSON。
「保存预设」与预览、保存共用同一套草稿校验，所以越界取值或非法的 Look / 视图变换组合会被直接拒绝。

> 当前只支持保存与列出；**把预设加载回界面**留待后续（见「开发状态」）。

#### 应用到工程

顺序固定且不可跳步：**应用完整草稿 → 回读逐项校验 → 备份 → 保存工程**。

* **默认另存为**：目标必须是绝对路径且以 `.blend` 结尾、父目录必须存在。
  如果该文件**已存在**，同样按「覆盖」对待（要求二次确认并先生成备份），绝不静默覆盖。
* **覆盖当前工程**：目标**只能**是 Blender 当前工程 —— 请求里带路径会被直接拒绝。
  界面上必须显示目标绝对路径与预计备份路径，用户勾选确认后才签发一次性令牌。
* **未保存改动会明确警告**：当 Blender 里还有未保存的改动时，磁盘上的 `.blend` 是上一次保存的版本，
  因此备份**只包含那一版、不包含当前未保存的改动**；这些改动会在本次保存中一并写入工程文件
  （受本工具管理的曝光/辉光参数会被设成当前草稿值）。界面上会把这句话显示出来。
* **备份**：在目标同目录生成 `<主干>.bak-<时间戳>.blend`（保留 `.blend` 扩展名，改名回去即可直接打开）。
  **备份路径在准备阶段就算定，并随确认令牌一起绑定** —— 提交时不会重算，所以实际备份位置一定
  就是用户确认过的那条；该路径若已被占用则直接拒绝（不销毁已有备份）。
  **备份失败就拒绝覆盖。**
* **一次性确认令牌**：绑定「基线 ID + 完整草稿指纹 + 保存模式 + 目标路径」，有效期 120 秒。
  提交时逐项比对 —— 不一致判篡改，用过判复用，超时判过期；校验通过即刻作废，不给重复落盘留窗口。
* **失败即不落盘，并回滚基线**：回读不一致、备份失败、保存失败都直接中止，且都会**尝试把 Blender
  恢复到提交前的基线**（草稿已经写进场景，不恢复会让界面显示的参数与 Blender 里实际取值脱节），
  回滚后再**回读校验**。响应里带回「应用 / 回读 / 备份 / 保存 / 失败后回滚」五态与步骤，
  **草稿保留在界面上**；回滚没能确认成功时会明确报出 `ROLLBACK_FAILED` 并要求人工核对，
  而不是含糊地说一句「已回滚」。
* 保存成功后**自动重新采集基线**：文件内容已经就是这份草稿，沿用旧基线会让后续预览「恢复到旧状态」。
  基线一变，此前签发的令牌自然失效（这是预期行为）。

细节与错误码见 [`docs/接口文档.md`](docs/接口文档.md)。

### 配置

复制 `config.example.json` 为 `config.local.json`（已被 `.gitignore` 排除）按需覆盖 host / port / 超时。
服务与 MCP 客户端都**只允许连回环地址**，配置成远程地址会被直接拒绝。

环境变量可覆盖：`TOON_TUNER_MCP_HOST`、`TOON_TUNER_MCP_PORT`、`TOON_TUNER_MCP_CONNECT_TIMEOUT`、
`TOON_TUNER_MCP_RESPONSE_TIMEOUT`、`TOON_TUNER_SERVER_HOST`、`TOON_TUNER_SERVER_PORT`。

### 测试

```powershell
pytest
```

CI（`.github/workflows/test.yml`）在 **windows-latest** 上用 **Python 3.11 / 3.12** 各跑一遍：
装 `requirements.txt` → `python -m pytest`。**不依赖真实 Blender**、**不上传任何产物**
（模型、预览图、日志都不留）。真实 Blender / 真实浏览器验收仍是**人工步骤**，不进 Runner。
前端另有独立的一套（**pytest 不依赖 Node**）：

```powershell
cd web
npm ci
npm run typecheck
npm run build     # 末尾会跑 scripts/verify-dist.mjs 校验产物
npm run test      # tooling/dist-artifacts.test.ts 会断言 dist 的真实内容，因此 build 要在 test 之前
```

前端测试有两条与**运行环境**有关的约定，目的不是「让测试变绿」，而是让测试可信：

- **jsdom 缺少 `onpointer*` IDL 属性**：Preact 按 `"onpointerdown" in element` 决定注册的事件名，
  为假时会注册驼峰 `PointerDown`，于是浏览器语义的 `pointerdown` 永远打不中 ——
  表现是「拖动在测试里毫无反应」，看起来像组件坏了。`src/testing/setup-dom.ts` 在 `setupFiles`
  里补上缺失的 IDL 属性（**只补存在性**，不伪造 `setPointerCapture` 等行为，组件已按不支持捕获降级）。
- **开发期令牌桥**（`tooling/dev-token-bridge.test.ts`）：断言它能取到令牌、失败时给出明确原因、
  响应不缓存。没有它，`npm run dev` 下所有写请求都会 401。

测试使用内置假 MCP 服务与**假 `bpy` 桩**，不需要安装 Blender：

- 假 `bpy` 桩会**真的执行服务端生成的 Python 代码**，因此能验证生成逻辑本身（不只是响应格式）；
  `FakeCameraData` 特意**复刻了真实 `view_frame` 的行为**（fit 方向半高恒为 0.5、深度不在单位距离），
  这是一条回归护栏：一旦有人把未归一化的角点当单位距离用，自动取景的距离会立刻爆掉，测试会红；
- 覆盖基线幂等、整份草稿应用、旧任务作废终态、越界/注入拒绝、无任意 Python 路由、
  渲染后分辨率必还原、字符串属性不被 `list()` 拆解等；
- 基线相关：建立基线即产出首张预览、`preview_url` 可直接取到 PNG、URL 不含本机路径、
  旧任务不得携带结果、渲染失败时基线保留但任务落 `failed`；
- 依赖枚举相关（`tests/test_color_looks.py`）：能力扫描按视图给出**真正接受**的集合并完整恢复状态、
  RNA 报错不可解析时退化到赋值探测、无 OCIO 时不崩、单视图探针的合法性判定、
  `identifier` 省略时不误探字符串 `"None"`、value/label 分离且 **label 不会被写进 Blender**、
  归一化的四种等价匹配与回退、写入阶段顺序、写 look 失败时 `view_transform` 被回滚、
  非法组合抛 `INVALID_DEPENDENT_ENUM`（且不产生任务、不碰 Blender）、切换视图后完整草稿可预览、
  恢复基线逐项一致、旧预设名称迁移、前端 value/label 与代次守卫契约。
- 预设相关（`tests/test_presets.py`）：`%LOCALAPPDATA%` 解析与环境变量覆盖、默认目录不在仓库内、
  文件名净化（保留中文 / 去保留字符 / 设备名 / 长度上限 / 净化不改 `name`）、
  schema 缺省与 null 按当前版本、**旧 schema 报错不迁移**、顶层与参数条目未知字段拒绝、
  浮点上下限与 `bool` 拒绝、枚举不被本机枚举绑定、
  敏感内容（盘符/UNC/用户目录/AppData/%TEMP%/预览目录/贴图与模型路径/凭据名与凭据值）一律拒绝且不落盘、
  CRUD 全流程、**覆盖保存保留身份与创建时间**、伪造 `preset_id` 无效、重命名身份不变且文件同步改名、
  **复制的到新身份**（回归：曾回退成源身份）、净化后同名文件不互相覆盖、原子写入不留临时文件、
  坏文件/旧 schema/重复身份进 `skipped`、列表响应不含本机绝对路径、
  加载成草稿（只取 active / 优先 effective / 严格拒绝非法 look / 判不了时明确延后）、
  预设接口在 Blender 连不上时依然可用。
- 取景相关（`tests/test_framing.py`）：当前相机模式不改动相机与当前帧、三种自动取景均完整入画、
  安全边距生效、模型离世界原点仍正确、排除刚体代理与描边壳、预览后恢复原 `scene.camera`、
  **渲染抛错也恢复且无临时相机残留**、切帧/换相机/移动相机/改焦距/改 shift 判为失效、
  相机动画与角色漂移只告警、预览分辨率保持纵横比、基线接受/拒绝非法 `framing`、409 `FRAMING_STALE`。
- 保存相关（`tests/test_presets.py` / `tests/test_commit.py` / `tests/test_security_token.py`）：
  所有写接口缺令牌 / 令牌错误一律 401 且**零副作用**、令牌不落静态文件、不回流到任何响应；
  预设新增 / 同名更新（保留创建时间）/ 非法名称 / 参数越界 / 依赖枚举非法 / 路径穿越 / 原子写入失败；
  另存为目标校验（相对路径、非 `.blend`、目录、目录不存在）、覆盖需二次确认、未保存工程禁止覆盖；
  确认令牌过期 / 复用 / 篡改草稿 / 篡改模式 / 篡改目标 / 基线失效；
  覆盖前生成同目录时间戳备份且内容为覆盖前版本、**备份失败则拒绝覆盖**；
  回读不一致 → 不保存且列出不一致项、保存失败 → 保留草稿并回报四态状态；
  路径里的引号只是普通字符（`repr` 量化），请求塞代码 / 额外字段一律 422。
- v4 参数面相关（`tests/test_surface_probe.py` / `test_surface_identity.py` /
  `test_surface_cel.py` / `test_v4_surface_api.py`）：
  只读探针输出脱敏（路径键整体丢弃）；结构签名对**值**不敏感、对**色标数量与节点增删**敏感；
  能力驱动降级（探不到色带 / 命中 0 或 >1 个 Emission 插座 / 假 socket 名不得变成生产硬编码）；
  色带**整体写入**与整体回滚（数量不符、单项越界 ⇒ 整份不写）；
  基线含 Cel schema + 身份 + 结构指纹、探不到组时安全降级但基线仍成功；
  混合 L0 + Cel 草稿**只渲染一次**；
  身份缺失 / 结构变化在**任何写入之前**失败且 `restore.attempted = false`；
  值层外部改动只报告不作废；新任务取代旧任务；失败任务不覆盖最后一张成功预览；
  写入 / 回读 / 渲染 / 恢复各阶段失败注入后 L0 与 Cel 两条基线都恢复并**分别报告**；
  保存令牌绑定参数面草稿与结构指纹（篡改 ⇒ `COMMIT_TOKEN_MISMATCH` 且不落盘，
  旧客户端不传这两个字段时行为完全不变）。
- `/next` 托管相关（`tests/test_next_frontend.py`）：迁移期 `/` 与 `/next` 并存且互不影响；
  未构建 ⇒ `503 FRONTEND_NOT_BUILT` + 构建步骤、**不回退旧页面**；
  令牌只注入 HTML（静态资源逐字节直出，`index.html` 为 `no-store`，资源为 `immutable`）；
  路径穿越（`../`、URL 编码、反斜杠、绝对路径、盘符）一律 404 且取不到 assets 之外的文件。
- v4 前端（`web/`，Vitest 共 127 条）：递归 schema 解析与**未知 kind 降级为只读**；
  `dirtyIds` 计算与提交前过滤（只读项 / 参考组 / 陌生 id 被剔除）；色标范围与严格递增、
  2/3/4 档渲染、拖动与键盘输入的命令合并（一次拖动一条、不同色标不合并、复位/复制为原子命令）；
  撤销/重做往返；复制到兼容组（数量不同即拒绝，不截断不补齐）；Sakura 恒只读；
  结构失效后禁用预览与提交；外部值变化只提示；任务取代与代次丢弃旧结果；
  失败任务保留最后一张成功预览；防缓存 URL；轮询结束/取消/超时/卸载；虚拟列表只挂可视行；
  令牌请求头矩阵（写请求带、GET 不带、401/409/502 展示）；构建产物含唯一占位且无令牌形态串。

### 真实浏览器验收

```powershell
node tests/browser_check.mjs http://127.0.0.1:8765
```

用本机已安装的 Chrome / Edge（CDP 驱动，无需下载 Chromium）跑真实页面 + 真实 Blender，
逐条断言：基线后自动出首张预览、图片 URL 带 `job_id` 防缓存、拖动滑块替换旧图、
**阻断图片请求时显示明确错误且不再显示成功状态**、解除阻断后自愈；
取景栏四项事实、默认「当前相机预览」标识、相机动画告警文案、
自动全身取景后角色完整入画、**原相机世界变换/焦距/shift 逐字段未变**、
临时预览相机零残留、切帧后失效横幅 + 停止自动预览 + 禁用重新取景 + 画面不被顶替、刷新基线后自愈。
另有依赖枚举相关断言：Look 下拉框的 `value` 逐项等于后端能力表里的 Blender 真实 identifier、
切换视图变换后 Look 列表立即刷新并按规范化名称迁移（`AgX - High Contrast` ⇄ `High Contrast`）、
迁移后给出说明文案、切换视图后完整草稿可正常预览（不再 `BLENDER_SCRIPT_ERROR`）、
任务记录里 `effective_value` 是真实 identifier 且 `display_label` 独立保存、
快速连续切换视图后列表对应**最后一次**选择、非法组合报 `INVALID_DEPENDENT_ENUM` 且前端摊开依赖详情。
截图默认落在系统临时目录。当前结果：**52/52 通过**。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 仅检查本地控制服务 |
| GET | `/api/blender/status` | 快速探测端口与协议（始终 200，用 `status` 区分） |
| GET | `/api/blender/scene` | 执行完整只读场景探针 |
| POST | `/api/blender/reconnect` | 重置连接状态并立即重新检测 |
| GET | `/api/params/schema` | 返回 L0 参数白名单（驱动前端动态生成控件） |
| POST | `/api/session/baseline` | 读取并固化「内存基线」（含帧/相机/取景快照），**并立即用基线参数创建首张预览任务**；可传 `framing` 指定取景方式 |
| GET | `/api/session/baseline` | 查看当前内存基线（未建立时 409） |
| POST | `/api/session/restore` | 回滚到基线，返回逐项校验结果 |
| GET | `/api/color/looks` | 某视图下 Blender **真正接受**的 Look 档位（`{view_transform: [{value,label}]}` 能力表）；可传 `view_transform` / `identifier` 走单视图只读探针 |
| POST | `/api/color/looks/refresh` | 重新扫描 Look 能力表（换了 OCIO 配置时用）；只改能力表，不动工程取值 |
| GET | `/api/framing/context` | 只读取景上下文：当前帧/相机/动画/角色是否入画/越界比例 + 基线是否失效 + 四种取景方式白名单 |
| POST | `/api/preview` | 入参 `{"draft": {"参数id": 取值}, "framing": {"mode": ..., "margin": ...}}`，返回 `job_id`；**不接受任何代码** |
| GET | `/api/jobs/{job_id}` | 任务状态查询（替代固定 `sleep`） |
| GET | `/api/preview/{job_id}` | 取回该任务的预览 PNG（浏览器唯一的取图入口） |
| GET | `/api/presets` | 本地预设列表 + 运行设置白名单（`quality_tiers` / `pipeline_modes` / `framing_modes` / `defaults`）；读不动的文件进 `skipped` |
| GET | `/api/presets/{id}` | 读取单个预设（含逐参数 `configured_value` / `effective_value` / `active`） |
| POST | `/api/presets` | 新建预设；`parameters` 可给三元组，也可只给裸取值 |
| PUT | `/api/presets/{id}` | 覆盖保存（`preset_id` 与 `created_at` 由服务端保留，客户端改不动） |
| POST | `/api/presets/{id}/rename` | 重命名（**身份不变**） |
| POST | `/api/presets/{id}/duplicate` | 复制为新预设（**新身份**，默认名字加「副本」） |
| DELETE | `/api/presets/{id}` | 删除预设 |
| POST | `/api/session/commit/prepare` | 校验基线/草稿/保存模式/目标路径，返回一次性短效确认令牌 + 目标绝对路径 + 预计备份路径 + 告警 |
| POST | `/api/session/commit` | 入参 `{"token", "mode", "draft", "target_path"}`：消费令牌 → 应用完整草稿 → 回读校验 → 备份 → 保存工程 |
| GET | `/api/diagnostics/describe` | 只读拓扑导出（受管节点组 / ColorRamp 结构 / 对象与材质清单），**已脱敏**；用于校准 Cel 的真实 socket 名 |
| GET | `/api/v4/surface/schema` | v4 递归 schema（`toon-surface/2`）：Cel 色带等参数树，每个节点带 `supported`/`editable`/`active`/`readonly_reason` |
| GET | `/api/v4/session/baseline` | v4 基线：身份记录 + 结构指纹 + 基线值 + 降级清单（建立基线仍走 `POST /api/session/baseline`） |
| POST | `/api/v4/preview` | v4 预览：L0 与 Cel 编进**同一个任务**，只渲染一次；可带 `expected_structure_hash` |
| GET | `/api/v4/jobs/{job_id}` | v4 任务状态（与 `/api/jobs/{job_id}` 共用同一份存储） |
| GET | `/next` | v4 工作台页面（构建产物；**未构建 ⇒ 503 `FRONTEND_NOT_BUILT`**，不回退旧页面） |
| GET | `/next/assets/{path}` | v4 构建资源（内容哈希命名、长缓存；路径越界 ⇒ 404） |

> 上表最后四行是 **v4 过渡接口**（Cel 竖切）。旧页面继续用前面的接口，行为一行不改；
> `POST /api/session/baseline` 的响应里多了一个可选 `surface` 字段。
> `POST /api/session/commit/prepare` / `commit` 各自新增**可选**的 `surface_draft` 与
> `structure_hash`：都不传时行为与旧客户端完全一致。详见 [`docs/接口文档.md`](docs/接口文档.md) §6。

> **令牌**：除 `GET` 之外的**所有写接口**都必须带请求头 `X-Toon-Tuner-Token`。
> 令牌在进程启动时随机生成，只通过 `GET /` 的页面响应注入前端，**不写入静态文件、不写日志**；
> 缺失或错误一律 `401 SESSION_TOKEN_INVALID`。完整契约见 [`docs/接口文档.md`](docs/接口文档.md)。

`status` 取值：`connected` / `disconnected` / `timeout` / `protocol_error` / `blender_error`。
任务 `status` 取值：`queued` / `running` / `done` / `failed` / `superseded`。
新任务提交时，排队中与运行中的旧任务会立即进入 `superseded` 终态并丢弃结果。

常用错误码：`PARAM_INVALID`（参数越界/未知字段）、`NO_BASELINE`（未建基线）、
`FRAMING_STALE`（409，基线建立后帧或相机被外部改动，retryable —— 刷新基线即可恢复）、
`FRAMING_UNAVAILABLE`（409，拿不到当前取景上下文）、
`PREVIEW_OUTPUT_UNAVAILABLE`（409，非 retryable：预览只能出 PNG，但工程的输出设置切不过去 ——
常见于把渲染输出设成影片格式的工程。**预览已中止，工程零改动**；把「输出属性 → 输出」改成图片格式后重试）、
`INVALID_DEPENDENT_ENUM`（400，依赖枚举取值非法，例如 Standard 下提交 `AgX - Punchy`；返回体里带
`parameter` / `value` / `depends_on` / `allowed`，`retryable=false`）。

预设相关错误码：`PRESET_NOT_FOUND`（404）、`PRESET_INVALID`（400，未知参数/越界取值/含禁止保存的内容）、
`PRESET_SCHEMA_UNSUPPORTED`（400，schema 版本不支持，**不自动迁移**）、
`PRESET_NAME_CONFLICT`（409，同名预设，名称不区分大小写）、`PRESET_STORAGE_ERROR`（500，目录读写失败）。

### Blender 5.x 适配记录

- 合成器节点树在 5.x 是 **`scene.compositing_node_group`**，不再是 `scene.node_tree`。
- 输出格式多了 **`render.image_settings.media_type`**（`IMAGE` / `MULTI_LAYER_IMAGE` / `VIDEO`）。
  **`media_type == "VIDEO"`（工程是影片输出）时，`file_format` 的可用集合被限定为影片格式**，
  此时直接赋 `"PNG"` 会抛 `enum "PNG" not found in ('FFMPEG')` —— 必须先切回 `IMAGE`。
  预览渲染因此**不硬写 PNG**：先「赋值 + 回读」地安全切换，切不过去就零污染中止。
- `bl_rna.properties["file_format"].enum_items` **不能用来判断可用性** —— 影片态下它照样把 PNG 列出来。
- `Image.save_render()` 的格式**听场景的 `image_settings`、不听文件扩展名**：给 `.png` 路径但在
  JPEG 场景里调用，得到的是 JPEG。
- 本管线的辉光节点为 **`AI_Compositor › Autocel_Glow`**（GLARE，label「辉光」）；该组内无 Tonemapping 节点。
- `view_transform` / `look` 的候选值无法从 `bl_rna.enum_items` 取到（非 UI 上下文只返回 `NONE`）。
  `PyOpenColorIO` 的 `getViews(display)` 可以用来枚举视图，但 **`getLookNames()` 是 OCIO 全局名单、
  不是当前视图的合法集合**（是各视图合法集合的超集），只能当探测原料。
  `look` 的权威允许列表改从 **Blender 自己的枚举报错文本**里解析（见「依赖枚举（视图变换 → Look）」）。
- 实测 Blender 5.2.1：`Standard`/`Filmic`/`Filmic Log`/`Raw`/`Khronos PBR Neutral` → `None` + 通用 7 档；
  `AgX` → `None` + `AgX - *` 9 档；`False Color` → `None` + `False Color - *` 9 档；
  `ACES 1.3`/`ACES 2.0` → `None` + `<同名> - Reference Gamut Compression`。
  切换 `view_transform` 时 Blender 自身会按「档位」跨视图映射 look（`AgX - High Contrast` ⇄ `High Contrast`）。

### 常见问题

| 现象 | 处理 |
|---|---|
| 页面显示「未连接」 | 确认 Blender 已打开，并在插件面板里启动了 MCP 服务（监听 9876） |
| 显示「连接超时」 | Blender 可能正忙（例如正在渲染）；空闲后点「重新连接」 |
| 显示「协议错误」 | 9876 端口可能被别的程序占用；确认是本工具的 Blender MCP |
| 显示「Blender 执行错误」 | Blender 内执行的探针/写入失败；确认当前场景状态后重试 |
| 预览报「缺少基线」 | 先在连接正常时建立基线；Blender 重开后基线需重建 |
| 状态显示「基线已建立，预览失败」 | 基线参数已保留，但预览图没能产出/加载。检查 Blender 内是否有相机与 `AI_Compositor` 合成器节点组，再点「建立 / 刷新基线」重试 |
| 预览图偏亮/偏暗 | 这是草稿本身的效果；点「恢复基线」可回滚 |
| 人物出画/只有半张脸 | 工程相机构图偏近所致。把「预览取景方式」切到 `auto_full_body` / `auto_upper_body` / `auto_headshot`，或调大「安全边距」。自动取景用临时相机，**不会改动你的相机** |
| 出现「当前帧/相机已变化，请刷新基线」 | 你在建立基线后切了帧或动了相机（或相机带关键帧动画、帧被别的脚本改了）。点「建立 / 刷新基线」重新锁定即可；刷新前自动预览会保持停止，避免把新构图误当参数效果 |
| 取景方式切了但画面没变 | 若已出现失效横幅，「重新取景」会被禁用（这是有意的）。先刷新基线 |
| 切换视图变换后 Look 下拉框变了 | 这是有意的：Look 的合法档位由视图变换决定。旧档位有等价项会按名称迁移并给出说明，没有等价项则回退 `None` |
| 预览报 `INVALID_DEPENDENT_ENUM` | 该 Look 在当前「视图变换」下不合法。提示里会列出可选档位；换一个档位，或先把视图变换调回去 |
| 旧预设里的 Look 名字不认了 | 预设里的显示名会在加载时按当前视图迁移成真实 identifier；若确实没有等价档位会回退 `None` 并给出告警 |
| 预设保存在哪 | `%LOCALAPPDATA%\CartoonModelShader\presets`（不在仓库里）。用 `TOON_TUNER_PRESET_DIR` 可改位置 |
| 提示预设「schema 版本不支持」 | 这不是本工具写的预设，或来自更早/更新的版本。**不会自动迁移**（迁移错了就是静默改取值），请用对应版本打开后另存 |
| 提示预设含「禁止保存的内容」 | 预设里带了本机路径、模型/贴图路径或凭据。这是有意拒绝的：清洗会让你以为存下了、其实被改过 |
| 提示预设重名 | 名称不区分大小写、忽略首尾空格。换一个名字，或先重命名原有预设 |
| 预设列表里出现「已跳过」的文件 | 该文件读不动、版本不对或 `preset_id` 重复。列表里会附错误码与原因，修好或删掉即可 |
| 页面打不开 | 确认 uvicorn 已在 8765 运行 |

## 开发状态

- [x] 需求文档定稿
- [x] 仓库初始化
- [x] **MVP-01：只读连接闭环** —— 连接 9876 → 读当前场景 → 展示连接状态与场景摘要 → 断线重连（不改动任何 Blender 数据）
- [x] **MVP-02：L0 曝光/辉光调参与无污染预览** —— 内存基线 → 整份草稿应用 → 单次预览渲染 → `job_id` 状态查询 → 回滚基线并校验（只出临时 PNG，不保存工程）
- [x] **MVP-02 补充：预览取景** —— 顶部取景诊断栏（帧/相机/动画/是否入画）→ 四种取景方式 → 临时预览相机自动取景（`finally` 必定恢复原相机）→ 基线失效保护（切帧/动相机即停止自动预览）
- [x] **MVP-02 修正：依赖枚举联动** —— 按 `view_transform` 探测 Look 合法集合 → value/label 分离 → 切换视图即刷新候选并迁移旧值 → 下发前依赖校验（`INVALID_DEPENDENT_ENUM`）→ 写入顺序固定 + 原子回滚
- [x] **MVP-03 第一批：Windows CI + 本地预设存储后端** —— `windows-latest` × Python 3.11/3.12 自动闸门；预设落到 `%LOCALAPPDATA%\CartoonModelShader\presets`（不进仓库），保存/读取/重命名/复制/删除 + schema 与敏感内容校验 + 加载成草稿
- [ ] **MVP-03 其余批次**：预设界面入口、A/B 对比、预览质量档位的实际应用、会话恢复
- [ ] 保存预设 / 应用到工程（**须在「恢复基线」通过重复测试后才开始**）
- [x] **保存预设 / 应用到工程（自动测试完成；真实 Blender 验收待办）** —— 原子写入的本地预设 + 一次性确认令牌 → 应用完整草稿 → 回读逐项校验 → 同目录时间戳备份 → 保存工程（默认另存为；覆盖路径必须二次确认）
- [x] **v4 提交 1：只读拓扑探针与技术方案** —— `GET /api/diagnostics/describe`（脱敏）+ `docs/技术方案_v4.md`
- [x] **v4 提交 2：递归 schema、三层身份、Cel 适配器、通用执行器** —— `src/server/surface/`
- [x] **v4 提交 3A：Cel 后端完整竖切（自动测试完成；真实 Blender 验收待办）** —— 基线采集只读拓扑 → `GET /api/v4/surface/schema` → `POST /api/v4/preview`（L0 + Cel 同一任务、只渲染一次、失败即恢复两条基线并分别报告）→ 身份/结构闸门写入前判定 → 保存令牌绑定参数面草稿与结构指纹
- [x] **v4 提交 3B：Vite/Preact 工作台与 Cel 色阶编辑器（自动测试完成；真实 Blender 验收待办）** —— `web/` 工程（Vite + Preact + TypeScript + Vitest，自研轻量虚拟列表）→ `/next` 三栏工作台（顶部状态栏 / 导航 / 固定预览 / 检查器 / 底部操作栏）→ Cel 编辑器（色带拖动、位置与 RGBA、插值、复制到兼容组、Sakura 只读）→ 前端命令栈（撤销/重做）→ 任务代次与防缓存预览 → CI 独立 `web` job
- [ ] **v4 提交 4：完整参数族（世界/描边/渲染质量/灯光/相机/材质）与 L0–L3 调度，L0 迁入递归 schema**
- [ ] **v4 提交 5：预设 v1→v2 迁移、新 UI 切换到 `/` 并下线旧页面**
- [ ] **校对 `/next` 的真实浏览器效果**（本提交只做了 Vitest 的 jsdom 断言；真机浏览器仍需人工看一眼）
- [x] **校准 Cel 的真实 socket 名** —— 真机确认（2026-10-11，Blender 5.2.1 LTS，受管 Cel 组
      全部命中「`EMISSION` 节点（节点名本地化，如「自发光」）+ `Strength` 插座且未连线」）→
      `cel.<group>.emission_strength` 写入 `NODE_SOCKET.default_value`
      （`object_id` = 组名/节点名/插座名，三段来自探测结果）；插座被上游连线、或该组是
      reference 角色时仍降级只读
- [x] **渲染脚本侧辉光节点定位** —— 合成器 Glare 节点按 `bl_idname == "CompositorNodeGlare"`
      定位（名字优先、类型回退）：真机上节点名随工程而变（实测 `GLOW_卡通辉光`），
      硬编码任何名字都不可靠
- [ ] **真实 Blender 验收保存流程**（人工步骤：另存为到新文件、覆盖当前工程、确认备份可打开、故意让保存失败并确认草稿保留）
- [ ] **真实 Blender 验收 v4 竖切**（人工步骤：连续预览不累积污染、恢复基线逐项一致、保存重开后参数图一致）- [ ] 严格材质匹配与手工归类（L2）
- [ ] A/B 对比、差异热力图、背景切换
- [ ] 正式渲染与运行清单
- [ ] 预设的「加载 / 应用到界面」（当前只支持保存与列出，加载留待后续）
