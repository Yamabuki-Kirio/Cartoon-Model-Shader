# -*- coding: utf-8 -*-
"""
材质预览图生成（Blender 内运行，v3.1）
=====================================================================================
为每个"需要用户确认"的材质渲三张图：

  (a) <模型>__<idx>__overview.png   整体图 + 目标材质高亮（洋红自发光）
  (b) <模型>__<idx>__mask.png       只显示目标材质（其余全透明）
  (c) <模型>__<idx>__proposed.png   按【推荐分类】重建目标材质后的效果

做法：
  1. 用「临时映射」把推荐分类喂给主脚本 —— 这样严格准入直接通过，
     所有材质都是正常重建状态（不用改主脚本，也不用 diagnostic 兜底）
  2. 渲 (c)
  3. 把目标材质换成洋红自发光 → 渲 (a)
  4. 把其余材质换成全透明      → 渲 (b)

用法（经 launch_tool 派发）：
  blender -b --factory-startup --python preview_gen.py -- \
     --pmx <pmx> --confirmation <confirmation.json> --out <previews 目录> \
     --resolve <源工程.blend> --log <log> --done <done>
"""
import bpy
import json
import os
import sys
import time
import traceback
import addon_utils

HERE = os.path.dirname(os.path.abspath(__file__))
V31 = os.path.dirname(HERE)
MAIN_SCRIPT = os.path.join(V31, "一键卡通渲染.py")


def arg(name, default=None):
    a = sys.argv
    if name in a:
        i = a.index(name)
        if i + 1 < len(a):
            return a[i + 1]
    return default


PMX = arg("--pmx")
CONF = arg("--confirmation")
OUT = arg("--out")
SOURCE = arg("--resolve", os.environ.get("TOON_SRC_BLEND", ""))
LOG = arg("--log", "preview_gen.log")
DONE = arg("--done", "preview_gen.done")
RES = int(arg("--res", "720"))


def log(m):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def make_magenta(mat):
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emi = nt.nodes.new("ShaderNodeEmission")
    emi.inputs["Color"].default_value = (1.0, 0.0, 1.0, 1.0)
    emi.inputs["Strength"].default_value = 12.0   # 拉高发光，透过虚化的外层也能看清
    nt.links.new(emi.outputs["Emission"], out.inputs["Surface"])
    for a, v in (("blend_method", "OPAQUE"), ("surface_render_method", "DITHERED")):
        try:
            setattr(mat, a, v)
        except Exception:
            pass


def make_transparent(mat):
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    nt.links.new(tr.outputs["BSDF"], out.inputs["Surface"])
    for a, v in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
        try:
            setattr(mat, a, v)
        except Exception:
            pass


def fade(mat, amount=0.82):
    """
    让材质半透明，但**保留原有节点树**（在输出前插一层 MixShader）。
    为什么需要它：很多待确认材质在模型【内层】（例如「表情」被外层脸皮完全遮住），
    只改目标材质颜色的话，整体图里根本看不到它。把其余材质虚化，目标才显形。
    """
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return False
    out = None
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial":
            out = n
            break
    if out is None or not out.inputs["Surface"].is_linked:
        return False
    src = out.inputs["Surface"].links[0].from_socket
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    mix = nt.nodes.new("ShaderNodeMixShader")
    mix.inputs[0].default_value = amount        # amount 越大越透明
    nt.links.new(tr.outputs["BSDF"], mix.inputs[1])
    nt.links.new(src, mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    for a, v in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
        try:
            setattr(mat, a, v)
        except Exception:
            pass
    return True


def main():
    for p in (LOG, DONE):
        try:
            os.remove(p)
        except Exception:
            pass
    os.makedirs(OUT, exist_ok=True)

    with open(CONF, encoding="utf-8") as f:
        conf = json.load(f)
    targets = [x for x in conf["items"] if x.get("requires_user")]
    if not targets:
        log("该模型没有需要确认的材质，跳过预览生成")
        with open(DONE, "w", encoding="utf-8") as f:
            f.write("OK 0\n")
        return
    log("需要预览的材质 %d 个：%s" % (len(targets), "、".join(x["material"] for x in targets)))

    # ---- 临时映射：把推荐分类喂进去，让严格准入通过
    tmp_map = {"schema": "toon-material-map/1",
               "model": {"display_name": conf["model"].get("display_name"),
                         "fingerprint": conf["model"].get("fingerprint"),
                         "note": "预览用临时映射（渲染预览图，不写入正式侧车映射）"},
               "assignments": {}}
    for x in targets:
        g = x.get("proposed_class")
        if not g or g == "UNRESOLVED":
            g = "dark"                      # 没候选的先用 dark 占位，只为让准入通过
        tmp_map["assignments"][x["material"]] = {"group": g, "source": "preview_temp"}
    tmp_path = os.path.join(OUT, "_preview_temp_map.json")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tmp_map, f, ensure_ascii=False, indent=2)

    # ---- 导入模型
    for c in ("bl_ext.blender_org.mmd_tools",):
        try:
            addon_utils.enable(c, default_set=False, persistent=False)
        except Exception as e:
            log("enable %s -> %s" % (c, e))
    for o in list(bpy.data.objects):
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass
    log("importing %s" % PMX)
    bpy.ops.mmd_tools.import_model(filepath=PMX, scale=0.08)

    real = [o for o in bpy.data.objects if o.type == "MESH" and len(o.data.polygons) >= 100]
    for o in list(bpy.data.objects):
        if o.type == "MESH" and o not in real:
            o.hide_render = True
            o.hide_viewport = True
    bpy.ops.object.select_all(action="DESELECT")
    for o in real:
        o.select_set(True)
    if real:
        bpy.context.view_layer.objects.active = real[0]

    # ---- 跑主脚本搭管线（不开渲），用临时映射过严格准入
    os.environ["TOON_V3_DIR"] = V31
    os.environ["TOON_MATERIAL_POLICY"] = "strict"
    os.environ["TOON_MATERIAL_MAP"] = tmp_path
    os.environ["TOON_ALLOW_LOW_CONFIDENCE"] = "0"
    ns = {"__name__": "oneclick_render", "__file__": MAIN_SCRIPT}
    src = open(MAIN_SCRIPT, encoding="utf-8").read()
    exec(compile(src, MAIN_SCRIPT, "exec"), ns)
    ns["SOURCE_BLEND"] = SOURCE
    ns["OUT_DIR"] = OUT
    ns["MATERIAL_POLICY"] = "strict"
    ns["MATERIAL_MAP_PATH"] = tmp_path
    ns["TARGET_MODE"] = "SELECTED"
    ns["DO_RENDER"] = False
    ns["FILM_TRANSPARENT"] = True
    ns["KEEP_ORIGINAL_MATERIALS"] = False
    ns["EXTRA_INPUTS"] = [("pmx", PMX), ("main_script", MAIN_SCRIPT)]
    ns["main"]()
    log("管线就绪，材质已按主脚本重建")

    scene = bpy.context.scene
    r = scene.render
    r.image_settings.file_format = "PNG"
    r.resolution_percentage = 100
    try:
        r.resolution_x = int(RES * 1080 / 1980)
        r.resolution_y = RES
    except Exception:
        pass

    def shoot(tag):
        p = os.path.join(OUT, tag)
        r.filepath = p
        bpy.ops.render.render(write_still=True)
        ok = os.path.exists(p)
        log("  %s %s" % ("✓" if ok else "✗", os.path.basename(p)))
        return p

    mats = {m.name: m for m in bpy.data.materials}
    made = []
    model_file = conf["model"]["file"]

    for x in targets:
        nm = x["material"]
        mat = mats.get(nm)
        if mat is None:
            log("  ! 找不到材质 %s，跳过" % nm)
            continue
        idx = x["index"]
        base = "%s__%d" % (model_file, idx)

        others = [m for n2, m in mats.items()
                  if n2 != nm and not n2.startswith("AI_Outline")]

        # (c) 推荐分类效果：其余材质虚化，目标保持主脚本按推荐分类重建后的样子
        for o in others:
            fade(o)
        made.append(shoot(base + "__proposed.png"))

        # (a) 整体图 + 目标高亮（目标洋红自发光，其余仍虚化）
        make_magenta(mat)
        made.append(shoot(base + "__overview.png"))

        # (b) 只显示目标（其余全透明）
        for o in others:
            make_transparent(o)
        made.append(shoot(base + "__mask.png"))
        log("  完成 %s（序号 %d）" % (nm, idx))

    with open(os.path.join(OUT, "_preview_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "toon-preview-manifest/1", "model": conf["model"]["file"],
                   "images": [os.path.basename(p) for p in made]}, f,
                  ensure_ascii=False, indent=1)

    with open(DONE, "w", encoding="utf-8") as f:
        f.write("OK %d\n" % len(made))
    log("共生成 %d 张预览图" % len(made))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL\n" + traceback.format_exc())
        try:
            with open(DONE, "w", encoding="utf-8") as f:
                f.write("ERROR\n")
        except Exception:
            pass
