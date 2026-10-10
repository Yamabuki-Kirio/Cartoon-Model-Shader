# Cartoon-Model-Shader

> **自用项目。**

做 MMD 模型三渲二的时候下载了很多插件 —— Autocel 智能卡通化着色器、Cycles Render Engine、glTF 2.0 format、MCP for Blender、MiaoboxNode、MikuMikuRig、MMD Tools、MMD Tools Append、MMD 自动换头、mmd_kafei_tools、Pose Library、卡渲秘咒。顺序繁多，每次从零开始做出来的效果都不一样，所以做了个脚本自动把模型渲染成我理想中的情况，现在再做可视化界面方便调参数。

## 效果示例

![卡通渲染效果图](docs/assets/render-preview.jpg)

> 角色模型由本项目处理并渲染；图中背景使用 Adobe After Effects（AE）合成。

## 功能

本项目用于自动处理 MMD 模型的卡通渲染流程：

- 自动读取 PMX 模型并识别皮肤、头发、服装、眼睛等材质。
- 根据材质特征自动匹配卡通着色方案。
- 对无法确定的材质提供浏览器确认界面。
- 自动配置 Blender 渲染管线并输出渲染结果。
- 在浏览器中调整曝光、辉光、取景和 Cel 色带。
- 支持预览、参数预设、工程备份与安全保存。
- 预览或保存失败时尝试恢复到修改前的基线。

## 架构

```text
浏览器界面
    ↓ HTTP
本地控制服务
    ↓ Blender MCP
Blender 当前工程
```

浏览器负责参数调整和状态展示；本地服务负责材质识别、参数校验、任务管理、备份与保存；Blender 负责应用着色器、生成预览和正式渲染。

## 如何开始

要求：

- Windows 10/11
- Python 3.11 或 3.12
- Node.js 22
- Blender 与 Blender MCP

安装并启动：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd web
npm ci
npm run build
cd ..

uvicorn src.server.app:app --host 127.0.0.1 --port 8765
```

在 Blender 中打开工程并启动 MCP，然后访问：

- 调参页面：<http://127.0.0.1:8765/>
- v4 工作台：<http://127.0.0.1:8765/next>

## 注意事项

- 当前项目仍在开发和真实 Blender 验收阶段。
- 首次使用请操作工程副本，不要直接使用唯一原件。
- 覆盖工程前请确认目标路径和备份文件。
- `/next` 当前主要开放 Cel 色带功能，完整参数面仍在开发。

详细说明：

- [技术设计](docs/技术设计.md)
- [安全检查](docs/安全检查.md)
