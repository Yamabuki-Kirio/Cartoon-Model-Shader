# -*- coding: utf-8 -*-
"""
=====================================================================================
 一键卡通渲染  ·  One-Click Toon Render Pipeline
=====================================================================================
 把 Sakura_1.blend 里那套"MMD 卡通渲染"完整搬到另一个模型上，并直接出图。

 用法（Blender 内运行，Agent 模式下也可由 --python 调起）：
   1) 用 MMD Tools 把新模型 (.pmx) 导入当前工程
   2) 选中模型网格（或设 TARGET_MODE = "ALL_VISIBLE"）
   3) 脚本编辑器 → 打开本文件 → 运行脚本
   4) 跑完会在 OUT_DIR 下得到：带辉光 PNG、无辉光 PNG、成品 .blend

 它会做的事：
   [1] 从源工程追加卡通着色节点组（Cel_* / RayToon_* / Sakura_Hair_Reference /
       通用描边3.1 / Tonemapping）与 Outline 材质 —— 优先原样搬运，保证 100% 一致
   [2] 追加失败时自动回退为内置的"Shader to RGB 二分"生成器
   [3] 渲染环境：EEVEE + 采样 + AgX High Contrast + sunset.exr 世界(0.12)
   [4] 视图层光照同步属性（Autocel 的 LightVector / LightColor / LightEnergy 等）
   [5] 合成器：Glare Bloom 辉光 → 输出
   [6] 材质装配：按 MMD 材质名自动归类到对应 Cel 组，重建 贴图→组→Alpha→输出
   [7] 反壳法描边：给每个网格挂几何节点修改器（线宽 0.91mm）
   [8] 灯光装置：21 盏启用灯 + 6 盏备用灯，参数与源工程逐项一致
   [9] 相机：78mm 特写（带 shift 偏构图）
   [10] 一键渲染输出
=====================================================================================
"""

import bpy
import os
import re
import json
import math
import sys

# =====================================================================================
#  配置区  —— 换模型时基本只需要改这一块
# =====================================================================================

# 提供节点组/材质的"源工程"。这几个卡通节点组只存在于该文件里，必须从它搬运
# （审计整改项 7：路径可用环境变量覆盖，便于迁移到别的机器 / 别的目录）
SOURCE_BLEND = os.environ.get("TOON_SRC_BLEND", r"<SOURCE_RIG_DIR>\Sakura_1.blend")

# 输出目录
OUT_DIR = os.environ.get("TOON_OUT_DIR", r"<PIPELINE_DIR>")

# 额外输入文件（驱动脚本可写入，如源 pmx 路径），会记进 run_manifest.json 的输入哈希
EXTRA_INPUTS = []          # [(role, path), ...]

# 要处理的模型：
#   "SELECTED"     只处理当前选中的网格
#   "ALL_VISIBLE"  处理当前场景里所有可见（未隐藏）的网格
TARGET_MODE = "SELECTED"

# 是否在跑完后渲染。设为 False 就只搭场景不出图
DO_RENDER = True
# 是否渲染到透明背景（源工程为 True）。
# 如果你的模型本身是白色/浅色，透明 PNG 在很多看图器里会显示在白底上，
# 结果"白模型消失在白底里"。这时改成 False，就能带上世界环境当背景。
FILM_TRANSPARENT = True

# 渲染分辨率（None 表示沿用源工程的 1080x1980）
RESOLUTION = None          # 例如 (1080, 1980)

# 是否加描边。默认开启。
#
# 描边用的是反壳法：把网格复制一层、沿法向外扩 0.91mm、翻转面，
# 再靠材质的"背面剔除"把近侧那层壳剔掉，只留下剪影外的一圈 —— 源工程就是这么做的。
#
# 【如果开启后模型整个变成死黑】十有八九不是描边的问题，而是模型材质被判成了"半透明"：
#   半透明的模型会透出背后的黑色描边壳，看起来就是一片黑。
#   脚本里的 detect_transparent() 用 MMD 材质自身的 alpha 属性判断，正常情况下不会误判；
#   若你手动改过材质，请检查材质是不是 OPAQUE。
ENABLE_OUTLINE = True

# 描边实现方式：
#   "builtin" —— 用脚本内置的反壳法（逻辑完全可控，推荐）
#   "source"  —— 复用源工程的「通用描边3.1」节点组（与原工程一致）
OUTLINE_IMPL = "builtin"

# 描边线宽（毫米），源工程实测 0.91
OUTLINE_WIDTH_MM = 0.91

# 是否把原材质改名备份（方便对比/回退），True 会把原材质重命名为 原名.orig
KEEP_ORIGINAL_MATERIALS = True

# -------------------------------------------------------------------------------------
#  材质归类规则：按 MMD 材质名（正则）匹配到对应卡通节点组
#  顺序即优先级，第一个匹配上的生效。可按自己的模型命名习惯随意增删。
# -------------------------------------------------------------------------------------
MATERIAL_RULES = [
    # 影子 / 阴影面（放在 hair 之前，避免 hairshadow 被当成头发）
    (r"影|shadow",                                     "Cel_Dark"),
    # 脸部专用（RayToon 柔和面部）
    (r"face|顔|脸",                                    "RayToon_Face_Soft"),
    # 眼睛 / 瞳孔 / 眼高光
    (r"eye|目|眼|瞳|pupil|iris",                       "RayToon_Eyes_Unlit"),
    # 牙齿 / 舌头 / 睫毛 / 眉毛（源工程里这些走 Cel_Eyes）
    (r"teeth|歯|齿|牙|tongue|舌|lash|睫毛|brow|眉|まつげ", "Cel_Eyes"),
    # 自发光件
    (r"glow|光る|emission|emissive|hika",              "Cel_Eyes"),
    # 头部头发：源工程里「头_Material1/6/7/10」这类头部发片走专用 Sakura_Hair_Reference。
    # 这里先归为 Cel_Hair，再由 USE_HAIR_REFERENCE_GROUP + HEAD_HAIR_PATTERNS 决定是否升级成参考组。
    (r"^头_Material\d+$",                              "Cel_Hair"),
    # 头发
    (r"hair|髪|髮|发|前髪|後髪|ahoge|アホ毛",           "Cel_Hair"),
    # 服装布料（放在皮肤之前：像「スカート腕ヘドフォン」这类合并网格要归到衣服）
    (r"cloth|服|衣|布|上着|下着|ブラウス|スカート|スパッツ|シャツ|ズボン|ネクタイ|"
     r"靴|ソックス|手袋|袖|skirt|shirt|dress|tie|sock|boot|glove|blouse|spats|ribbon|リボン",
     "Cel_Cloth"),
    # 内着 / 鞋 / 腰带 背带（模型A 暴露的 11 个未匹配材质里有 10 个属于这一类）
    (r"內著|内着|内衣|下着|鞋|靴|帶|帯|带|belt|strap|sash|サッシュ",
     "Cel_Cloth"),
    # 躯干网格：源工程里「体」（如 身_体）是躯干外壳/衣服本体，走 Cel_Cloth（不是皮肤）
    (r"体",                                            "Cel_Cloth"),
    # 皮肤（含日文的手/脚/腕等）
    (r"skin|肌|皮膚|hada|body|bodyskin|腕|手|おてて|足|脚|おみあし|ひざ|もも|腿|首|肩|胸", "Cel_Skin"),
    # 饰品 / 金属 / 屏幕等硬表面
    (r"飾|饰|metal|金属|screen|屏幕|accessor|ornament|lens|ガラス|glass|ピアス|ネックレス|"
     r"headband|steel|護額|护额|額当て|メタル",
     "Cel_Dark"),
]
# 未命中任何规则时的兜底节点组
FALLBACK_GROUP = "Cel_Dark"

# 这些名字命中的材质是 MMD 的骨骼指示器 / 接地引导，本来就不该出图，直接跳过不动
SKIP_MATERIAL_PATTERNS = [
    r"インジケータ", r"インジケーター", r"ガイド", r"捩", r"グルーブ", r"グリップ",
    r"操作", r"ダミー", r"^Material$", r"^Dots Stroke$", r"^Outline", r"描边",
]

# 头发分组策略（源工程实测）：
#   身体自带的头发（身_髮 / 身_髮+）走通用 Cel_Hair；
#   头部头发（头_Material1/6/7/10 这类）走专用的 Sakura_Hair_Reference。
# USE_HAIR_REFERENCE_GROUP=True 时，名字命中 HEAD_HAIR_PATTERNS 的头发材质改用 Sakura_Hair_Reference。
# （该参考组是按原模型头发调的；换别的模型若发色怪异，关掉它即可退回 Cel_Hair）
USE_HAIR_REFERENCE_GROUP = True
HEAD_HAIR_PATTERNS = [r"^头_Material\d+$"]

# -------------------------------------------------------------------------------------
#  要搬运的资源清单
# -------------------------------------------------------------------------------------
APPEND_NODE_GROUPS = [
    # ★ "Compositor" 是源工程的合成器节点组。追加它会把 Tonemapping 等整套依赖一并带进来
    #   （Blender 的 libraries.load 不会单独列出 Tonemapping，只能靠依赖链拿到）
    "Compositor",
    "Cel_Skin", "Cel_Hair", "Cel_Cloth", "Cel_Dark", "Cel_Eyes",
    "RayToon_Face_Soft", "RayToon_Eyes_Unlit",
    "Sakura_Hair_Reference",
    "通用描边3.1", "通用描边3.002", "通用描边",
]
APPEND_MATERIALS = ["Outline"]

# 描边节点组候选（按顺序取第一个存在的）
OUTLINE_GROUP_CANDIDATES = ["通用描边3.1", "通用描边3.002", "通用描边", "AI_Outline_Fallback"]

# 渲染引擎 / 色彩管理（源工程实测值）
VIEW_TRANSFORM = "AgX"
LOOK = "AgX - High Contrast"
WORLD_STRENGTH = 0.12
HDRI_NAME = "sunset.exr"

# ★ 基准贴图的色彩空间。
#
#   ⚠ 口径修正（审计整改项 2）：原文写「源工程实测全部是 Filmic sRGB」是错的。
#     实测 22 张贴图是混合的：['Filmic sRGB', 'sRGB']。
#     准确表述：贴图色彩空间是本模型偏暗的【主要贡献因素之一】，不是唯一根因；
#     也不能推出「所有新模型都应统一改成 Filmic sRGB」。
#     run_texture_ledger() 会在运行后逐张贴图记录：用途 / 原色彩空间 / 改后色彩空间。
#
#   趋势是成立的：同一张贴图，"Filmic sRGB" 解出的线性值比普通 "sRGB" 亮得多。
#   实测：把源工程贴图【一次性全部】改成 "sRGB" 后，整图平均亮度 0.502 → 0.345
#   （复刻版当时是 0.365）。所以它是主因之一，但该实验一次性动了多张不同初始状态的
#   贴图、也没区分哪些真参与渲染，不足以证明唯一因果。
#   pmx 导入进来的贴图默认是 "sRGB"，这里按源工程主流取值统一设置。
#   如果新模型用 Filmic sRGB 会过曝，改成 "sRGB" 或 None（None = 保持贴图原样）。
TEXTURE_COLORSPACE = "Filmic sRGB"

# -------------------------------------------------------------------------------------
#  ★ 合成路径模式（审计整改项 3）：必须二选一，不能既声称「方法一致」又输出改良结果
#
#   "faithful" —— 忠实复刻：照抄源工程【当前实际生效】的合成路径。
#                 源工程合成器里 Kafka_Arm_Bypass_Final 这个 Mix 节点
#                 （A=Tonemapping / B=渲染层 / Factor=Cryptomatte.Matte，而 matte≈0）
#                 实际把 Tonemapping 与 Bloom 都旁路了 → 有效路径 = 渲染层直出。
#   "enhanced" —— 增强辉光：渲染层 → Glare 辉光 → Tonemapping(源工程组) → 输出。
#                 这是【改良版】，好看但不是源工程当前的合成路径。
# -------------------------------------------------------------------------------------
PIPELINE_MODE = os.environ.get("TOON_PIPELINE_MODE", "faithful")

# -------------------------------------------------------------------------------------
#  ★ 严格材质匹配（审计整改项 6）：未命中规则的材质不再静默落进 Cel_Dark
#   True  —— 未命中即报 ERROR 并终止，宁可不渲染也不假装成功
#   False —— 回退到 FALLBACK_GROUP，但在日志与台账里显式列出
# -------------------------------------------------------------------------------------
STRICT_MATERIAL_MATCH = True

# 辉光（源工程实测值）
GLOW = {"Type": "Bloom", "Quality": "Medium", "Threshold": 1.0,
        "Smoothness": 0.1, "Strength": 2.0, "Size": 0.5, "Maximum": 10.0}

# 相机
CAMERA = dict(name="Toon_Face_Closeup_Camera", lens=78.0, sensor=36.0,
              shift_x=0.40, shift_y=-0.16, loc=(-0.3066, -1.65, 1.25),
              rot=(1.5292, 0.0, 0.0038), clip=(0.1, 1000.0))

# 自动取景：True 时忽略上面的 loc/rot/shift，改为按目标模型包围盒自动构图（正面平视）
# 换身高差异较大的模型时打开它更省事；想要 100% 复刻原构图就保持 False
AUTO_FRAME = False
AUTO_FRAME_MARGIN = 1.35      # 留白系数，越大画面越松

# -------------------------------------------------------------------------------------
#  灯光装置：源工程实测的 21 盏启用灯 + 6 盏备用灯
#  字段：(名称, 类型, 能量, 颜色, 位置, 旋转, 集合, 附加参数)
# -------------------------------------------------------------------------------------
LIGHTS_ACTIVE = [
    ("AutoSun", "SUN", 1.0, (1.0, 1.0, 1.0), (0.4914, -0.3225, 1.4121), (1.3077, 0.0, -0.6974),
     "Collection", {"angle": 0.015}),

    ("Double Side Accent.Left", "POINT", 200.0, (1.0, 1.0, 1.0), (-2.5, -1.3, 0.8), (0.0, 0.0, 0.0),
     "集合 4 2", {"shadow_soft_size": 0.5}),
    ("Double Side Accent.Left Accent.001", "AREA", 23.5619, (0.0331, 0.0331, 1.0), (-2.3, 0.5, 1.8),
     (1.5708, 0.0, -1.5708), "集合 4 2", {"area": (0.5, 1.0)}),
    ("Double Side Accent.Left Accent.002", "AREA", 23.5619, (0.0331, 0.0331, 1.0), (-1.7321, -1.0, 1.8),
     (1.5708, 0.0, -1.0472), "集合 4 2", {"area": (0.5, 1.0)}),
    ("Double Side Accent.Left Accent.003", "AREA", 23.5619, (0.0331, 0.0331, 1.0), (-1.7321, 1.0, 0.7),
     (1.5708, 0.0, -2.0944), "集合 4 2", {"area": (0.5, 1.0)}),
    ("Double Side Accent.Left Accent.004", "AREA", 23.5619, (0.0331, 0.0331, 1.0), (-2.3, -0.5, 0.7),
     (1.5708, 0.0, -1.5708), "集合 4 2", {"area": (0.5, 1.0)}),
    ("Double Side Accent.Right", "POINT", 200.0, (1.0, 1.0, 1.0), (2.5, -1.3, 0.8), (-3.1416, 0.0, 0.0),
     "集合 4 2", {"shadow_soft_size": 0.5, "scale": (-1.0, -1.0, -1.0)}),
    ("Double Side Accent.Right Accent.001", "AREA", 23.5619, (1.0, 0.1329, 0.1329), (2.3, 0.5, 1.8),
     (1.5708, 0.0, 1.5708), "集合 4 2", {"area": (0.5, 1.0), "scale": (-1.0, 1.0, 1.0)}),
    ("Double Side Accent.Right Accent.002", "AREA", 23.5619, (1.0, 0.1329, 0.1329), (1.7321, -1.0, 1.8),
     (-1.5708, 0.0, 1.0472), "集合 4 2", {"area": (0.5, 1.0), "scale": (-1.0, -1.0, -1.0)}),
    ("Double Side Accent.Right Accent.003", "AREA", 23.5619, (1.0, 0.1329, 0.1329), (1.7321, 1.0, 0.7),
     (-1.5708, 0.0, 2.0944), "集合 4 2", {"area": (0.5, 1.0), "scale": (-1.0, -1.0, -1.0)}),
    ("Double Side Accent.Right Accent.004", "AREA", 23.5619, (1.0, 0.1329, 0.1329), (2.3, -0.5, 0.7),
     (-1.5708, 0.0, 1.5708), "集合 4 2", {"area": (0.5, 1.0), "scale": (-1.0, -1.0, -1.0)}),

    ("Lamp", "SPOT", 100.0, (1.0, 0.9893, 0.9576), (-0.2584, -2.5629, 2.2959),
     (-1.1694, -0.0092, 2.9796), "打光", {"spot": (1.309, 0.5683), "soft": 0.38, "scale": (2.0457,) * 3}),
    ("Lamp.001", "SPOT", 250.0, (1.0, 0.8002, 0.4447), (1.7271, 2.8237, 2.3171),
     (-0.1906, -1.0352, -1.9798), "打光", {"spot": (1.309, 1.0), "soft": 0.353, "scale": (2.0457,) * 3}),
    ("Lamp.002", "SPOT", 521.4, (0.6359, 0.7699, 1.0), (-2.907, 3.2176, 2.3659),
     (-0.2018, -1.0792, -0.514), "打光", {"spot": (1.309, 1.0), "soft": 0.373, "scale": (2.0457,) * 3}),

    ("Left Accent.Accent", "AREA", 15.708, (1.0, 0.3185, 0.2462), (-2.0, -0.35, 1.0),
     (1.5708, 0.0, -1.3963), "集合 4", {"area": (1.0, 1.0), "specular": 0.0}),
    ("Left Accent.Key", "SPOT", 50.0, (1.0, 1.0, 1.0), (0.0, -1.5, 2.0),
     (-0.7854, -0.2269, 2.9671), "集合 4", {"spot": (1.309, 0.15), "soft": 0.1}),
    ("Left Accent.Rim", "AREA", 11.781, (1.0, 1.0, 1.0), (0.0, 1.5, 1.0),
     (1.5708, 0.0, 3.1416), "集合 4", {"area": (1.0, 0.25), "specular": 0.25}),
    ("Left Accent.Top", "SPOT", 150.0, (1.0, 1.0, 1.0), (0.3, 0.15, 3.0),
     (-0.2094, 0.0, -1.3963), "集合 4", {"spot": (1.309, 0.85), "soft": 0.333, "specular": 0.2}),

    # mmd_kafei_tools 三点照明（主光 / 辅光 / 背光，能量 150 / 30 / 250）
    ("主光", "AREA", 150.0, (1.0, 0.6376, 0.5029), (1.0, -1.7321, 0.5359),
     (1.309, 0.0, 0.5236), "灯光", {"area": (1.0, 0.25), "volume": 0.0, "hide": True}),
    ("辅光", "AREA", 30.0, (0.7011, 0.8228, 1.0), (-1.299, -0.75, 0.4019),
     (1.309, 0.0, -1.0472), "灯光", {"area": (1.0, 0.25), "volume": 0.0, "hide": True}),
    ("背光", "AREA", 250.0, (0.5029, 0.7011, 1.0), (0.0, 0.7071, 0.7071),
     (-0.7854, 0.0, 0.0), "灯光", {"area": (1.0, 0.25), "volume": 0.0}),

    ("日光.002", "SUN", 2.0, (1.0, 1.0, 1.0), (0.0, 0.0, 1.7921),
     (-0.7854, 0.0, 4.0143), "换头替换模型", {"angle": 0.00918, "soft": 0.125}),
    ("日光XT.001", "SUN", 0.78, (1.0, 1.0, 1.0), (0.0, 0.0, 1.7921),
     (-0.7854, 0.0, 4.0143), "换头替换模型", {"angle": 0.00918, "soft": 0.125}),
]

LIGHTS_SPARE = [
    ("Toon_Key",  "AREA", 18.0, (1.00, 0.94, 0.90), (-0.45, -1.45, 1.78), (1.2232, 0.0, -0.3009),
     "卡通三点光", {"area": (4.80, 0.25), "hide": True}),
    ("Toon_Fill", "AREA",  2.0, (0.78, 0.86, 1.00), ( 0.35, -1.55, 1.35), (1.4892, 0.0,  0.2221),
     "卡通三点光", {"area": (4.50, 0.25), "hide": True}),
    ("Toon_Rim",  "AREA", 18.0, (0.64, 0.48, 1.00), ( 0.75,  0.55, 1.65), (1.1646, 0.0,  2.2035),
     "卡通三点光", {"area": (3.00, 0.25), "hide": True}),
    ("Toon_Top",  "AREA",  5.0, (0.82, 0.70, 1.00), ( 0.00, -0.05, 2.45), (0.0568, 0.0,  0.0000),
     "卡通三点光", {"area": (0.75, 0.25), "hide": True}),
    ("Light", "POINT", 1000.0, (1.0, 1.0, 1.0), (4.0762, 1.0055, 5.9039),
     (0.6503, 0.0552, 1.8664), "Collection", {"soft": 0.1, "hide": True}),
    # ★ 日光.001 虽然对象本身 hide_render=False，但它所在的集合
    #   「Merlin Toon Carrier 3.1(不要修改)」在源工程里是 collection.hide_render=True，
    #   所以它【不参与渲染】。放回备用区，别加回启用区。
    ("日光.001", "SUN", 2.0, (1.0, 1.0, 1.0), (0.0, 0.0, 1.7921),
     (-0.7854, 0.0, 4.0143), "卡通三点光", {"angle": 0.00918, "soft": 0.125, "hide": True}),
]

# 内置回退方案的色阶数据（源工程实测值）
CEL_FALLBACK_PRESETS = {
    "Cel_Skin": ([0.38, 0.56, 0.74], [(0.58, 0.46, 0.43), (0.80, 0.69, 0.65), (1.00, 0.94, 0.90)], 0.50),
    "Cel_Hair": ([0.34, 0.55, 0.73], [(0.38, 0.28, 0.36), (0.68, 0.55, 0.64), (1.00, 0.90, 0.94)], 0.50),
    "Cel_Cloth": ([0.34, 0.55, 0.73], [(0.34, 0.38, 0.50), (0.62, 0.67, 0.77), (0.96, 0.97, 1.00)], 0.42),
    "Cel_Dark": ([0.34, 0.56, 0.77], [(0.20, 0.17, 0.25), (0.38, 0.34, 0.46), (0.66, 0.62, 0.72)], 0.33),
    "Cel_Eyes": ([0.30, 0.76], [(0.78, 0.80, 0.79), (0.96, 0.98, 0.97)], 0.48),
    "RayToon_Face_Soft": ([0.34, 0.52, 0.70], [(0.78, 0.70, 0.68), (0.90, 0.84, 0.81), (1.00, 0.96, 0.93)], 0.44),
    "RayToon_Eyes_Unlit": ([0.18, 0.88], [(0.90, 0.92, 0.91), (0.98, 1.00, 0.99)], 0.44),
}


# =====================================================================================
#  通用小工具
# =====================================================================================

LOG = []


def log(msg):
    LOG.append(msg)
    print(msg)


# -------------------------------------------------------------------------------------
#  ★ 贴图台账（审计整改项 4）
#  逐张记录：材质 / 贴图名 / 用途 / 原色彩空间 / 改后色彩空间 / 是否真参与重建后的渲染。
#  运行结束后写进 OUT_DIR/texture_ledger.json。
# -------------------------------------------------------------------------------------
TEXTURE_LEDGER = []
UNMATCHED_MATERIALS = []
SKIPPED_MATERIALS = []       # 已落库的「该跳过」材质（指示器/描边），不算未命中
_IMG_CS_SNAPSHOT = {}        # image.name -> 运行前原始色彩空间
RUN_INFO = {}                # 记录本次运行的实际生效配置（写进 run_manifest.json）

# 贴图用途判定的关键词（按 MMD/常见命名习惯）
TEX_ROLE_PATTERNS = [
    (r"toon|トゥーン|トーン|色階|色阶|階調",                        "toon"),
    (r"spa|スフィア|sphere|sph|加算|乗算|乘算|mul|add",             "sphere"),
    (r"norm|nrm|normal|法線|法线|bump",                             "normal"),
    (r"mask|マスク|alpha|不透明度|透過|透明|影|シャドウ|shadow",      "mask"),
    (r"matcap|縁|エッジ|edge|highlight|ハイライト|spec|鏡面|镜面",   "matcap"),
]


def classify_texture_role(name):
    low = (name or "").lower()
    for pat, role in TEX_ROLE_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return role
    return "base"


def snapshot_image_colorspaces():
    """在任何改写之前，把所有贴图的原始色彩空间记下来（否则后续读到的已被改过）。"""
    _IMG_CS_SNAPSHOT.clear()
    for img in bpy.data.images:
        try:
            _IMG_CS_SNAPSHOT[img.name] = img.colorspace_settings.name
        except Exception:
            _IMG_CS_SNAPSHOT[img.name] = None


def scan_model_textures(targets):
    """
    扫描目标模型【全部】材质节点树里的图像纹理，登记用途与原始色彩空间。
    这一步跑在重建材质之前，所以能如实反映 pmx 导入后的初始状态。
    """
    seen = {}
    for obj in targets:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes:
                continue
            for n in mat.node_tree.nodes:
                if n.bl_idname != "ShaderNodeTexImage" or n.image is None:
                    continue
                key = (mat.name, n.image.name)
                if key in seen:
                    continue
                try:
                    w, h = n.image.size
                except Exception:
                    w, h = 0, 0
                uv = None
                try:
                    vin = n.inputs["Vector"]
                    if vin.is_linked and vin.links[0].from_node.bl_idname == "ShaderNodeUVMap":
                        uv = vin.links[0].from_node.uv_map
                except Exception:
                    pass
                linked = any(o.is_linked for o in n.outputs)
                seen[key] = {
                    "material": mat.name,
                    "image": n.image.name,
                    "role_hint": classify_texture_role(n.image.name),
                    "orig_colorspace": _IMG_CS_SNAPSHOT.get(n.image.name),
                    "new_colorspace": None,
                    "used_as_base": False,
                    "resolution": [w, h],
                    "uv_map": uv,
                    "linked_at_scan": bool(linked),
                }
    return seen


def write_texture_ledger(out_dir, scanned):
    """合并「扫描登记」与「重建后实际使用情况」→ texture_ledger.json"""
    used = {}
    for rec in TEXTURE_LEDGER:
        used[(rec["material"], rec["image"])] = rec
    merged = []
    for key, rec in scanned.items():
        u = used.get(key)
        rec = dict(rec)
        if u is not None:
            rec["used_as_base"] = True
            rec["new_colorspace"] = u["new_colorspace"]
            rec["note"] = "已按 TEXTURE_COLORSPACE 改写"
        else:
            # 没被选作基础色的（球面/toon/法线/遮罩等），重建后不再参与渲染，色彩空间保持原样
            rec["new_colorspace"] = rec["orig_colorspace"]
            rec["note"] = "未修改（重建后不再参与渲染）"
        merged.append(rec)
    # 重建时才出现、扫描时没见到的（理论上不该有）也补上
    for key, u in used.items():
        if key not in scanned:
            merged.append(u)
    merged.sort(key=lambda r: (r["material"], r["image"]))
    summary = {
        "images_total": len({r["image"] for r in merged}),
        "entries_total": len(merged),
        "orig_colorspace_histogram": {},
        "new_colorspace_histogram": {},
        "role_histogram": {},
        "used_as_base_images": sorted({r["image"] for r in merged if r.get("used_as_base")}),
    }
    for r in merged:
        for field, bucket in (("orig_colorspace", "orig_colorspace_histogram"),
                              ("new_colorspace", "new_colorspace_histogram"),
                              ("role_hint", "role_histogram")):
            k = str(r.get(field))
            summary[bucket][k] = summary[bucket].get(k, 0) + 1
    path = os.path.join(out_dir, "texture_ledger.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "entries": merged}, f,
                      ensure_ascii=False, indent=1)
        log(f"      贴图台账 → {path}")
        log(f"      其中 原始色彩空间分布：{summary['orig_colorspace_histogram']}")
        log(f"          实际使用的基础贴图 {len(summary['used_as_base_images'])} 张 "
            f"（占登记 {summary['images_total']} 张中的一部分，其余为球面/toon/法线/遮罩，"
            f"重建后不再参与渲染）")
    except Exception as e:
        log(f"      ! 贴图台账写出失败：{e}")
    return summary


def sha256_of(path):
    """算文件 SHA-256（审计整改项 8：输入/脚本/Blend/PNG 全部留指纹）"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None


def write_run_manifest(out_dir, pre_existing, extra_inputs=(), extra_info=None):
    """本次运行的完整清单：输入哈希 + 脚本哈希 + 输出哈希 + 生效配置"""
    import time
    outputs = []
    for fn in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, fn)
        if not os.path.isfile(p):
            continue
        outputs.append({
            "name": fn,
            "size": os.path.getsize(p),
            "sha256": sha256_of(p),
            "generated_this_run": fn not in pre_existing,
        })
    inputs = [{"role": "source_blend", "path": SOURCE_BLEND,
               "sha256": sha256_of(SOURCE_BLEND)}]
    for role, path in extra_inputs:
        inputs.append({"role": role, "path": path, "sha256": sha256_of(path)})
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blender_version": bpy.app.version_string,
        "pipeline_mode": PIPELINE_MODE,
        "strict_material_match": STRICT_MATERIAL_MATCH,
        "settings": {
            "view_transform": VIEW_TRANSFORM, "look": LOOK,
            "world_strength": WORLD_STRENGTH, "hdri": HDRI_NAME,
            "texture_colorspace": TEXTURE_COLORSPACE,
            "film_transparent": FILM_TRANSPARENT,
            "auto_frame": AUTO_FRAME, "enable_outline": ENABLE_OUTLINE,
            "outline_impl": OUTLINE_IMPL, "outline_width_mm": OUTLINE_WIDTH_MM,
            "glow": GLOW,
        },
        "run_info": dict(RUN_INFO),
        "unmatched_materials": list(UNMATCHED_MATERIALS),
        "skipped_materials": list(SKIPPED_MATERIALS),
        "pre_existing": list(pre_existing),
        "inputs": inputs,
        "outputs": outputs,
    }
    if extra_info:
        manifest.update(extra_info)
    path = os.path.join(out_dir, "run_manifest.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        log(f"      运行清单 → {path}")
    except Exception as e:
        log(f"      ! 运行清单写出失败：{e}")
    return manifest


def sock(node, name, type_hint=None):
    for s in node.inputs:
        if s.name == name and (type_hint is None or s.type == type_hint):
            return s
    return None


def mix_rgba(node):
    fac = next(s for s in node.inputs if s.name == "Factor" and s.type == "VALUE")
    a = next(s for s in node.inputs if s.name == "A" and s.type == "RGBA")
    b = next(s for s in node.inputs if s.name == "B" and s.type == "RGBA")
    res = next(s for s in node.outputs if s.name == "Result" and s.type == "RGBA")
    return fac, a, b, res


def get_comp_tree(scene):
    t = getattr(scene, "node_tree", None)
    if t is not None:
        return t
    return getattr(scene, "compositing_node_group", None)


def ensure_collection(scene, name, cache):
    if name in cache:
        return cache[name]
    c = bpy.data.collections.new(name)
    scene.collection.children.link(c)
    cache[name] = c
    return c


# =====================================================================================
#  步骤 1：从源工程追加资源
# =====================================================================================

def _is_plain_outline_material(m):
    """判断是不是源工程那个"纯黑自发光"的 Outline 材质"""
    if not (m and m.use_nodes):
        return False
    kinds = {n.bl_idname for n in m.node_tree.nodes}
    if not kinds.issubset({"ShaderNodeOutputMaterial", "ShaderNodeEmission"}):
        return False
    for n in m.node_tree.nodes:
        if n.bl_idname == "ShaderNodeEmission":
            try:
                c = n.inputs["Color"].default_value
                if c[0] < 0.01 and c[1] < 0.01 and c[2] < 0.01:
                    return True
            except Exception:
                pass
    return False


def append_from_source():
    """从源 .blend 原样搬运节点组与描边材质，保证风格 100% 一致"""
    result = {"ok": False, "groups": [], "materials": [], "error": None, "cleaned": 0}
    if not SOURCE_BLEND or not os.path.isfile(SOURCE_BLEND):
        result["error"] = f"源工程不存在：{SOURCE_BLEND}"
        return result

    before_o = set(bpy.data.objects)
    before_m = set(bpy.data.materials)
    before_g = set(bpy.data.node_groups)
    before_s = set(bpy.data.scenes)
    before_w = set(bpy.data.worlds)
    before_c = set(bpy.data.collections)

    try:
        with bpy.data.libraries.load(SOURCE_BLEND, link=False) as (src, dst):
            dst.node_groups = [n for n in APPEND_NODE_GROUPS if n in src.node_groups]
            dst.materials = [n for n in APPEND_MATERIALS if n in src.materials]
    except Exception as e:
        result["error"] = str(e)
        return result

    new_g = [g for g in bpy.data.node_groups if g not in before_g]
    new_m = [m for m in bpy.data.materials if m not in before_m]
    new_o = [o for o in bpy.data.objects if o not in before_o]
    new_s = [s for s in bpy.data.scenes if s not in before_s]
    new_w = [w for w in bpy.data.worlds if w not in before_w]
    new_c = [c for c in bpy.data.collections if c not in before_c]

    # 追加合成器节点组会把源工程的整套数据连带拖进来（1000+ 对象、场景、集合…），全部清掉
    for o in new_o:
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass
    for c in new_c:
        try:
            bpy.data.collections.remove(c)
        except Exception:
            pass
    for s in new_s:
        try:
            bpy.data.scenes.remove(s)
        except Exception:
            pass
    for w in new_w:
        try:
            bpy.data.worlds.remove(w)
        except Exception:
            pass
    # 源合成器树本身不用（我们只借它的 Tonemapping 依赖），删掉保持文件干净
    comp_tree = next((g for g in new_g if g.name == "Compositor" and
                      g.bl_idname == "CompositorNodeTree"), None)
    if comp_tree is not None:
        try:
            bpy.data.node_groups.remove(comp_tree)
            new_g = [g for g in new_g if g is not comp_tree]
        except Exception:
            pass
    result["cleaned"] = len(new_o) + len(new_c) + len(new_s) + len(new_w)

    # 材质只保留源工程那个纯黑描边材质，其余（连带拖进来的几十个）删掉
    kept = []
    for m in new_m:
        if _is_plain_outline_material(m):
            kept.append(m)
        else:
            try:
                bpy.data.materials.remove(m)
            except Exception:
                pass
    if kept:
        try:
            kept[0].name = "AI_Outline"      # 避开目标模型自带的同名 Outline(PMM 材质)
            kept[0].use_fake_user = True
        except Exception:
            pass
        for extra in kept[1:]:
            try:
                bpy.data.materials.remove(extra)
            except Exception:
                pass
        kept = [kept[0]]

    # 兜底：如果源工程没给出描边材质，就现场造一个一模一样的
    if not kept:
        om = bpy.data.materials.get("AI_Outline")
        if om is None:
            om = bpy.data.materials.new("AI_Outline")
            om.use_nodes = True
            nt = om.node_tree
            nt.nodes.clear()
            o_ = nt.nodes.new("ShaderNodeOutputMaterial")
            e_ = nt.nodes.new("ShaderNodeEmission")
            e_.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
            e_.inputs["Strength"].default_value = 1.0
            nt.links.new(e_.outputs["Emission"], o_.inputs["Surface"])
        kept = [om]

    result["groups"] = [g.name for g in new_g]
    result["materials"] = [m.name for m in kept]
    result["ok"] = True
    return result


# =====================================================================================
#  步骤 2：内置回退 —— 现场生成"Shader to RGB 二分"节点组
# =====================================================================================

def build_cel_group(name, positions, colors, emit_strength):
    g = bpy.data.node_groups.get(name)
    if g:
        return g
    g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    try:
        g.use_fake_user = True
    except Exception:
        pass

    s = g.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
    s.default_value = (1.0, 1.0, 1.0, 1.0)
    g.interface.new_socket(name="Emission", in_out="OUTPUT", socket_type="NodeSocketShader")

    nt = g
    gi = nt.nodes.new("NodeGroupInput");  gi.location = (-800, 0)
    go = nt.nodes.new("NodeGroupOutput"); go.location = (500, 0)

    diff = nt.nodes.new("ShaderNodeBsdfDiffuse"); diff.location = (-600, -150)
    diff.inputs["Color"].default_value = (0.8, 0.8, 0.8, 1.0)
    diff.inputs["Roughness"].default_value = 0.0

    s2rgb = nt.nodes.new("ShaderNodeShaderToRGB");     s2rgb.location = (-400, -150)
    sep = nt.nodes.new("ShaderNodeSeparateColor");     sep.location = (-200, -150)
    ramp = nt.nodes.new("ShaderNodeValToRGB");         ramp.location = (0, -150)
    comb = nt.nodes.new("ShaderNodeCombineColor");     comb.location = (200, -150)
    mix = nt.nodes.new("ShaderNodeMix");               mix.location = (200, 60)
    emis = nt.nodes.new("ShaderNodeEmission");         emis.location = (350, -150)

    ramp.color_ramp.interpolation = "CONSTANT"
    cr = ramp.color_ramp
    while len(cr.elements) > 1:
        cr.elements.remove(cr.elements[-1])
    cr.elements[0].position = positions[0]
    cr.elements[0].color = (*colors[0], 1.0)
    for p, c in zip(positions[1:], colors[1:]):
        e = cr.elements.new(p)
        e.color = (*c, 1.0)

    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    fac, ma, mb, mres = mix_rgba(mix)
    fac.default_value = 1.0
    emis.inputs["Strength"].default_value = emit_strength

    L = nt.links.new
    L(diff.outputs["BSDF"], s2rgb.inputs["Shader"])
    L(s2rgb.outputs["Color"], sep.inputs["Color"])
    L(sep.outputs["Blue"], ramp.inputs["Fac"])
    L(sep.outputs["Red"], comb.inputs["Red"])
    L(sep.outputs["Green"], comb.inputs["Green"])
    L(ramp.outputs["Color"], comb.inputs["Blue"])
    L(ramp.outputs["Color"], ma)
    L(gi.outputs["Color"], mb)
    L(mres, emis.inputs["Color"])
    L(emis.outputs["Emission"], go.inputs["Emission"])
    return g


def build_fallback_groups(needed):
    made = []
    for n in needed:
        if n in bpy.data.node_groups or n not in CEL_FALLBACK_PRESETS:
            continue
        pos, cols, st = CEL_FALLBACK_PRESETS[n]
        build_cel_group(n, pos, cols, st)
        made.append(n)
    return made


def build_fallback_outline_group(name):
    g = bpy.data.node_groups.get(name)
    if g:
        return g
    g = bpy.data.node_groups.new(name, "GeometryNodeTree")
    try:
        g.use_fake_user = True
    except Exception:
        pass
    g.interface.new_socket(name="几何数据", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    g.interface.new_socket(name="几何数据", in_out="INPUT", socket_type="NodeSocketGeometry")
    g.interface.new_socket(name="描边的宽度(毫米)", in_out="INPUT", socket_type="NodeSocketFloat")
    # ★ 用「材质索引」而不是「材质」：
    #   多材质网格上 Set Material + Join Geometry 会触发材质槽重映射，
    #   会把整个模型的面都指到描边材质上（表现就是整片死黑）。
    #   改成先在对象上挂好描边材质槽，再让描边壳指向该槽索引，就不会错位。
    g.interface.new_socket(name="描边材质索引", in_out="INPUT", socket_type="NodeSocketInt")

    nt = g
    gi = nt.nodes.new("NodeGroupInput"); gi.location = (-600, 0)
    go = nt.nodes.new("NodeGroupOutput"); go.location = (700, 0)
    setpos = nt.nodes.new("GeometryNodeSetPosition");         setpos.location = (-420, 80)
    rmnrm = nt.nodes.new("GeometryNodeRemoveAttribute");      rmnrm.location = (-260, 80)
    flip = nt.nodes.new("GeometryNodeFlipFaces");             flip.location = (-100, 80)
    setidx = nt.nodes.new("GeometryNodeSetMaterialIndex");    setidx.location = (100, 80)
    flat = nt.nodes.new("GeometryNodeSetShadeSmooth");        flat.location = (250, 160)
    toinst = nt.nodes.new("GeometryNodeGeometryToInstance");  toinst.location = (400, 80)
    join = nt.nodes.new("GeometryNodeJoinGeometry");          join.location = (560, 0)
    nrm = nt.nodes.new("GeometryNodeInputNormal");            nrm.location = (-420, -200)
    vscale = nt.nodes.new("ShaderNodeVectorMath");            vscale.location = (-160, -200)
    vscale.operation = "SCALE"
    mm2m = nt.nodes.new("ShaderNodeMath");                    mm2m.location = (-420, -320)
    mm2m.operation = "DIVIDE"
    mm2m.inputs[1].default_value = 1000.0

    try:
        rmnrm.inputs["Name"].default_value = "custom_normal"
    except Exception:
        pass

    for nd in (setpos, flip, setidx):
        sel = sock(nd, "Selection")
        if sel:
            sel.default_value = True

    try:
        sock(flat, "Shade Smooth").default_value = False   # 强制平面着色
    except Exception:
        pass

    L = nt.links.new
    L(gi.outputs["几何数据"], join.inputs["Geometry"])
    L(gi.outputs["几何数据"], setpos.inputs["Geometry"])
    L(setpos.outputs["Geometry"], rmnrm.inputs["Geometry"])
    L(rmnrm.outputs["Geometry"], flip.inputs["Mesh"])
    L(flip.outputs["Mesh"], setidx.inputs["Geometry"])
    L(gi.outputs["描边材质索引"], setidx.inputs["Material Index"])
    L(setidx.outputs["Geometry"], flat.inputs["Geometry"])
    L(flat.outputs["Geometry"], toinst.inputs["Geometry"])
    L(toinst.outputs["Instances"], join.inputs["Geometry"])
    L(join.outputs["Geometry"], go.inputs["几何数据"])
    L(gi.outputs["描边的宽度(毫米)"], mm2m.inputs[0])
    L(mm2m.outputs["Value"], vscale.inputs["Scale"])
    L(nrm.outputs["Normal"], vscale.inputs["Vector"])
    L(vscale.outputs["Vector"], setpos.inputs["Offset"])
    return g


# =====================================================================================
#  步骤 3：渲染环境
# =====================================================================================

def setup_render(scene):
    r = scene.render
    try:
        r.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        r.engine = "BLENDER_EEVEE"

    if RESOLUTION:
        r.resolution_x, r.resolution_y = RESOLUTION
    else:
        r.resolution_x, r.resolution_y = 1080, 1980
    r.resolution_percentage = 100
    r.film_transparent = FILM_TRANSPARENT
    r.use_motion_blur = False

    ee = scene.eevee
    for k, v in {
        "taa_render_samples": 16, "use_raytracing": True, "use_shadows": True,
        "shadow_ray_count": 1, "shadow_step_count": 6, "use_fast_gi": True,
        "fast_gi_method": "AMBIENT_OCCLUSION_ONLY", "fast_gi_distance": 0.1,
        "fast_gi_quality": 0.25, "fast_gi_ray_count": 2, "fast_gi_step_count": 8,
        "direct_light_intensity": 1.0, "indirect_light_intensity": 1.0,
        "use_volumetric_shadows": False, "volumetric_samples": 64,
    }.items():
        if hasattr(ee, k):
            try:
                setattr(ee, k, v)
            except Exception:
                pass

    vs = scene.view_settings
    for attr, val in (("view_transform", VIEW_TRANSFORM), ("look", LOOK),
                      ("exposure", 0.0), ("gamma", 1.0)):
        try:
            setattr(vs, attr, val)
        except Exception:
            log(f"  ! 色彩管理 {attr}={val} 设置失败")


def setup_world(scene):
    w = scene.world
    if w is None:
        w = bpy.data.worlds.new("Toon_Lavender_World")
        scene.world = w
    w.use_nodes = True
    try:
        w.color = (0.0, 0.0, 0.0)
    except Exception:
        pass
    nt = w.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    out.location, bg.location, env.location = (300, 0), (0, 0), (-300, 0)
    bg.inputs["Strength"].default_value = WORLD_STRENGTH
    # 源工程里背景色就是纯黑 (0,0,0)；实际照明由下面的环境纹理提供（颜色接口被链接覆盖）
    try:
        bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    except Exception:
        pass

    loaded = False
    for base in (bpy.utils.system_resource("DATAFILES") if hasattr(bpy.utils, "system_resource") else "",
                 bpy.utils.resource_path("LOCAL")):
        cand = os.path.join(base, "datafiles", "studiolights", "world", HDRI_NAME)
        if os.path.isfile(cand):
            try:
                img = bpy.data.images.load(cand)
                env.image = img
                img.colorspace_settings.name = "Linear Rec.709"
                loaded = True
                break
            except Exception:
                pass
    L = nt.links.new
    if loaded:
        L(env.outputs["Color"], bg.inputs["Color"])
    else:
        bg.inputs["Color"].default_value = (0.05, 0.04, 0.08, 1.0)
        log(f"  ! 未找到 {HDRI_NAME}，世界改用低强度冷灰")
    L(bg.outputs["Background"], out.inputs["Surface"])


def setup_view_layer_props(scene):
    """Autocel 的光照同步系统：视图层自定义属性，供 Ambient Sync 等节点组读取"""
    try:
        vl = bpy.context.view_layer
    except Exception:
        return
    defaults = {
        "LightVector": (1.3077203035354614, -3.8551149827981135e-08, -0.6973908543586731),
        "LightColor": (1.0, 1.0, 1.0),
        "LightEnergy": 1.0,
        "AmbientColor": (0.2, 0.2, 0.2),
        "AmbientStrength": 1.0,
    }
    for k, v in defaults.items():
        try:
            if k not in vl:
                vl[k] = v
        except Exception:
            pass


def _apply_glow_settings(gl):
    for name, val in (("Type", "Bloom"), ("Quality", "Medium"),
                      ("Threshold", GLOW["Threshold"]), ("Smoothness", GLOW["Smoothness"]),
                      ("Strength", GLOW["Strength"]), ("Size", GLOW["Size"]),
                      ("Maximum", GLOW["Maximum"]), ("Clamp", False)):
        s = sock(gl, name)
        if s:
            try:
                s.default_value = val
            except Exception:
                pass
    for attr, val in (("glare_type", "BLOOM"), ("quality", "MEDIUM"),
                      ("threshold", GLOW["Threshold"]), ("size", GLOW["Size"]),
                      ("strength", GLOW["Strength"]), ("smoothness", GLOW["Smoothness"]),
                      ("maximum", GLOW["Maximum"])):
        if hasattr(gl, attr):
            try:
                setattr(gl, attr, val)
            except Exception:
                pass


def _simplify_source_compositor(tree, scene):
    """
    源工程的合成器里有一组 Kafka 手臂 Cryptomatte 遮罩支路，在别的模型上恒为空转。
    这里把它拆掉，改成 渲染层 → Glare → Tonemapping → 输出 的干净链路，
    并把「渲染层」节点重新指向当前场景（否则追加过来的那个 RLayer 会指向源场景，出白图）。
    """
    rl = next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)
    gl = next((n for n in tree.nodes if n.bl_idname == "CompositorNodeGlare"), None)
    if rl is None or gl is None:
        return False

    # ★ 关键：把渲染层指向当前场景 / 当前视图层
    try:
        rl.scene = scene
    except Exception:
        pass
    try:
        vl = scene.view_layers[0].name if scene.view_layers else "ViewLayer"
        rl.layer = vl
    except Exception:
        pass

    # 找 Tonemapping 组节点
    tm = None
    for n in tree.nodes:
        if n.bl_idname == "CompositorNodeGroup" and n.node_tree and \
                n.node_tree.bl_idname == "CompositorNodeTree":
            tm = n
            break

    # 找输出节点
    out = None
    for n in tree.nodes:
        if n.bl_idname in ("NodeGroupOutput", "CompositorNodeComposite"):
            out = n
            break

    # 删除遮罩支路
    for n in list(tree.nodes):
        if n.bl_idname in ("CompositorNodeCryptomatteV2", "ShaderNodeMix") or \
                n.name.startswith("Kafka_"):
            try:
                tree.nodes.remove(n)
            except Exception:
                pass

    # 重新接线
    try:
        for l in list(rl.outputs["Image"].links):
            tree.links.remove(l)
        for l in list(gl.outputs["Image"].links):
            tree.links.remove(l)
    except Exception:
        pass
    tree.links.new(rl.outputs["Image"], gl.inputs["Image"])
    tail = gl
    if tm is not None:
        tree.links.new(gl.outputs["Image"], tm.inputs["Image"])
        tail = tm
    if out is not None:
        tree.links.new(tail.outputs["Image"], out.inputs["Image"])
    return True


def setup_compositor(scene):
    """
    自己搭合成器链路：渲染层 → Glare 辉光 → Tonemapping(源工程组) → 输出。
    只复用源工程的 Tonemapping 节点组（随 Compositor 依赖追加进来），
    节点全部自建，保证「渲染层」一定指向当前场景。
    """
    is_5x = not hasattr(scene, "node_tree")
    if is_5x:
        tree = getattr(scene, "compositing_node_group", None)
        # 如果当前绑定的是追加进来的源合成器，换成我们自己的（避免破坏源数据）
        if tree is not None and tree.name == "Compositor":
            tree = None
        if tree is None:
            tree = bpy.data.node_groups.get("AI_Compositor")
            if tree is None:
                tree = bpy.data.node_groups.new("AI_Compositor", "CompositorNodeTree")
            try:
                scene.compositing_node_group = tree
            except Exception as e:
                log(f"  ! 无法创建合成节点组：{e}")
                return
        try:
            has_img = any(getattr(i, "in_out", None) == "OUTPUT" for i in tree.interface.items_tree)
            if not has_img:
                tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        except Exception:
            pass
    else:
        try:
            scene.use_nodes = True
        except Exception:
            pass
        tree = getattr(scene, "node_tree", None)
        if tree is None:
            log("  ! 取不到合成节点树，跳过辉光")
            return

    tree.nodes.clear()
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.location = (-400, 0)
    try:
        rl.scene = scene
    except Exception:
        pass
    try:
        rl.layer = scene.view_layers[0].name if scene.view_layers else "ViewLayer"
    except Exception:
        pass

    gl = None
    if PIPELINE_MODE == "enhanced":
        gl = tree.nodes.new("CompositorNodeGlare"); gl.location = (-120, 0)
    try:
        out = tree.nodes.new("NodeGroupOutput" if is_5x else "CompositorNodeComposite")
    except Exception as e:
        log(f"  ! 合成输出节点创建失败：{e}")
        return
    out.location = (400, 0)

    L = tree.links.new

    # ---- 忠实复刻模式：源工程的实际有效路径就是「渲染层直出」（Tonemap/Bloom 被旁路）
    if PIPELINE_MODE == "faithful":
        L(rl.outputs["Image"], out.inputs["Image"])
        RUN_INFO["compositor_effective"] = "RenderLayers -> Output (源工程旁路等价)"
        log("      合成器【忠实复刻】：渲染层 → 输出"
            "（源工程 Kafka_Arm_Bypass_Final 已把 Tonemap/Bloom 旁路，见 docs/根因与口径.md）")
        return

    # ---- 增强辉光模式：渲染层 → Glare → Tonemapping → 输出（改良版，不声称与源工程一致）
    _apply_glow_settings(gl)
    L(rl.outputs["Image"], gl.inputs["Image"])

    tm = None
    tmpl = bpy.data.node_groups.get("Tonemapping")
    if tmpl is not None and tmpl.bl_idname == "CompositorNodeTree":
        try:
            tm = tree.nodes.new("CompositorNodeGroup")
            tm.location = (140, 0)
            tm.node_tree = tmpl
            for nm, v in (("映射模式", "NAESTonemap"), ("颜色溢出保护", False)):
                s = sock(tm, nm)
                if s:
                    s.default_value = v
        except Exception as e:
            log(f"      ! Tonemapping 接入失败：{e}")
            tm = None

    if tm is not None:
        L(gl.outputs["Image"], tm.inputs["Image"])
        L(tm.outputs["Image"], out.inputs["Image"])
        RUN_INFO["compositor_effective"] = "RenderLayers -> Glare -> Tonemapping -> Output"
        log("      合成器【增强辉光·改良版】：渲染层 → Glare 辉光 → Tonemapping(源工程) → 输出")
    else:
        L(gl.outputs["Image"], out.inputs["Image"])
        RUN_INFO["compositor_effective"] = "RenderLayers -> Glare -> Output"
        log("      ! 没有 Tonemapping 节点组，画面对比度会偏暗")


# =====================================================================================
#  步骤 4：材质装配
# =====================================================================================

def pick_group(mat_name):
    """
    返回 (节点组名, 命中的规则正则)。未命中任何规则时返回 (None, None)。
    ★ 审计整改项 6：以前这里直接 `return FALLBACK_GROUP`，会把不认识的中/日/英文
      材质名悄悄全塞进 Cel_Dark，验收时看不出任何异常。现在必须显式暴露未命中。
    """
    for pattern, group in MATERIAL_RULES:
        if re.search(pattern, mat_name, re.IGNORECASE):
            if group == "Cel_Hair" and USE_HAIR_REFERENCE_GROUP \
                    and any(re.search(p, mat_name, re.IGNORECASE) for p in HEAD_HAIR_PATTERNS) \
                    and bpy.data.node_groups.get("Sakura_Hair_Reference"):
                return "Sakura_Hair_Reference", pattern
            return group, pattern
    return None, None


def should_skip(mat_name):
    for p in SKIP_MATERIAL_PATTERNS:
        if re.search(p, mat_name, re.IGNORECASE):
            return True
    return False


def find_base_image(mat):
    """
    取原材质的基准贴图，并记下它用的是哪个 UV 图层。
    返回 (image, uv_map_name_or_None)
    —— MMD 模型常常有多个 UV 图层，裸贴图节点会走"活动 UV"，采错就会一片黑。
    """
    if not (mat and mat.use_nodes):
        return None, None
    cands = []
    for n in mat.node_tree.nodes:
        if n.bl_idname != "ShaderNodeTexImage" or n.image is None:
            continue
        try:
            w, h = n.image.size
            area = int(w) * int(h)
        except Exception:
            area = 0
        linked = any(o.is_linked for o in n.outputs)
        uv = None
        try:
            vin = n.inputs["Vector"]
            if vin.is_linked:
                src = vin.links[0].from_node
                if src.bl_idname == "ShaderNodeUVMap":
                    uv = src.uv_map
                elif src.bl_idname == "ShaderNodeNormalMap" or src.bl_idname == "ShaderNodeMapping":
                    uv = None
        except Exception:
            pass
        cands.append((linked, area, n.image, uv))
    if not cands:
        return None, None
    linked_only = [c for c in cands if c[0]]
    pool = linked_only or cands
    pool.sort(key=lambda c: c[1], reverse=True)
    return pool[0][2], pool[0][3]


def detect_transparent(mat):
    """
    判断原材质本身是不是"半透明材质"。
    ★ 必须用 MMD 材质自己的 alpha 属性来判断，不能看"原节点树里贴图 Alpha 有没有接出去"：
      很多 MMD 贴图（尤其是 TGA/PNG 的皮肤、衣服）整张的 Alpha 通道是 0，
      MMD 对不透明材质根本不使用它。一旦把贴图 Alpha 接进混合，整个模型会变透明 ——
      透明 PNG 里显示成"白色"，换成有背景的图就直接"整片消失/发黑"。
    """
    mm = getattr(mat, "mmd_material", None)
    if mm is not None:
        try:
            return float(mm.alpha) < 0.999
        except Exception:
            pass
    # 没有 mmd_tools 数据时，退回看原材质是不是用了"透明/混合"渲染方式
    try:
        if getattr(mat, "blend_method", "OPAQUE") not in ("OPAQUE", None):
            return True
    except Exception:
        pass
    return False


def rebuild_material(mat, group, transparent=False, orig_name=None):
    """把材质重建成：图像纹理 → Cel 组 → Alpha 混合 → 输出（保留原贴图与 UV 映射）"""
    image, uv_name = find_base_image(mat)
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (520, 0)
    grp = nt.nodes.new("ShaderNodeGroup");          grp.location = (60, 0)
    grp.node_tree = group
    trans = nt.nodes.new("ShaderNodeBsdfTransparent"); trans.location = (60, -320)
    amix = nt.nodes.new("ShaderNodeMixShader");     amix.location = (320, -120)

    base = sock(grp, "Color")
    if image is not None:
        tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-380, 0)
        tex.image = image
        # ★ 对齐源工程的贴图色彩空间，否则整体会偏暗。
        #   原始色彩空间取运行开始时的快照，改完一并记进台账（审计整改项 4）。
        orig_cs = _IMG_CS_SNAPSHOT.get(image.name)
        new_cs = orig_cs
        if TEXTURE_COLORSPACE:
            try:
                image.colorspace_settings.name = TEXTURE_COLORSPACE
                new_cs = TEXTURE_COLORSPACE
            except Exception as e:
                log(f"  ! 贴图 {image.name} 色彩空间改 {TEXTURE_COLORSPACE} 失败：{e}")
        try:
            w, h = image.size
        except Exception:
            w, h = 0, 0
        TEXTURE_LEDGER.append({
            "material": orig_name or mat.name,
            "image": image.name,
            "role_hint": "base",
            "orig_colorspace": orig_cs,
            "new_colorspace": new_cs,
            "used_as_base": True,
            "resolution": [w, h],
            "uv_map": uv_name,
            "linked_at_scan": True,
        })
        if uv_name:
            uvn = nt.nodes.new("ShaderNodeUVMap"); uvn.location = (-600, -100)
            uvn.uv_map = uv_name
            nt.links.new(uvn.outputs["UV"], tex.inputs["Vector"])
        nt.links.new(tex.outputs["Color"], base)
        if transparent:
            nt.links.new(tex.outputs["Alpha"], amix.inputs[0])
        else:
            amix.inputs[0].default_value = 0.0     # 不透明材质：不接贴图 Alpha
    else:
        if base:
            base.default_value = (0.9, 0.9, 0.9, 1.0)
        amix.inputs[0].default_value = 0.0

    emis_out = None
    for s in grp.outputs:
        if s.type == "SHADER":
            emis_out = s
            break
    if emis_out is None and grp.outputs:
        emis_out = grp.outputs[0]

    L = nt.links.new
    L(emis_out, amix.inputs[1])
    L(trans.outputs["BSDF"], amix.inputs[2])
    L(amix.outputs["Shader"], out.inputs["Surface"])

    for attr, val in (("blend_method", "HASHED"), ("surface_render_method", "DITHERED"),
                      ("use_transparent_shadow", True), ("alpha_threshold", 0.5),
                      # ★ mmd_tools 会给单面材质打开背面剔除；换到新模型上如果网格是
                      #   双面/缠绕方向不一致的，剔除会让正面消失，只剩背光面 —— 一片黑。
                      #   统一关掉，保证新模型一定能看到正面。
                      ("use_backface_culling", False)):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    return image is not None


# =====================================================================================
#  步骤 5：描边
# =====================================================================================

def get_outline_group():
    if OUTLINE_IMPL == "source":
        for n in OUTLINE_GROUP_CANDIDATES:
            g = bpy.data.node_groups.get(n)
            if g and g.bl_idname == "GeometryNodeTree":
                return g
    return build_fallback_outline_group("AI_Outline_GN")


def add_outline(obj, group, material, width_mm):
    if material:
        try:
            material.use_fake_user = True
        except Exception:
            pass

    # 先把描边材质挂到对象材质槽，拿到它的索引供几何节点使用
    slot_index = None
    try:
        for i, m in enumerate(obj.data.materials):
            if m is material:
                slot_index = i
                break
        if slot_index is None:
            obj.data.materials.append(material)
            slot_index = len(obj.data.materials) - 1
    except Exception:
        slot_index = None

    md = obj.modifiers.new(name=group.name, type="NODES")
    md.node_group = group

    ids = {}
    try:
        for it in group.interface.items_tree:
            if getattr(it, "in_out", None) == "INPUT":
                ids.setdefault(it.name, it.identifier)
    except Exception:
        pass

    def setv(socket_name, value):
        ident = ids.get(socket_name)
        if ident is None:
            return False
        try:
            md.properties.inputs[ident]["value"] = value
            return True
        except Exception:
            return False

    setv("描边的宽度(毫米)", width_mm)
    if slot_index is not None:
        setv("描边材质索引", slot_index)
    # 源工程「通用描边3.1」的接口名不同，一并兼容
    setv("描边的材质", material)
    setv("顶点组名称", "描边权重")
    setv("设置比例", 0.3)
    return md


# =====================================================================================
#  步骤 6：灯光 / 相机
# =====================================================================================

def setup_lights(scene):
    cache = {}
    made = []
    reused = 0
    for spec in (LIGHTS_ACTIVE, LIGHTS_SPARE):
        for name, ltype, energy, color, loc, rot, coll_name, extra in spec:
            ex = bpy.data.objects.get(name)
            if ex is not None and ex.type != "LIGHT":
                ex = None                      # 同名但类型不符，另建
            if ex is not None:
                reused += 1
                o = ex
                d = o.data
                if d is None or d.type != ltype:
                    # 场景自带同名灯但类型不符（如 .blend 里的 POINT 撞上 SUN 名）→ 换灯数据
                    d = bpy.data.lights.new(name, ltype)
                    o.data = d
            else:
                d = bpy.data.lights.new(name, ltype)
                o = bpy.data.objects.new(name, d)
            # ★ 无论新建还是复用，都按规格覆盖。
            #   复用分支以前只 link 一下就 continue，导致「场景里自带的同名灯保持原样」——
            #   例如直接用 --blend 打开的场景里有个 hide_render=False 的 `Light`，
            #   它会一直参与渲染，使有效灯数量变成 22 而源工程是 21，破坏可复现性。
            d.energy = energy
            d.color = color
            d.use_shadow = True
            if ltype == "AREA" and "area" in extra:
                d.size, d.size_y = extra["area"]
                d.spread = math.pi
            if ltype == "SPOT":
                if "spot" in extra:
                    d.spot_size, d.spot_blend = extra["spot"]
                if "soft" in extra:
                    d.shadow_soft_size = extra["soft"]
            if ltype in ("SUN", "POINT") and "soft" in extra:
                d.shadow_soft_size = extra["soft"]
            if ltype == "SUN" and "angle" in extra:
                d.angle = extra["angle"]
            for key, attr in (("specular", "specular_factor"), ("diffuse", "diffuse_factor"),
                              ("volume", "volume_factor")):
                if key in extra and hasattr(d, attr):
                    setattr(d, attr, extra[key])
            o.rotation_mode = "XYZ"
            o.location = loc
            o.rotation_euler = rot
            o.scale = extra.get("scale", (1.0, 1.0, 1.0))
            o.hide_render = bool(extra.get("hide", False))
            target_coll = ensure_collection(scene, coll_name, cache)
            for c in list(o.users_collection):
                if c is not target_coll:
                    try:
                        c.objects.unlink(o)
                    except Exception:
                        pass
            if o.name not in target_coll.objects:
                target_coll.objects.link(o)
            made.append(o)
    setup_lights.reused = reused
    return made


def world_bbox(objs):
    """合并计算一组对象的世界空间包围盒 → (中心, 尺寸)"""
    from mathutils import Vector
    mn = Vector((1e9, 1e9, 1e9))
    mx = Vector((-1e9, -1e9, -1e9))
    found = False
    for o in objs:
        if o.type != "MESH" or not o.data.vertices:
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
            found = True
    if not found:
        return None, None
    return (mn + mx) * 0.5, (mx - mn)


def setup_camera(scene, targets=None):
    cam = bpy.data.objects.get(CAMERA["name"])
    if cam is None:
        cd = bpy.data.cameras.new(CAMERA["name"])
        cam = bpy.data.objects.new(CAMERA["name"], cd)
        scene.collection.objects.link(cam)
    cd = cam.data
    cd.lens = CAMERA["lens"]
    cd.sensor_width = CAMERA["sensor"]
    cd.clip_start, cd.clip_end = CAMERA["clip"]

    if AUTO_FRAME and targets:
        center, size = world_bbox(targets)
        if center is not None:
            res_x, res_y = scene.render.resolution_x, scene.render.resolution_y
            sensor_fit = max(size.x, size.y * res_x / max(res_y, 1))
            fov = 2.0 * math.atan(CAMERA["sensor"] / (2.0 * CAMERA["lens"]))
            dist = (sensor_fit * AUTO_FRAME_MARGIN / 2.0) / math.tan(fov / 2.0) * 2.0
            dist = max(dist, 0.3)
            cd.shift_x = 0.0
            cd.shift_y = 0.0
            cam.location = (center.x, center.y - dist, center.z)
            cam.rotation_euler = (math.pi / 2.0, 0.0, 0.0)   # 正面平视，看向 +Y
            scene.camera = cam
            log(f"      自动取景：模型包围盒 {tuple(round(v,3) for v in size)} · 机位距离 {dist:.2f}m")
            return cam

    cd.shift_x = CAMERA["shift_x"]
    cd.shift_y = CAMERA["shift_y"]
    cam.location = CAMERA["loc"]
    cam.rotation_euler = CAMERA["rot"]
    scene.camera = cam
    return cam


# =====================================================================================
#  步骤 7：主流程
# =====================================================================================

def gather_targets():
    if TARGET_MODE == "SELECTED":
        objs = [o for o in bpy.context.selected_objects if o.type == "MESH"]
        if not objs:
            objs = [o for o in bpy.context.view_layer.objects if o.type == "MESH" and not o.hide_get()]
    else:
        objs = [o for o in bpy.context.view_layer.objects
                if o.type == "MESH" and o.visible_get()]
    return [o for o in objs if o.data and len(o.data.materials)]


def main():
    scene = bpy.context.scene
    RUN_INFO.clear()
    TEXTURE_LEDGER.clear()
    UNMATCHED_MATERIALS.clear()
    SKIPPED_MATERIALS.clear()
    log("=" * 72)
    log(" 一键卡通渲染 —— 开始")
    log("=" * 72)
    log(f" 合成路径模式 PIPELINE_MODE = {PIPELINE_MODE}"
        + ("（忠实复刻：源工程实际生效路径）" if PIPELINE_MODE == "faithful"
           else "（增强辉光：改良版，不声称与源工程一致）"))
    log(f" 严格材质匹配 STRICT_MATERIAL_MATCH = {STRICT_MATERIAL_MATCH}")
    log(f" 源工程 = {SOURCE_BLEND}")
    log(f" 输出目录 = {OUT_DIR}")

    # 输出目录必须为空（审计整改项 8）：否则日志里的 outputs 列表会混进历史遗留文件
    os.makedirs(OUT_DIR, exist_ok=True)
    pre_existing = sorted(os.listdir(OUT_DIR))
    if pre_existing:
        log(f" ! 输出目录非空，已有 {len(pre_existing)} 个文件；"
            f"它们会出现在 outputs 清单里，已在 run_manifest.json 的 pre_existing 标出")
    snapshot_image_colorspaces()

    # ---- 1 追加源资源
    res = append_from_source()
    if res["ok"]:
        log(f"[1/9] 从源工程追加：节点组 {len(res['groups'])} 个 / 材质 {len(res['materials'])} 个")
        if res["groups"]:
            log("      " + "、".join(res["groups"]))
    else:
        log(f"[1/9] 源工程追加失败（{res['error']}），改用内置回退方案")

    # ---- 2 补齐缺失的节点组
    needed = sorted({g for _, g in MATERIAL_RULES} | {FALLBACK_GROUP} | set(CEL_FALLBACK_PRESETS))
    made = build_fallback_groups(needed)
    if made:
        log(f"[2/9] 内置回退补建节点组：{'、'.join(made)}")
    else:
        log("[2/9] 所需卡通节点组齐备")

    # ---- 3 渲染环境
    setup_render(scene)
    setup_world(scene)
    setup_view_layer_props(scene)
    log(f"[3/9] 渲染环境：EEVEE + {VIEW_TRANSFORM} / {LOOK} + {HDRI_NAME}({WORLD_STRENGTH})"
        f" + 视图层光照同步属性")

    # ---- 4 合成器
    setup_compositor(scene)
    if PIPELINE_MODE == "faithful":
        log("[4/9] 合成器：忠实复刻（渲染层直出，无辉光 / 无 Tonemapping）")
    else:
        log(f"[4/9] 合成器：增强辉光 · Glare {GLOW['Type']} 阈值{GLOW['Threshold']} "
            f"强度{GLOW['Strength']}（改良版，不声称与源工程一致）")

    # ---- 5 描边资源
    outline_group = get_outline_group()
    outline_mat = bpy.data.materials.get("AI_Outline")
    if outline_mat is None:
        outline_mat = bpy.data.materials.new("AI_Outline")
        outline_mat.use_nodes = True
        ntm = outline_mat.node_tree
        ntm.nodes.clear()
        o = ntm.nodes.new("ShaderNodeOutputMaterial")
        e = ntm.nodes.new("ShaderNodeEmission")
        e.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        e.inputs["Strength"].default_value = 1.0
        ntm.links.new(e.outputs["Emission"], o.inputs["Surface"])
    outline_mat.use_fake_user = True
    try:
        outline_mat.use_backface_culling = True
    except Exception:
        pass
    log(f"[5/9] 描边：节点组「{outline_group.name}」+ 材质「{outline_mat.name}」")

    # ---- 6 材质装配 + 描边
    targets = gather_targets()
    if not targets:
        log("[6/9] ! 没有找到可用网格。请先选中模型，或把 TARGET_MODE 设为 ALL_VISIBLE")
    scan_textures = scan_model_textures(targets)          # 必须在重建前扫描
    stats = {}
    outlined = 0
    skipped = 0
    for obj in targets:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            orig_name = mat.name
            if should_skip(orig_name):
                skipped += 1
                SKIPPED_MATERIALS.append(orig_name)
                continue
            gname, hit_pattern = pick_group(orig_name)
            if gname is None:
                # ★ 审计整改项 6：未命中不再静默落 Cel_Dark
                UNMATCHED_MATERIALS.append(orig_name)
                log(f"      ✗ 未命中规则：材质「{orig_name}」")
                if STRICT_MATERIAL_MATCH:
                    continue
                gname = FALLBACK_GROUP
            grp = bpy.data.node_groups.get(gname)
            if grp is None:
                log(f"      ✗ 节点组「{gname}」不存在（材质「{orig_name}」）")
                if STRICT_MATERIAL_MATCH:
                    UNMATCHED_MATERIALS.append(orig_name)
                    continue
                gname = FALLBACK_GROUP
                grp = bpy.data.node_groups.get(gname)
            if grp is None:
                continue
            is_trans = detect_transparent(mat)      # 必须在重建前判断
            if KEEP_ORIGINAL_MATERIALS and not mat.name.endswith(".orig"):
                try:
                    mat.name = mat.name + ".orig"
                except Exception:
                    pass
            rebuild_material(mat, grp, transparent=is_trans, orig_name=orig_name)
            stats[gname] = stats.get(gname, 0) + 1
        if ENABLE_OUTLINE and not any(
                m.type == "NODES" and m.node_group and m.node_group.name == outline_group.name
                for m in obj.modifiers):
            add_outline(obj, outline_group, outline_mat, OUTLINE_WIDTH_MM)
            outlined += 1
    log(f"[6/9] 材质装配：处理 {len(targets)} 个网格 / {sum(stats.values())} 个材质槽"
        + (f"（跳过 {skipped} 个指示器类材质）" if skipped else ""))
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        log(f"      {k:<22} × {v}")
    if SKIPPED_MATERIALS:
        log(f"      跳过（指示器/描边，按设计不出图）：{'、'.join(SKIPPED_MATERIALS)}")
    if UNMATCHED_MATERIALS:
        log(f"[6/9] !!! ERROR 未匹配材质 {len(UNMATCHED_MATERIALS)} 个："
            f"{'、'.join(UNMATCHED_MATERIALS)}")
        if STRICT_MATERIAL_MATCH:
            log("      严格模式下不静默兜底。请为这些材质名补一条 MATERIAL_RULES 规则，"
                "或显式把 STRICT_MATERIAL_MATCH 设为 False 接受 Cel_Dark 兜底。")
            raise RuntimeError(
                f"未匹配材质 {len(UNMATCHED_MATERIALS)} 个：{UNMATCHED_MATERIALS}")
        log("      已按 STRICT_MATERIAL_MATCH=False 兜底到 " + FALLBACK_GROUP)
    log(f"[6/9] 描边：{'新挂 ' + str(outlined) + ' 个网格（线宽 ' + str(OUTLINE_WIDTH_MM) + 'mm）' if ENABLE_OUTLINE else '已跳过（ENABLE_OUTLINE=False）'}")

    # ---- 7 灯光
    lights = setup_lights(scene)
    log(f"[7/9] 灯光：共 {len(lights)} 盏（新建 {len(lights) - getattr(setup_lights, 'reused', 0)} / "
        f"复用 {getattr(setup_lights, 'reused', 0)}），"
        f"其中 {sum(1 for l in lights if not l.hide_render)} 盏参与渲染")

    # ---- 8 相机
    setup_camera(scene, targets)
    log(f"[8/9] 相机：{CAMERA['name']} · {CAMERA['lens']}mm"
        + ("（自动取景）" if AUTO_FRAME else "（原工程构图）"))

    # ---- 9 保存 + 渲染
    os.makedirs(OUT_DIR, exist_ok=True)
    blend_out = os.path.join(OUT_DIR, "一键卡通渲染_成品.blend")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=blend_out)
        log(f"[9/9] 已保存：{blend_out}")
    except Exception as e:
        log(f"[9/9] ! 保存失败：{e}")

    if DO_RENDER:
        render_outputs(scene)

    # ---- 10 台账与运行清单（审计整改项 4 / 8）
    write_texture_ledger(OUT_DIR, scan_textures)
    write_run_manifest(OUT_DIR, pre_existing, extra_inputs=list(EXTRA_INPUTS))

    log("=" * 72)
    log(" 完成")
    log("=" * 72)
    return LOG


def render_outputs(scene):
    r = scene.render
    old = (r.image_settings.file_format, r.resolution_x, r.resolution_y, r.filepath)
    fmt_ok = True
    try:
        r.image_settings.file_format = "PNG"
    except Exception as e:
        fmt_ok = False
        log(f"      ! 无法把输出格式改为 PNG（{e}）")
    log(f"      渲染上下文场景={bpy.context.scene.name} · 输出格式={r.image_settings.file_format}"
        f" · 分辨率={r.resolution_x}x{r.resolution_y}")

    tree = get_comp_tree(scene)
    if PIPELINE_MODE == "faithful":
        # 忠实复刻模式下合成器里根本没有 Glare 节点，"带辉光/无辉光"按定义完全相同，
        # 只出一张正式图，避免制造两张同内容的假对照。
        log("      忠实复刻模式：合成器无辉光节点，只输出一张正式图")
        variants = (("", False),)
    else:
        variants = (("", False), ("_无辉光", True))
    for tag, mute_glow in variants:
        if tree:
            for n in tree.nodes:
                if n.bl_idname == "CompositorNodeGlare":
                    n.mute = mute_glow
        path = os.path.join(OUT_DIR, f"一键渲染{tag}.png")
        r.filepath = path
        try:
            res = bpy.ops.render.render(write_still=True)
            ok = os.path.exists(path)
            if ok:
                log(f"      渲染完成：{path}")
            else:
                log(f"      ! 渲染未落盘 op={list(res)} filepath={r.filepath} "
                    f"fmt={r.image_settings.file_format}")
        except Exception as e:
            log(f"      ! 渲染失败：{e}")

    if tree:
        for n in tree.nodes:
            if n.bl_idname == "CompositorNodeGlare":
                n.mute = False
    try:
        r.image_settings.file_format, r.resolution_x, r.resolution_y, r.filepath = old
    except Exception:
        pass


if __name__ == "__main__":
    main()
