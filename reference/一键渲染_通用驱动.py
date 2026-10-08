# -*- coding: utf-8 -*-
"""
通用后台驱动：导入 pmx（或直接打开一个 .blend）→ 运行「一键卡通渲染.py」→ 出图。

审计整改项 7：不再把路径与开关写死在代码里，全部走命令行参数（由 launch_*.py 传入）。

用法：
  blender --background --factory-startup --python 一键渲染_通用驱动.py -- \
      --pmx  <模型.pmx>                    # 二选一：要渲染的 pmx
      --blend <场景.blend>                 # 二选一：直接打开这个 .blend（模型已导入好的场景）
      --out  <输出目录>                    # 必填，建议先建成空目录
      --mode faithful|enhanced             # 合成路径模式，默认 faithful
      --resolve <源工程.blend>             # 提供卡通节点组的源工程
      --script <一键卡通渲染.py>            # 主脚本路径
      --scale 0.08                         # pmx 导入比例（--blend 模式忽略）
      --min-polys 100                      # 小于该面数的网格视为刚体代理，不出图
      --auto-frame 0|1                     # 是否自动取景
      --transparent 0|1                    # 是否透明底
      --hair-ref 0|1                       # 是否启用 Sakura 头发参考组（模型特化开关）
      --glow-threshold <f>                 # 仅 enhanced 模式：Glare 阈值（默认沿用源工程值 1.0）
      --glow-strength  <f>                 # 仅 enhanced 模式：Glare 强度（默认 2.0）
      --glow-size      <f>                 # 仅 enhanced 模式：Glare 尺寸（默认 0.5）
      --log <日志文件>
      --done <完成标记文件>

★ 关于辉光：源工程那套 Glare 参数（Bloom / 阈值1.0 / 强度2.0 / 尺寸0.5）在其工程里
  是【被旁路】的，从未真正生效；而且 Blender 5.x 的 Glare 节点已经没有 Mix 输入，
  照抄过来在多数模型上会明显过曝。用上面三个开关可覆盖为实测可用的值。

--pmx 与 --blend 二选一：前者会清空场景后导入 pmx；后者直接打开给定 .blend，
保留里面已经准备好的场景（材质/骨架/集合），只把渲染管线套上去。

日志同时写 stdout 与 --log 指定的文件；跑完写 --done。
"""
import bpy
import os
import sys
import time
import json
import traceback
import addon_utils


def arg(name, default=None):
    a = sys.argv
    if name in a:
        i = a.index(name)
        if i + 1 < len(a):
            return a[i + 1]
    return default


def arg_flag(name, default=False):
    v = arg(name, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


HERE = os.path.dirname(os.path.abspath(__file__))

PMX = arg("--pmx")
BLEND = arg("--blend")
OUT_DIR = arg("--out")
MODE = (arg("--mode", "faithful") or "faithful").strip().lower()
SOURCE = arg("--resolve", os.path.join(os.path.dirname(HERE), "NARUTO", "Sakura_1.blend"))
SCRIPT = arg("--script", os.path.join(HERE, "一键卡通渲染.py"))
SCALE = float(arg("--scale", "0.08"))
MIN_POLYS = int(arg("--min-polys", "100"))
STRICT = arg_flag("--strict", True)
AUTO_FRAME = arg_flag("--auto-frame", False)
TRANSPARENT = arg_flag("--transparent", True)
HAIR_REF = arg_flag("--hair-ref", True)
GLOW_THRESHOLD = arg("--glow-threshold")
GLOW_STRENGTH = arg("--glow-strength")
GLOW_SIZE = arg("--glow-size")
LOG_PATH = arg("--log", os.path.join(HERE, "一键渲染.stdout.log"))
DONE = arg("--done", os.path.join(HERE, "一键渲染.done"))

if not OUT_DIR or (not PMX and not BLEND):
    raise SystemExit("必须指定 --out，以及 --pmx 或 --blend 之一")
if PMX and BLEND:
    raise SystemExit("--pmx 与 --blend 只能二选一")


def log(m):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sha256_of(path):
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


def enable_mmd():
    for c in ("bl_ext.blender_org.mmd_tools",):
        try:
            addon_utils.enable(c, default_set=False, persistent=False)
        except Exception as e:
            log("enable %s -> %s" % (c, e))


def main():
    for p in (LOG_PATH, DONE,):
        try:
            os.remove(p)
        except Exception:
            pass

    log("blender %s" % bpy.app.version_string)
    log("pmx      = %s" % PMX)
    log("blend    = %s" % BLEND)
    log("out      = %s" % OUT_DIR)
    log("mode     = %s" % MODE)
    log("resolve  = %s" % SOURCE)
    log("script   = %s" % SCRIPT)

    if PMX and not os.path.isfile(PMX):
        raise SystemExit("找不到 pmx：%s" % PMX)
    if BLEND and not os.path.isfile(BLEND):
        raise SystemExit("找不到 blend：%s" % BLEND)
    if not os.path.isfile(SOURCE):
        raise SystemExit("找不到源工程：%s" % SOURCE)
    if not os.path.isfile(SCRIPT):
        raise SystemExit("找不到主脚本：%s" % SCRIPT)

    os.makedirs(OUT_DIR, exist_ok=True)
    pre_existing = sorted(os.listdir(OUT_DIR))
    if pre_existing:
        log("! 输出目录非空（%d 个文件），已被 run_manifest.json 的 pre_existing 记录"
            % len(pre_existing))

    if BLEND:
        # ---- 输入是「已经导入好的场景」：直接打开，保留原有材质/骨架/集合
        enable_mmd()
        log("opening %s" % BLEND)
        bpy.ops.wm.open_mainfile(filepath=BLEND)
        enable_mmd()          # open_mainfile 之后再确保一次，mmd_material 才能取到
        log("opened: scene=%s  objects=%d" % (bpy.context.scene.name, len(bpy.data.objects)))
    else:
        enable_mmd()
        for o in list(bpy.data.objects):
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass

        log("importing %s" % PMX)
        bpy.ops.mmd_tools.import_model(filepath=PMX, scale=SCALE)
    all_meshes = [o for o in bpy.data.objects if o.type == "MESH"]

    # ★ 刚体/骨骼代理小块（mmd_tools 会导入几十上百个）不出图。
    #   单看面数区分：真网格几万面，代理块通常只有几面。
    real = [o for o in all_meshes if len(o.data.polygons) >= MIN_POLYS]
    proxies = [o for o in all_meshes if o not in real]
    log("imported: meshes=%d  真网格=%d  代理块=%d（已 hide_render）"
        % (len(all_meshes), len(real), len(proxies)))
    for o in proxies:
        try:
            o.hide_render = True
            o.hide_viewport = True
        except Exception:
            pass
    for o in real[:10]:
        log("  mesh: %s (mats=%d, polys=%d)" % (o.name, len(o.material_slots), len(o.data.polygons)))

    bpy.ops.object.select_all(action="DESELECT")
    for o in real:
        o.select_set(True)
    if real:
        bpy.context.view_layer.objects.active = real[0]

    ns = {"__name__": "oneclick_render"}
    src = open(SCRIPT, encoding="utf-8").read()
    exec(compile(src, SCRIPT, "exec"), ns)

    # ---- 覆盖配置（全部来自命令行，不再写死）
    ns["SOURCE_BLEND"] = SOURCE
    ns["OUT_DIR"] = OUT_DIR
    ns["PIPELINE_MODE"] = MODE
    ns["STRICT_MATERIAL_MATCH"] = STRICT
    ns["TARGET_MODE"] = "SELECTED"
    ns["DO_RENDER"] = True
    ns["AUTO_FRAME"] = AUTO_FRAME
    ns["FILM_TRANSPARENT"] = TRANSPARENT
    ns["USE_HAIR_REFERENCE_GROUP"] = HAIR_REF
    ns["KEEP_ORIGINAL_MATERIALS"] = False
    # 辉光覆盖（仅对 enhanced 有意义；None 表示沿用源工程值）
    glow_over = {}
    if GLOW_THRESHOLD is not None:
        ns["GLOW"]["Threshold"] = glow_over["Threshold"] = float(GLOW_THRESHOLD)
    if GLOW_STRENGTH is not None:
        ns["GLOW"]["Strength"] = glow_over["Strength"] = float(GLOW_STRENGTH)
    if GLOW_SIZE is not None:
        ns["GLOW"]["Size"] = glow_over["Size"] = float(GLOW_SIZE)
    ns["GLOW_OVERRIDE"] = glow_over
    ns["EXTRA_INPUTS"] = (
        ([("blend", BLEND)] if BLEND else [("pmx", PMX)]) +
        [("driver", os.path.abspath(__file__)), ("main_script", SCRIPT)])

    log("config: PIPELINE_MODE=%s STRICT_MATERIAL_MATCH=%s AUTO_FRAME=%s "
        "FILM_TRANSPARENT=%s HAIRREF=%s"
        % (ns["PIPELINE_MODE"], ns["STRICT_MATERIAL_MATCH"], ns["AUTO_FRAME"],
           ns["FILM_TRANSPARENT"], ns["USE_HAIR_REFERENCE_GROUP"]))
    if glow_over:
        log("glow override: %s（源工程值为 阈值1.0/强度2.0/尺寸0.5，且其工程里该节点被旁路）"
            % glow_over)

    log("=== run one-click pipeline ===")
    ns["main"]()

    # ---- 本次运行后新生成的产物（与历史遗留分开）
    after = sorted(os.listdir(OUT_DIR))
    fresh = [f for f in after if f not in pre_existing]
    log("本次新生成 %d 个文件：" % len(fresh))
    for fn in fresh:
        log("  + %s (%d bytes)" % (fn, os.path.getsize(os.path.join(OUT_DIR, fn))))
    log("sha256(input) = %s" % sha256_of(BLEND or PMX))
    log("sha256(main script) = %s" % sha256_of(SCRIPT))

    with open(DONE, "w", encoding="utf-8") as f:
        f.write("OK\n")


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
