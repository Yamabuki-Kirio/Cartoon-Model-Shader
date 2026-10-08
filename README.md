# Cartoon-Model-Shader

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
| 参数面 | 界面由 46 项 schema 驱动，MVP 先开放约 20 项高价值参数 |

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

- **前端**：本地 Web 界面，参数界面由 `reference/参数面清单.json` 动态生成，而非把 46 项控件写死。
- **本地控制服务**：管理连接、校验参数、排队去抖、保存预设与运行记录、推送状态。默认仅监听 localhost，启动时生成随机令牌校验请求。
- **Blender 桥接层**：在现有 MCP 接口之上增加会话协议层（协议版本、任务 ID、能力查询、错误码、状态查询）。接口若不能主动推送，则由控制服务短间隔**非阻塞轮询**。

## 重算层级

| 层级 | 内容 | 目标反馈 | 触发方式 |
|---|---|---|---|
| **L0** | 辉光、曝光、Gamma、View Transform、Look、Tonemapping | 0.75–1.5 s | 停止调整 250–400 ms 后**自动预览** |
| **L1** | Cel 色阶、Emission、描边宽度/颜色、世界强度、采样、阴影 | 2–3 s | 滑块释放 / 颜色确认后 |
| **L2** | 材质归类、基础贴图、色彩空间、分辨率 | 约 5 s | 「应用并预览」，带进度 |
| **L3** | 管线模式、重新追加资源、灯光装置、相机策略 | 7–8 s | 独立「重建管线」操作 |

## 状态模型

`原始工程状态` → 用户点「初始化管线」→ `工具基线状态` → 调参产生 `草稿参数` → 预览从基线恢复 + 应用完整草稿得 `预览状态` → 用户点「应用到工程」得 `已提交状态`。

保存提供两种方式：**默认另存为**；覆盖当前 `.blend` 时显示绝对路径、二次确认，并先生成带时间戳的备份。

## 目录结构

```
Cartoon-Model-Shader/
├── docs/                            设计与需求文档
│   ├── 需求文档.md
│   ├── 技术设计.md
│   ├── 安全检查.md
│   └── AI交付_第一步实施方案.md        MVP-01 任务书
├── reference/                       参考数据（参数面 schema 等）
│   ├── 参数面清单.json
│   ├── 一键卡通渲染.py
│   ├── 一键渲染_通用驱动.py
│   └── 审计_本次执行.md
├── src/
│   ├── server/                      本地控制服务（FastAPI）
│   │   ├── app.py                   路由 + 静态托管
│   │   ├── config.py                非敏感配置（仅回环）
│   │   ├── blender_mcp.py           TCP JSON 客户端
│   │   ├── scene_probe.py           只读探针模板 + 解析
│   │   ├── models.py                API 模型
│   │   ├── errors.py                稳定错误码
│   │   └── redact.py                诊断脱敏
│   └── web/                         浏览器前端（原生 HTML/CSS/JS）
├── tests/                           假 MCP + pytest 用例
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

## 本地运行（MVP-01 · 只读连接闭环）

本阶段只做一件事：**在浏览器里确认能连上 Blender，并只读展示当前工程与场景摘要**。
不修改场景、不调参、不渲染、不保存、不导入；不提供任何接受任意 Python 的接口。

### 安装与启动

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn src.server.app:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。需要 Python 3.11+。

### 配置

复制 `config.example.json` 为 `config.local.json`（已被 `.gitignore` 排除）按需覆盖 host / port / 超时。
服务与 MCP 客户端都**只允许连回环地址**，配置成远程地址会被直接拒绝。

环境变量可覆盖：`TOON_TUNER_MCP_HOST`、`TOON_TUNER_MCP_PORT`、`TOON_TUNER_MCP_CONNECT_TIMEOUT`、
`TOON_TUNER_MCP_RESPONSE_TIMEOUT`、`TOON_TUNER_SERVER_HOST`、`TOON_TUNER_SERVER_PORT`。

### 测试

```powershell
pytest
```

测试使用内置假 MCP 服务，**不需要安装 Blender**。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 仅检查本地控制服务 |
| GET | `/api/blender/status` | 快速探测端口与协议（始终 200，用 `status` 区分） |
| GET | `/api/blender/scene` | 执行完整只读场景探针 |
| POST | `/api/blender/reconnect` | 重置连接状态并立即重新检测 |

`status` 取值：`connected` / `disconnected` / `timeout` / `protocol_error` / `blender_error`。

### 常见问题

| 现象 | 处理 |
|---|---|
| 页面显示「未连接」 | 确认 Blender 已打开，并在插件面板里启动了 MCP 服务（监听 9876） |
| 显示「连接超时」 | Blender 可能正忙（例如正在渲染）；空闲后点「重新连接」 |
| 显示「协议错误」 | 9876 端口可能被别的程序占用；确认是本工具的 Blender MCP |
| 显示「Blender 执行错误」 | 只读探针在 Blender 内失败；确认当前场景状态后重试 |
| 页面打不开 | 确认 uvicorn 已在 8765 运行 |

## 开发状态

- [x] 需求文档定稿
- [x] 仓库初始化
- [x] **MVP-01：只读连接闭环** —— 连接 9876 → 读当前场景 → 展示连接状态与场景摘要 → 断线重连（不改动任何 Blender 数据）
- [ ] 最小闭环：连接 → 读场景 → 建立基线 → 调整 L0 → 自动预览 → 恢复基线 → 保存预设
- [ ] Cel 色阶编辑器（7 组）
- [ ] 严格材质匹配与手工归类
- [ ] A/B 对比、差异热力图、背景切换
- [ ] 正式渲染与运行清单
- [ ] 覆盖/另存为保存流程 + 自动备份
