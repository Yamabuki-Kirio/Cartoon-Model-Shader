# Render Pipeline v3.1

这是 Cartoon-Model-Shader 的独立材质适配与一键渲染组件。它负责 PMX 预检、材质分类、本地确认、侧车映射和单次渲染调度；只有 `SUCCESS` 被视为可交付成品。

## 前置条件

- Windows、Blender 5.x；
- Blender MCP 正在 `127.0.0.1:9876` 监听；
- 包含已调校节点组的源 `.blend`；
- 本机 PMX 及贴图素材。素材本身不进入本仓库。

## 配置

可使用命令行参数，或设置对应环境变量：

| 参数 | 环境变量 | 用途 |
|---|---|---|
| `--model-root` | `TOON_MODEL_ROOT` | 按名称或指纹查找 PMX 的素材根目录 |
| `--source-blend` | `TOON_SRC_BLEND` | 提供节点组的源工程 |
| `--blender` | `TOON_BLENDER` | Blender 可执行文件 |
| `--runtime-dir` | `TOON_RUNTIME_DIR` | 预览、任务、锁和运行输出目录 |

仓库不保存本机路径。缺少必要配置时入口会明确拒绝运行。

## 唯一入口

```powershell
python .\render_pipeline\开始渲染.py `
  --pmx "D:\Models\角色\角色.pmx" `
  --model-root "D:\Models" `
  --source-blend "D:\ToonAssets\source.blend" `
  --blender "C:\Program Files\Blender Foundation\Blender\blender.exe"
```

也可以用 `--fingerprint` 或 `--name` 在模型根目录下查找模型：

```text
预检 → 自动分类 → 查找侧车映射 → 严格准入
     ├─ 无需确认：直接渲染
     └─ 需要确认：打开本地确认页，确认后由服务端唯一续跑
```

浏览器不能提交模型路径或任意命令。自动续跑与页面按钮共用同一状态机，并由跨进程文件锁阻止重复渲染。

## 终态契约

| 终态 | 可交付 |
|---|---|
| `SUCCESS` | 是 |
| `REJECTED` | 否 |
| `DIAGNOSTIC` | 否 |
| `FAILED` | 否 |

运行数据默认写入 `render_pipeline/runtime/`。

侧车映射目录由 `--maps-dir` 全程贯穿（入口 → 确认服务 → 驱动 → 主脚本），默认值是**用户数据目录**
`%LOCALAPPDATA%\CartoonModelShader\model_material_maps`（可用 `TOON_MAPS_DIR` 覆盖），
**不是仓库内的 `render_pipeline/model_material_maps/`** —— 那一个目录只放随代码分发的只读样例
（`example.material-map.json`，由 `tests/test_render_pipeline_repo_guard.py` 守卫）。
用户确认结果与「待填写」模板都属于运行数据，一律不写进仓库代码目录：模板落输出目录，
确认结果落 `--maps-dir` 指向的用户数据目录。

## 测试

```powershell
python -m pytest render_pipeline/tests
```

自动测试不要求提交模型或启动 Blender。真实 Blender 端到端 `SUCCESS` 是人工验收项。
