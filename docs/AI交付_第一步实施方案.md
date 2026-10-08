# Cartoon Model Shader：交付开发 AI 的第一步实施方案

版本：1.0  
阶段：MVP-01，只读连接闭环  
目标仓库：`Yamabuki-Kirio/Cartoon-Model-Shader`（Private）  
平台：Windows  

## 1. 本阶段目标

实现一个可以在本机浏览器打开的最小 Web 工具，通过现有 Blender MCP 端口 `127.0.0.1:9876` 连接用户已经打开的 Blender，并以只读方式展示当前 Blender 与场景摘要。

完整闭环：

```text
启动本地控制服务
→ 浏览器打开 Web 页面
→ 检测 127.0.0.1:9876
→ 读取当前 Blender 场景
→ 展示连接状态与场景摘要
→ Blender 断开时显示错误
→ Blender 恢复后可手动重连
```

本阶段完成后，产品还不能调参、渲染或保存工程；它只证明浏览器、本地控制服务和 Blender 三层可以可靠通信。

## 2. 强制约束

1. 仅支持 Windows。
2. 界面运行在普通浏览器，不封装 Electron/Tauri。
3. 必须复用现有 Blender MCP `127.0.0.1:9876`，不得要求安装新的 Blender 插件。
4. 本阶段对 Blender 只读，不得修改场景、节点、材质、灯光、相机、选择状态或文件。
5. 不得调用渲染、保存、导入、追加资源或管线初始化操作。
6. 不使用固定 `sleep` 等待 Blender。
7. 服务默认只监听 `127.0.0.1`，不得监听 `0.0.0.0`。
8. 不得把本机绝对路径、模型、贴图、 `.blend`、日志或密钥提交到仓库。
9. 不改写 `reference/参数面清单.json` 和 `docs/需求文档.md` 的事实内容。
10. 开始编码前先检查仓库现有结构、README、AGENTS.md 和依赖文件，保留已有实现，不重复搭建同类组件。

## 3. 推荐技术栈

后端：

- Python 3.11+
- FastAPI
- Uvicorn
- Pydantic
- Python 标准库 `socket` 与 `json`

前端：

- 原生 HTML/CSS/JavaScript
- 暂不引入 React/Vue 和构建工具
- 由 FastAPI 直接托管静态页面

测试：

- pytest
- FastAPI TestClient 或 httpx
- 使用假 TCP MCP 服务测试，不要求 CI 环境安装 Blender

如果仓库已经选择了不同但合理的技术栈，开发 AI 应优先沿用现有技术栈，并在交付说明中解释，不要为了遵循本建议而无意义重写。

## 4. 已知 Blender MCP 协议

现有接口是 TCP 行分隔 JSON：

```text
Host: 127.0.0.1
Port: 9876
编码: UTF-8
每条请求以换行符结束
```

执行只读 Python 的已知请求格式：

```json
{
  "type": "execute_code",
  "params": {
    "code": "<Blender Python>"
  }
}
```

服务端返回一个 JSON 对象。由于实际响应外壳可能因 MCP 版本不同而变化，客户端必须：

- 保存原始响应用于诊断，但正常日志不得泄露完整本地路径；
- 兼容结果字段是字符串、对象或嵌套对象的情况；
- 对连接拒绝、超时、非 JSON、截断 JSON和 Blender Python 异常分别报错；
- 不要猜测更多未验证的 MCP 消息类型。

## 5. Blender 只读探针

向 Blender 发送一段只读 Python。该代码只能读取数据并打印/返回带唯一标记的 JSON，不得写入任何 `bpy` 数据。

建议返回结构：

```json
{
  "protocol": "toon-tuner-scene-probe/1",
  "blender": {
    "version": "5.2.1 LTS",
    "file_path": "<MODEL_DIR>/model.blend",
    "is_saved": true
  },
  "scene": {
    "name": "Scene",
    "render_engine": "BLENDER_EEVEE_NEXT",
    "resolution": [1080, 1980, 100],
    "frame_current": 1,
    "camera": "Camera"
  },
  "objects": {
    "total": 336,
    "mesh_count": 139,
    "visible_mesh_count": 1,
    "light_count": 29,
    "camera_count": 1
  },
  "role_candidates": [
    {
      "name": "model_mesh",
      "polygons": 35544,
      "material_slots": 19,
      "visible": true,
      "hide_render": false
    }
  ]
}
```

角色候选筛选仅用于展示，不在本阶段自动决定目标：

- 对象类型为 `MESH`；
- 有材质槽；
- 按面数降序；
- 最多返回 10 个；
- 不改变 Blender 的活动对象或选择状态。

探针执行前后必须验证以下状态没有变化：

- 当前场景名称；
-活动对象名称；
- 已选择对象名称集合；
- 当前帧；
-当前文件路径。

可以在自动测试中用假 MCP 校验请求内容；在真实 Blender 手工验收时记录上述前后值。

## 6. 后端目录建议

在沿用仓库现状的前提下，建议形成：

```text
src/
├── server/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── blender_mcp.py
│   ├── scene_probe.py
│   ├── models.py
│   └── errors.py
└── web/
    ├── index.html
    ├── app.js
    └── styles.css

tests/
├── test_blender_mcp.py
├── test_scene_probe.py
├── test_api.py
└── fake_mcp_server.py

config.example.json
requirements.txt
```

职责：

- `config.py`：加载 host、port、连接超时等非敏感配置。
- `blender_mcp.py`：TCP 行分隔 JSON 客户端，不含业务逻辑。
- `scene_probe.py`：构造只读 Blender Python、解析结果。
- `models.py`：API 响应和错误模型。
- `app.py`：FastAPI 路由、静态页面和生命周期。
- `fake_mcp_server.py`：测试连接成功、超时、坏 JSON 和 Blender 异常。

## 7. 配置要求

仓库提交 `config.example.json`：

```json
{
  "blender_mcp": {
    "host": "127.0.0.1",
    "port": 9876,
    "connect_timeout_seconds": 1.5,
    "response_timeout_seconds": 5.0
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8765
  }
}
```

真实本地配置使用 `config.local.json`，必须在 `.gitignore` 中排除。环境变量可以覆盖配置，但不要在第一版做复杂配置系统。

## 8. API 需求

### `GET /api/health`

只检查本地控制服务，不连接 Blender。

成功：

```json
{
  "ok": true,
  "service": "toon-tuner",
  "version": "0.1.0"
}
```

### `GET /api/blender/status`

快速测试 TCP 端口和协议是否可用。允许执行最小只读版本探针，但不获取完整场景。

返回状态至少区分：

- `connected`
- `disconnected`
- `timeout`
- `protocol_error`
- `blender_error`

### `GET /api/blender/scene`

执行完整只读场景探针并返回第 5 节结构。

### `POST /api/blender/reconnect`

清理客户端连接状态并立即重新检测。由于底层是短连接时，此接口仍需保留稳定的前端语义。

### 错误响应

使用稳定错误码：

```json
{
  "ok": false,
  "error": {
    "code": "BLENDER_CONNECTION_REFUSED",
    "message": "无法连接 Blender MCP 127.0.0.1:9876",
    "retryable": true
  }
}
```

不要把 Python traceback 或完整 MCP 原始响应直接显示给普通界面；诊断详情可以折叠显示并做路径脱敏。

## 9. Web 页面需求

单页即可，不追求最终视觉设计。

页面应包含：

### 连接卡片

- 绿色：已连接；
- 黄色：正在检测；
- 红色：未连接或协议错误；
- 显示 `127.0.0.1:9876`；
- “重新连接”按钮；
- 最近检测时间。

### Blender 信息

- Blender 版本；
- 当前文件名；
- 文件是否已保存；
- 活动场景；
- 渲染引擎；
- 当前相机；
- 分辨率。

默认只显示文件名，不在显眼位置展示完整本机路径；完整路径放在可展开的诊断区。

### 场景对象摘要

- 对象总数；
- 网格、灯光和相机数量；
- 角色候选表：对象名、面数、材质槽数、是否可见、是否参与渲染。

页面首次打开时自动检测一次。此后不进行高频轮询；第一版可以每 5 秒刷新连接状态，完整场景信息只在连接成功或点击刷新时获取。

### 空状态与错误

- Blender 未启动；
- 9876 未监听；
- MCP 返回无法解析的数据；
- Blender 当前没有打开已保存文件；
- 场景没有网格；
- 请求超时。

每种情况都要给出用户可以采取的下一步，而不是只显示异常字符串。

## 10. 安全要求

- FastAPI 与 MCP 客户端只能连接/监听 localhost。
- 对 MCP host 配置做校验，MVP 不允许配置远程地址。
- 任何来自浏览器的请求都不能直接携带并执行任意 Python。
- Blender Python 只能来自后端内置的固定探针模板。
- 禁止提供 `/execute`、`/eval` 或任意代码执行 API。
- 日志默认隐藏用户名和绝对路径。
- 不在仓库保存 Blender 响应样本中的真实本地路径。
- 前端展示的所有文本使用 `textContent`，不得把 Blender 对象名作为 HTML 注入。

## 11. 测试要求

至少覆盖：

1. 假 MCP 正常返回完整场景；
2. 端口拒绝连接；
3. 连接成功但响应超时；
4. 响应不是 JSON；
5. JSON 分多次 TCP chunk 返回；
6. Blender 探针返回错误；
7. Unicode 文件名、场景名和对象名；
8. 空场景；
9. 候选角色按面数排序并限制 10 个；
10. API 错误码稳定；
11. 前端至少完成一次手工断连/重连测试；
12. 探针中不存在赋值 `bpy` 数据、保存、渲染、导入等操作。

运行命令应写入 README，例如：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
uvicorn src.server.app:app --host 127.0.0.1 --port 8765
```

## 12. 验收标准

全部满足才算本阶段完成：

1. Windows 上一条命令可启动服务。
2. 浏览器打开 `http://127.0.0.1:8765` 可见连接页面。
3. Blender MCP 在线时，5 秒内显示版本和当前场景摘要。
4. Blender MCP 离线时，页面不崩溃，显示明确的重新连接提示。
5. Blender 恢复后无需重启 Web 服务即可重新连接。
6. 中文路径、对象名和场景名正常显示。
7. 读取场景前后，选择状态、活动对象、当前帧和文件状态不变。
8. 不产生新的 `.blend`、PNG、材质、灯光、节点或修改器。
9. 自动化测试全部通过。
10. 仓库没有真实 `.blend`、`.pmx`、贴图、日志、绝对用户路径或凭据。
11. README 写明安装、启动、测试和常见故障排查。
12. 提交一个范围清晰的 commit，不夹带后续调参功能。

## 13. 开发 AI 的交付物

开发 AI 完成后必须提供：

- 修改/新增文件清单；
- 架构说明；
- 本地启动命令；
- 测试命令与测试结果；
- 真实 Blender 手工验收结果；
- 已知限制；
- 下一阶段接入“基线 + 曝光调节 + 自动预览”所需的接口建议；
- Git diff 摘要；
- 不要自动 push，不要修改仓库可见性，不要创建 Release。

## 14. 可直接发送给开发 AI 的任务提示

```text
请在当前 Cartoon-Model-Shader 仓库中实现“MVP-01：只读 Blender 连接闭环”。

开始前完整阅读 README.md、docs/需求文档.md、reference/参数面清单.json、仓库内的 AGENTS.md（如有）以及《AI交付_第一步实施方案.md》。先检查现有代码，保留并复用合理实现。

目标是在 Windows 上启动一个仅监听 127.0.0.1 的本地 Web 服务，由普通浏览器访问；服务通过现有 Blender MCP TCP 端口 127.0.0.1:9876，以行分隔 JSON 的 execute_code 请求执行固定的只读 Blender 探针，展示 Blender 版本、当前文件、活动场景、渲染引擎、相机、分辨率、对象统计和最多 10 个角色候选。

本阶段绝对禁止修改 Blender 场景，禁止渲染、保存、导入、追加资源、执行用户提供的任意 Python，也不要实现曝光、辉光或其他调参功能。不得使用固定 sleep。必须处理断线、超时、坏 JSON、分块响应、Unicode 和空场景，并使用假 MCP 服务编写自动化测试。

优先使用 Python 3.11+、FastAPI、Uvicorn、Pydantic、原生 HTML/CSS/JavaScript 和 pytest；若仓库已有不同技术栈，沿用现状并解释原因。不要自动 push。

完成后运行测试，并报告文件清单、启动命令、测试结果、真实 Blender 验收结果、已知限制和下一阶段接口建议。以《AI交付_第一步实施方案.md》的验收标准为完成定义。
```
