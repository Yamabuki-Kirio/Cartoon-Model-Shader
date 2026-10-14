"""材质深度识别：从材质输出反向追踪实际生效的节点链。

模块边界（与 ``surface`` 包同一条纪律：纯函数式、不碰 Blender、不持状态）
--------------------------------------------------------------------------
* ``graph``     —— 只读图模型（节点 / 连线 / 邻接）与循环检测；
* ``identity``  —— 稳定参数身份与结构哈希（数值不进结构哈希）；
* ``trace``     —— 从 Material Output → Surface 反向追踪（含节点组穿透、循环检测）；
* ``semantics`` —— 由节点类型/插座名推断参数语义与识别等级；
* ``schema``    —— 生成递归参数 schema（复用 ``surface.schema`` 的节点模型）；
* ``probe``     —— 解析 ``surface_probe`` 的 describe 载荷，取某材质的图。

**禁止**：任意 Python、任意 Blender 属性表达式、任意命令、客户端生成的节点路径、
客户端指定的文件系统路径。前端只能提交服务端生成的参数 ID 与合法值。
写入一律走 ``surface.binding.BINDING_WHITELIST`` 内的字段，且节点身份/插座身份
必须由服务端探测生成。
"""

from __future__ import annotations

SCHEMA_VERSION = "toon-material-graph/1"

__all__ = ["SCHEMA_VERSION"]
