# -*- coding: utf-8 -*-
"""
确定性分级决策（v3.1）
=====================================================================================
替代"用置信度分数决定是否自动执行"的做法。

标定一个可靠的置信度阈值需要成百上千条真值样本，当前只有个位数。
用 0.60 这种数字决定"要不要自动跑"，看起来精确，实际是把没依据的猜测包装成决策。

------------------------------------------------------------------------------
【2026-10-09 收紧】R1/R2 不再进 AUTO_CONFIRMED，降为 SUGGESTED
------------------------------------------------------------------------------
原因（这条原则贯穿整个 v3.1）：

    结构证据能判断材质【位于哪里】、【是否为切换件/特效件】，
    但**不能单独决定应该使用哪个着色节点组**。

具体地：
  * R1「头部 morph 小件」可能是表情、舌头、眼泪、牙齿或饰品 —— 不一定都是 face_detail；
  * R2「被整体搬走的件」说明它是可隐藏特效，不代表应当用 dark —— 也可能是透明或发光。

在"错误自动分类 = 0"的约束下，把推断当事实是不可接受的。

所以 AUTO_CONFIRMED 现在只允许三个来源：
  1. 已验证的明确语义规则（v3 语义层已确定命中）
  2. 已有模型映射（用户确认过）
  3. 有人工真值覆盖、且所有反例测试通过的结构组合规则 —— **当前为空**，
     需要先积累真值并跑反例测试才能启用

------------------------------------------------------------------------------
四级（统计口径与用户约定一致）：
  MODEL_MAP             → model_map_confirmed
  SEMANTIC_CONFIRMED    → semantic_auto_confirmed
  SUGGESTED             → structure_suggested
  UNRESOLVED            → unresolved

  前两级 = auto_executable（不问用户直接跑）；后两级 = 必须确认。
------------------------------------------------------------------------------
"""
import re

STAGE_MODEL_MAP = "MODEL_MAP"
STAGE_SEMANTIC = "SEMANTIC_CONFIRMED"
STAGE_SUGGESTED = "SUGGESTED"
STAGE_UNRESOLVED = "UNRESOLVED"

STAGE_ORDER = (STAGE_MODEL_MAP, STAGE_SEMANTIC, STAGE_SUGGESTED, STAGE_UNRESOLVED)

#: 统计口径键名（与用户约定一致）
METRIC_KEY = {
    STAGE_MODEL_MAP: "model_map_confirmed",
    STAGE_SEMANTIC: "semantic_auto_confirmed",
    STAGE_SUGGESTED: "structure_suggested",
    STAGE_UNRESOLVED: "unresolved",
}

#: 允许直接执行、不询问用户的级别
AUTO_STAGES = (STAGE_MODEL_MAP, STAGE_SEMANTIC)

#: 结构规则的界值（只用于"是否成立"，不用于打分）
STRONG = {
    "head_bone_y": 0.78,          # R1: 主要骨骼在模型高度 78% 以上
    "morph_driven_ratio": 0.50,   # R1: 至少一半顶点被 morph 驱动
    "part_max_triangles": 3000,   # R1: 是"部件"而非主体
    "huge_disp": 0.50,            # R2: 位移超过 0.5 倍对角线
}

CLASS_FACE_DETAIL = "face_detail"
CLASS_DARK = "dark"

#: 确认页里给用户点的选项（逻辑类 → 中文按钮名）
CHOICES = [
    ("skin", "皮肤"),
    ("face", "脸部"),
    ("eye", "眼睛"),
    ("face_detail", "面部细节（睫/眉/口/牙/舌）"),
    ("hair", "头发/毛/尾"),
    ("cloth", "衣物/鞋帽"),
    ("metal", "金属/配饰"),
    ("emission", "发光"),
    ("dark", "暗部/其它"),
    ("prop", "道具"),
    ("ignore", "忽略（不出图）"),
    ("UNRESOLVED", "暂不确定"),
]

#: 「为什么只是建议」的统一说明（结构规则命中时附加）
WHY_ONLY_SUGGESTION = (
    "结构证据只能证明「它在哪里、是不是切换件/特效件」，"
    "不能决定「该用哪个着色节点组」—— 所以这里只能给建议，不能自动执行。"
)


def rule_R1(ev):
    """
    R1 头部件 morph 小件 —— **只用来生成候选，不再直接执行**。
    它可能是表情、舌头、眼泪、牙齿或饰品，不一定是 face_detail。
    """
    if ev.get("bone_y_center") is None:
        return False
    if ev["bone_y_center"] < STRONG["head_bone_y"]:
        return False
    if (ev.get("morph_vertex_ratio") or 0) < STRONG["morph_driven_ratio"]:
        return False
    tri = ev.get("triangles") or 0
    if tri <= 0 or tri >= STRONG["part_max_triangles"]:
        return False
    return True


def rule_R2(ev):
    """
    R2 被整体搬走的件 —— **只用来生成候选，不再直接执行**。
    它是可隐藏特效，但不一定是 dark（也可能是透明或发光）。
    """
    return (ev.get("morph_max_displacement") or 0) >= STRONG["huge_disp"]


def decide(mat_name, ev, semantic=None, model_map_hit=False, model_map_value=None,
           candidates=None, notes=None, reverse_tested_rules=()):
    """
    返回分级结果（全部字段都是确定性的，没有"分数"）：

      {
        "stage": MODEL_MAP | SEMANTIC_CONFIRMED | SUGGESTED | UNRESOLVED,
        "proposed_class": str|None,
        "structure_rules": ["R1"/"R2"/...],   # 命中的结构规则（仅作候选来源）
        "reasons": [str, ...],
        "candidates": [(class, reason)],
        "requires_user": bool,
        "auto_source": str|None,              # 自动执行时的来源说明
      }

    semantic: 语义层结果（Classification），用于判断"名字是否已经能确定"
    reverse_tested_rules: 已通过反例测试的结构规则 id 集合（当前为空 —— 见模块头说明）
    """
    reasons = []
    cands = list(candidates or [])
    notes = list(notes or [])
    hit_rules = []

    # ---- 1) 用户以前确认过
    if model_map_hit:
        return {
            "stage": STAGE_MODEL_MAP, "proposed_class": model_map_value,
            "structure_rules": [],
            "reasons": ["侧车映射命中：该材质此前已由用户确认为 `%s`" % model_map_value],
            "candidates": [(model_map_value, "用户确认")],
            "requires_user": False,
            "auto_source": "model_map",
            "notes": notes,
        }

    # ---- 2) 语义层已经确定（v3 的明确语义规则 / 精确别名 / 归一别名）
    if semantic is not None and semantic.klass not in ("unresolved",) \
            and not semantic.needs_review:
        return {
            "stage": STAGE_SEMANTIC, "proposed_class": semantic.klass,
            "structure_rules": [],
            "reasons": ["语义规则命中：`%s`（匹配方式 %s，依据 `%s`）"
                        % (semantic.klass, semantic.matched_by, semantic.rule or "—")],
            "candidates": [(semantic.klass, "语义规则")],
            "requires_user": False,
            "auto_source": "semantic_rule",
            "notes": notes,
        }

    # ---- 3) 结构规则：只生成候选，级别一律 SUGGESTED
    if rule_R1(ev):
        hit_rules.append("R1")
        cands.insert(0, (CLASS_FACE_DETAIL,
                         "结构规则 R1：主要骨骼高度 %.2f（位于头部）+ 顶点被 morph 驱动 %d%% "
                         "+ 三角数 %d（是部件）"
                         % (ev["bone_y_center"], round((ev.get("morph_vertex_ratio") or 0) * 100),
                            ev.get("triangles"))))
    if rule_R2(ev):
        hit_rules.append("R2")
        cands.insert(0, (CLASS_DARK,
                         "结构规则 R2：morph 最大位移 %.3f× 模型对角线，顶点被整体搬离原位"
                         % (ev.get("morph_max_displacement") or 0)))

    frozen = [r for r in hit_rules if r in (reverse_tested_rules or ())]
    if frozen:
        # 只有"有人工真值覆盖 + 通过反例测试"的结构规则才允许自动执行（当前没有）
        return {
            "stage": STAGE_SEMANTIC, "proposed_class": cands[0][0],
            "structure_rules": frozen,
            "reasons": ["结构规则 %s 已通过人工真值覆盖与反例测试，允许自动执行"
                        % "、".join(frozen)],
            "candidates": cands, "requires_user": False,
            "auto_source": "reverse_tested_structure_rule", "notes": notes,
        }

    if hit_rules:
        reasons.append("命中结构规则 %s —— %s" % ("、".join(hit_rules), WHY_ONLY_SUGGESTION))
        if "R1" in hit_rules:
            reasons.append("R1 的候选 `face_detail` 只是一种可能：同类结构也可能是舌/泪/牙/饰品")
        if "R2" in hit_rules:
            reasons.append("R2 的候选 `dark` 只是一种可能：可隐藏特效也可能是透明或发光材质")
    elif cands:
        reasons.append(WHY_ONLY_SUGGESTION)

    if cands:
        for c, why in cands[:3]:
            reasons.append("候选 `%s`：%s" % (c, why))
        if (ev.get("morph_vertex_ratio") or 0) >= STRONG["morph_driven_ratio"] \
                and ev.get("bone_y_center") is not None \
                and ev["bone_y_center"] < STRONG["head_bone_y"]:
            reasons.append("说明：它是**非头部**的 morph 切换件 —— 结构上知道它挂在身体哪一段，"
                           "但不知道它该用哪个着色节点组")
        return {
            "stage": STAGE_SUGGESTED, "proposed_class": cands[0][0],
            "structure_rules": hit_rules,
            "reasons": reasons, "candidates": cands, "requires_user": True,
            "auto_source": None, "notes": notes,
        }

    # ---- 4) 无语义 + 无结构依据
    if re.match(r"^material\d*$", (mat_name or "").strip(), re.IGNORECASE):
        reasons.append("`MaterialN` 无语义命名，且结构证据不足以给出候选 —— 默认不自动执行")
    else:
        reasons.append("没有足够的结构证据给出候选")
    for n in notes:
        reasons.append("旁证：%s" % n)
    return {
        "stage": STAGE_UNRESOLVED, "proposed_class": None, "structure_rules": hit_rules,
        "reasons": reasons, "candidates": [], "requires_user": True,
        "auto_source": None, "notes": notes,
    }


def summarize(stages):
    """
    按四级统计，键名与用户约定一致：
      semantic_auto_confirmed / structure_suggested / model_map_confirmed / unresolved
    """
    out = {"model_map_confirmed": 0, "semantic_auto_confirmed": 0,
           "structure_suggested": 0, "unresolved": 0}
    for s in stages:
        k = METRIC_KEY.get(s)
        if k:
            out[k] += 1
    out["total"] = len(stages)
    out["auto_executable"] = out["model_map_confirmed"] + out["semantic_auto_confirmed"]
    out["requires_user"] = out["structure_suggested"] + out["unresolved"]
    return out
