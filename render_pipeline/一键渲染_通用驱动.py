# -*- coding: utf-8 -*-
"""
通用后台驱动 v3：导入 pmx（或直接打开一个 .blend）→ 材质预检 → 运行主脚本 → 出图。

v3 相对 v2 的变化：
  * 新增 --material-policy strict|diagnostic（取代旧的 --strict 布尔）
  * 新增 --material-map / --write-map-template / --allow-low-confidence
  * 新增 --preflight-only：不开场景、只出材质预检报告（毫秒级，不用等 Blender 跑完）
  * 新增 --report-dir：预检与台账的落盘目录
  * 旧参数 --strict 1/0 仍可用，会打印弃用警告并分别映射到 strict / diagnostic
  * 结果写进 --done：SUCCESS / DIAGNOSTIC / REJECTED / FAILED —— 只有 SUCCESS 可交付

用法：
  blender --background --factory-startup --python 一键渲染_通用驱动.py -- \
      --pmx  <模型.pmx>                    # 二选一
      --blend <场景.blend>                 # 二选一
      --out  <输出目录>                    # 必填
      --material-policy strict|diagnostic  # 默认 strict
      --material-map <侧车映射.json>        # 可选，优先于按指纹自动查找
      --preflight-only                     # 只出预检报告就退出
      --write-map-template                 # 额外生成待填写的映射模板
      --allow-low-confidence               # 允许低置信度分类继续（默认不允许）
      --report-dir <目录>                  # 预检/台账落盘目录（默认 = --out）
      --mode faithful|enhanced
      --resolve <源工程.blend> --script <主脚本.py>
      --scale 0.08 --min-polys 100 --auto-frame 0|1 --transparent 0|1 --hair-ref 0|1
      --glow-threshold <f> --glow-strength <f> --glow-size <f>
      --log <日志文件> --done <完成标记文件>

关于辉光：源工程那套 Glare 参数在其工程里是被旁路的、从未生效，且 Blender 5.x 的
Glare 节点已无 Mix 输入，照抄会明显过曝。用上面三个开关覆盖为实测可用的值。
"""
import bpy
import os
import sys
import time
import json
import traceback
import addon_utils

# ★ 不要在仓库里留 __pycache__（主脚本会 import material_classifier）
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
V3_DIR = HERE
sys.path.insert(0, HERE)
import run_contract as RC          # noqa: E402  终态契约（唯一判定点）


def write_result(outcome, extra=None):
    """
    写结构化终态：--done 写大写 token；同时写 run_result.json（含 shippable 标志）。
    非 SUCCESS 的产物一律标记 shippable=false，成品消费端应据此拒绝。
    """
    token = RC.done_token(outcome)
    with open(DONE, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    payload = RC.result_payload(outcome, extra)
    payload["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "run_result.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("  ! run_result.json 写出失败：%s" % e)
    return payload


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


PMX = arg("--pmx")
BLEND = arg("--blend")
# ★ 路径文件兼容通道：
#   Python 参数数组、PowerShell、cmd 与 Git Bash 的 Unicode 路径传递均已验证无损。
#   --pmx-file / --blend-file 作为冗余工程接口保留，适合不便直接传长路径的调用方。
PMX_FILE = arg("--pmx-file")
BLEND_FILE = arg("--blend-file")
OUT_DIR = arg("--out")
MODE = (arg("--mode", "faithful") or "faithful").strip().lower()
SOURCE = arg("--resolve", os.environ.get("TOON_SRC_BLEND", ""))
SCRIPT = arg("--script", os.path.join(HERE, "一键卡通渲染.py"))
SCALE = float(arg("--scale", "0.08"))
MIN_POLYS = int(arg("--min-polys", "100"))
AUTO_FRAME = arg_flag("--auto-frame", False)
TRANSPARENT = arg_flag("--transparent", True)
HAIR_REF = arg_flag("--hair-ref", True)
GLOW_THRESHOLD = arg("--glow-threshold")
GLOW_STRENGTH = arg("--glow-strength")
GLOW_SIZE = arg("--glow-size")
LOG_PATH = arg("--log", os.path.join(HERE, "一键渲染.stdout.log"))
DONE = arg("--done", os.path.join(HERE, "一键渲染.done"))

# ---- v3 材质政策相关
LEGACY_STRICT = arg("--strict", None)
POLICY = arg("--material-policy", None)
MATERIAL_MAP = arg("--material-map")
PREFLIGHT_ONLY = arg_flag("--preflight-only", False)
WRITE_MAP_TEMPLATE = arg_flag("--write-map-template", False)
ALLOW_LOW_CONF = arg_flag("--allow-low-confidence", False)
REPORT_DIR = arg("--report-dir")
RULES_PATH = arg("--rules", os.path.join(HERE, "material_rules.json"))

_deprecated = False
if POLICY is None:
    if LEGACY_STRICT is not None:
        POLICY = "strict" if str(LEGACY_STRICT).strip().lower() in ("1", "true", "yes", "on") \
            else "diagnostic"
        _deprecated = True
    else:
        POLICY = "strict"
POLICY = str(POLICY).strip().lower()
if POLICY not in ("strict", "diagnostic"):
    raise SystemExit("--material-policy 只能是 strict 或 diagnostic，收到 %r" % POLICY)

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


def resolve_path_arg(direct, via_file, what):
    """
    取模型路径。两种来源：
      direct    直接命令行参数（默认）
      via_file  UTF-8 文本文件，内含一行路径（冗余兼容接口）
    同时把"原始值"和"安全显示值"都记进日志，便于定位是哪一段坏掉的。
    """
    path = direct
    src = "argv"
    if via_file:
        try:
            with open(via_file, "r", encoding="utf-8") as f:
                path = f.read().strip().lstrip("\ufeff")
            src = "file:%s" % via_file
        except Exception as e:
            raise SystemExit("读取 %s 路径文件失败：%s" % (what, e))
    if path:
        try:
            path = os.path.normpath(path)
        except Exception:
            pass
        log("%s 路径来源=%s" % (what, src))
        log("  repr     = %r" % path)
        log("  escapes  = %s" % path.encode("unicode_escape").decode("ascii"))
        log("  exists   = %s" % os.path.exists(path))
    return path


def enable_mmd():
    for c in ("bl_ext.blender_org.mmd_tools",):
        try:
            addon_utils.enable(c, default_set=False, persistent=False)
        except Exception as e:
            log("enable %s -> %s" % (c, e))


# ------------------------------------------------------------------ 预检（不需要场景）
def run_preflight(target_pmx, report_dir):
    sys.path.insert(0, V3_DIR)
    from material_classifier import MaterialClassifier, map_path_for, write_map_template
    import pmx_material_probe as probe

    clf = MaterialClassifier(RULES_PATH)
    os.makedirs(report_dir, exist_ok=True)
    rec = probe.preflight(target_pmx, clf,
                          maps_dir=os.path.join(V3_DIR, "model_material_maps"),
                          explicit_map=MATERIAL_MAP)

    # 报告可能被带出本机：把绝对路径换成相对/文件名
    rec_out = dict(rec)
    try:
        rec_out["pmx_path"] = os.path.relpath(rec["pmx_path"], os.path.dirname(target_pmx))
    except Exception:
        rec_out["pmx_path"] = rec["pmx"]

    jp = os.path.join(report_dir, "material_preflight.json")
    mp = os.path.join(report_dir, "material_preflight.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rec_out, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(probe.render_md(rec_out))
    log("预检 → %s" % jp)
    log("预检 → %s" % mp)

    s = rec.get("summary") or {}
    log("预检结论：材质 %s / 已解析 %s / 未解决 %s / 低置信 %s / 忽略 %s → %s"
        % (s.get("total"), s.get("resolved"), s.get("unresolved"),
           s.get("low_confidence"), s.get("ignored"),
           "可通过严格模式" if rec["would_pass_strict"] else "会被严格模式拒绝"))

    if WRITE_MAP_TEMPLATE:
        fp = (rec.get("fingerprint_file") or "").split(":", 1)[-1]
        dst = map_path_for(None, fp, os.path.join(V3_DIR, "model_material_maps"))
        entries = [(d["original_name"], "建议 %s（%s）" % (d["class"], d["matched_by"]))
                   for d in rec["materials"] if d["needs_review"] or d["class"] == "unresolved"]
        write_map_template(dst, entries, {
            "display_name": rec.get("model_name") or rec["pmx"],
            "fingerprint": rec.get("fingerprint_file"),
            "fingerprint_structure": rec.get("fingerprint_structure"),
            "pmx": rec["pmx"]})
        log("映射模板 → %s（%d 项待确认）" % (dst, len(entries)))
    return rec


def main():
    global PMX, BLEND
    for p in (LOG_PATH, DONE):
        try:
            os.remove(p)
        except Exception:
            pass

    PMX = resolve_path_arg(PMX, PMX_FILE, "pmx")
    BLEND = resolve_path_arg(BLEND, BLEND_FILE, "blend")

    log("=" * 72)
    log("一键渲染驱动 v3")
    log("=" * 72)
    log("blender  = %s" % bpy.app.version_string)
    log("pmx      = %s" % PMX)
    log("blend    = %s" % BLEND)
    log("out      = %s" % OUT_DIR)
    log("mode     = %s" % MODE)
    log("policy   = %s%s" % (POLICY, "（由已弃用的 --strict 映射而来）" if _deprecated else ""))
    if _deprecated:
        log("  ⚠ --strict 已弃用：请改用 --material-policy strict|diagnostic。"
            "注意旧语义 --strict 0 现在对应 diagnostic（出带标识的诊断图），"
            "不再等于「静默兜底到 Cel_Dark」——静默兜底已彻底移除。")
    log("map      = %s" % (MATERIAL_MAP or "(按模型指纹自动查找)"))
    log("rules    = %s" % RULES_PATH)
    log("resolve  = %s" % SOURCE)
    log("script   = %s" % SCRIPT)

    if not OUT_DIR:
        raise SystemExit("必须指定 --out")
    if PMX and BLEND:
        raise SystemExit("--pmx/--pmx-file 与 --blend/--blend-file 只能二选一")
    if not PMX and not BLEND:
        raise SystemExit("必须指定 --pmx 或 --blend（或对应的 -file 变体）之一")
    if PMX and not os.path.isfile(PMX):
        raise SystemExit("找不到 pmx：%s" % PMX)
    if BLEND and not os.path.isfile(BLEND):
        raise SystemExit("找不到 blend：%s" % BLEND)
    if not os.path.isfile(SOURCE):
        raise SystemExit("找不到源工程：%s" % SOURCE)
    if not os.path.isfile(SCRIPT):
        raise SystemExit("找不到主脚本：%s" % SCRIPT)
    if not os.path.isfile(RULES_PATH):
        raise SystemExit("找不到规则源：%s" % RULES_PATH)

    report_dir = REPORT_DIR or OUT_DIR
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- 只做预检：连场景都不用开
    if PREFLIGHT_ONLY:
        if not PMX:
            raise SystemExit("--preflight-only 需要 --pmx（预检读的就是 PMX 文件本身）")
        rec = run_preflight(PMX, report_dir)
        ok = bool(rec["would_pass_strict"])
        tok = "SUCCESS" if ok else "REJECTED"
        with open(DONE, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        log("预检终态：%s（这只表示「严格模式会不会通过」，不是渲染结果）" % tok)
        return

    pre_existing = sorted(os.listdir(OUT_DIR))
    if pre_existing:
        log("! 输出目录非空（%d 个文件），已在 run_manifest.json 的 pre_existing 记录"
            % len(pre_existing))

    if BLEND:
        enable_mmd()
        log("opening %s" % BLEND)
        bpy.ops.wm.open_mainfile(filepath=BLEND)
        enable_mmd()
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

    bpy.ops.object.select_all(action="DESELECT")
    for o in real:
        o.select_set(True)
    if real:
        bpy.context.view_layer.objects.active = real[0]

    # ---- 让主脚本在 import 阶段就读到正确政策（它顶部从环境变量取值）
    os.environ["TOON_V3_DIR"] = V3_DIR
    os.environ["TOON_MATERIAL_POLICY"] = POLICY
    os.environ["TOON_RULES"] = RULES_PATH
    os.environ["TOON_ALLOW_LOW_CONFIDENCE"] = "1" if ALLOW_LOW_CONF else "0"
    if MATERIAL_MAP:
        os.environ["TOON_MATERIAL_MAP"] = MATERIAL_MAP

    ns = {"__name__": "oneclick_render", "__file__": SCRIPT}
    src = open(SCRIPT, encoding="utf-8").read()
    exec(compile(src, SCRIPT, "exec"), ns)

    # ---- 覆盖配置（全部来自命令行）
    ns["SOURCE_BLEND"] = SOURCE
    ns["OUT_DIR"] = OUT_DIR
    ns["PIPELINE_MODE"] = MODE
    ns["MATERIAL_POLICY"] = POLICY
    ns["STRICT_MATERIAL_MATCH"] = (POLICY == "strict")
    ns["ALLOW_LOW_CONFIDENCE"] = ALLOW_LOW_CONF
    ns["MATERIAL_MAP_PATH"] = MATERIAL_MAP
    ns["TARGET_MODE"] = "SELECTED"
    ns["DO_RENDER"] = True
    ns["AUTO_FRAME"] = AUTO_FRAME
    ns["FILM_TRANSPARENT"] = TRANSPARENT
    ns["USE_HAIR_REFERENCE_GROUP"] = HAIR_REF
    ns["KEEP_ORIGINAL_MATERIALS"] = False
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
        [("driver", os.path.abspath(__file__)), ("main_script", SCRIPT),
         ("rules", RULES_PATH)])

    log("config: PIPELINE_MODE=%s POLICY=%s AUTO_FRAME=%s FILM_TRANSPARENT=%s HAIRREF=%s"
        % (MODE, POLICY, AUTO_FRAME, TRANSPARENT, HAIR_REF))

    log("=== run one-click pipeline ===")
    ns["main"]()
    outcome = ns.get("RUN_OUTCOME", "failed")

    if outcome == RC.OUTCOME_REJECTED:
        log("=" * 72)
        log(" 终态：REJECTED —— 严格模式在【材质重建与正式渲染之前】拦下（这是设计行为）")
        log(" 准确口径：PMX 已导入当前 Blender 临时进程，内存场景确实变化；")
        log("           但本轮不重建材质、不渲染、不保存 .blend、不输出成品 PNG，")
        log("           也不修改源资产（源工程/PMX 文件哈希保持不变）。")
        log("=" * 72)
        write_result(outcome)
        return

    after = sorted(os.listdir(OUT_DIR))
    fresh = [f for f in after if f not in pre_existing]
    log("本次新生成 %d 个文件：" % len(fresh))
    for fn in fresh:
        log("  + %s (%d bytes)" % (fn, os.path.getsize(os.path.join(OUT_DIR, fn))))
    log("sha256(input) = %s" % sha256_of(BLEND or PMX))
    log("sha256(main script) = %s" % sha256_of(SCRIPT))
    log("sha256(rules) = %s" % sha256_of(RULES_PATH))

    payload = write_result(outcome)
    log("终态：%s  shippable=%s" % (RC.done_token(outcome), payload["shippable"]))
    if not payload["shippable"]:
        log("  ! 该结果【不是成品】：诊断图仅用于定位问题，不能被成品流程消费")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL\n" + traceback.format_exc())
        try:
            write_result(RC.OUTCOME_FAILED)
        except Exception:
            pass
