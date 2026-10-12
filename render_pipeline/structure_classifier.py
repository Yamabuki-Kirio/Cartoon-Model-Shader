# -*- coding: utf-8 -*-
"""
结构证据分类器（v3.1）
=====================================================================================
目标：对"名字没法判断"的材质，用**结构证据**给出候选类别 + 置信度 + 可读判据，
      而不是继续往关键词表里塞词。

用到的证据（全部来自 PMX 二进制，不依赖材质名字面）：
  E1 morph 顶点命中率   —— 该材质的顶点有多大比例被顶点 morph 搬动
  E2 morph 最大位移     —— 相对模型对角线（>0.5 说明是被"搬走/弹开"的特效件）
  E3 material morph 引用 —— 该材质是否被材质 morph 引用（= 可切换显示/透明度的件）
  E4 高度位置           —— Y 轴（PMX 是 Y-up）归一化位置，0=脚 1=头顶
  E5 关联骨骼           —— 驱动这些顶点的主要骨骼
  E6 贴图同伴           —— 同一张贴图被哪些材质用；若同伴里已有高置信度分类则"跟票"
  E7 标量               —— alpha / 漫反射色 / 三角数 / 双面

设计原则：**每条判定都必须能说出依据**，并且**允许给不出候选**（unresolved）。
本模块不写死任何"材质名关键词"，唯一用到名字的地方是 E6 的贴图名作为"同一张贴图"的标识。
"""
import math

# 结构阈值（依据 1009 下 29 个 PMX 的实测分布，见 docs/结构特征标定.md）
TH = {
    "head_region": 0.78,          # 高度中心 >= 此值视为头部区域
    "morph_ratio_driven": 0.50,   # 顶点命中率 >= 此值视为"被 morph 驱动"
    "morph_disp_tiny": 0.02,      # 位移小于此值视为"几乎不动"
    "morph_disp_huge": 0.50,      # 位移大于此值视为"被搬走的特效件"
    "small_tri": 30,              # 三角数小于此值视为小件
    "peer_min_conf": 0.85,        # 同伴要有多高置信度才值得跟票
    "peer_min_agree": 0.60,       # 同伴一致度
    "peer_min_count": 3,          # 同伴数下限（少于这个数，跟票不构成独立证据）
    "head_part_max_tri": 3000,    # 判"头部件"的三角数上限
}

CLASS_OF_REGION_HEAD = "face_detail"     # 头部区域的可切换件（源工程里脸部细节走 Cel_Eyes）
CLASS_OF_REGION_BODY = "cloth"           # 身体区域的可切换件
CLASS_OF_HUGE_DISP = "dark"              # 被搬走的特效件


def _r(x, n=4):
    try:
        return round(float(x), n)
    except Exception:
        return None


def evidence_of(feats, mat, model_ctx):
    """把原始特征整理成一组带判读的"证据"。"""
    hc = feats.get("height_center") or 0.0
    byc = feats.get("bone_y_center")
    ratio = feats.get("morph_vertex_hit_ratio") or 0.0
    disp = feats.get("morph_max_displacement_rel") or 0.0
    mrefs = feats.get("material_morph_refs") or []
    bones = [b for b, _ in (feats.get("top_bones") or [])]
    return {
        "morph_vertex_ratio": _r(ratio),
        "morph_max_displacement": _r(disp, 5),
        "material_morph_refs": [m.get("morph") for m in mrefs],
        "height_center": _r(hc),
        "bone_y_center": _r(byc),
        "depth_center": feats.get("depth_center"),
        "top_bones": bones[:3],
        "texture": feats.get("texture_name"),
        "alpha": feats.get("alpha"),
        "triangles": feats.get("triangles"),
        # ★ 头部判定改用【主要骨骼高度】：材质自身包围盒会骗人
        #   （娜娜莉 Emotion1 包围盒高 0.70 像躯干，但骨骼是右肩/右手首，是上半身件）
        "in_head_region": bool(byc is not None and byc >= TH["head_region"]),
        "morph_driven": bool(ratio >= TH["morph_ratio_driven"]),
        "morph_static": bool(ratio <= 1e-9 and not mrefs),
        "huge_displacement": bool(disp >= TH["morph_disp_huge"]),
    }


def classify_by_structure(ev, peers=None):
    """
    返回 {"candidates": [(klass, confidence, [reasons])], "abstain": bool, "notes": [...]}
    peers: {材料同贴图的候选 [(材质名, klass, conf)]}
    """
    cands = []
    notes = []

    # ---- S1 贴图同伴投票（结构性：材质间关联，不是名字关键词）
    #  两条硬约束（第一版没加，结果"汗"跟着 alpha=0 的"汗2"投成了 ignore）：
    #    a) 同伴数量必须够（只有 1 个同伴时，跟票等于复制别人的判断，不是独立证据）
    #    b) 同伴必须是"实体类"，ignore / unresolved 不参与投票
    if peers and len(peers) >= TH["peer_min_count"]:
        agree = {}
        for _n, k, c in peers:
            if k in ("ignore", "unresolved") or not c:
                continue
            if c >= TH["peer_min_conf"]:
                agree.setdefault(k, []).append(c)
        best = None
        for k, cs in agree.items():
            frac = len(cs) / len(peers)
            if frac >= TH["peer_min_agree"] and (best is None or frac > best[1]):
                best = (k, frac, sum(cs) / len(cs))
        if best:
            k, frac, avg = best
            cands.append((k, round(min(0.90, avg * frac), 2),
                          ["E6 贴图同伴：同一张贴图的 %d/%d 个材质已归类为 %s"
                           % (len(agree[k]), len(peers), k)]))

    # ---- S2 被搬走的特效件（位移极大）
    if ev["huge_displacement"]:
        cands.append((CLASS_OF_HUGE_DISP, 0.55,
                      ["E2 morph 最大位移 %.3f×模型对角线（>=%.2f），顶点被整体搬动，"
                       "属特效/位移件" % (ev["morph_max_displacement"], TH["morph_disp_huge"])]))

    # ---- S3/S4 morph 驱动件：按高度区域定类
    if ev["morph_driven"] and not ev["huge_displacement"]:
        if ev["in_head_region"]:
            cands.append((CLASS_OF_REGION_HEAD, 0.75,
                          ["E1 %d%% 顶点被 morph 驱动 + E5 主要骨骼高度 %.2f 位于头部区域"
                           % (round(ev["morph_vertex_ratio"] * 100), ev["bone_y_center"])]))
        else:
            cands.append((CLASS_OF_REGION_BODY, 0.45,
                          ["E1 %d%% 顶点被 morph 驱动，但 E5 主要骨骼高度 %.2f 不在头部 —— "
                           "属身体区域的可切换件，具体该走哪个着色节点组结构证据无法确定"
                           % (round(ev["morph_vertex_ratio"] * 100), ev["bone_y_center"] or -1)]))

    # ---- S3b 头部件兜底（不要求 morph 率）
    # 小贞的「表情」骨骼在头部（0.79）但只有 31% 顶点被 morph 驱动，够不上 S3；
    # 可"主要骨骼在头部 + 是部件"本身已经是像样的证据，给 SUGGESTED 级的候选。
    if ev["in_head_region"] and (ev.get("triangles") or 0) < TH["head_part_max_tri"] \
            and not ev["morph_driven"]:
        cands.append((CLASS_OF_REGION_HEAD, 0.55,
                      ["E5 主要骨骼高度 %.2f 位于头部且三角数 %d 为部件；"
                       "E1 morph 驱动率仅 %d%% 未达强规则门槛，故只能作为候选"
                       % (ev["bone_y_center"], ev["triangles"],
                          round((ev.get("morph_vertex_ratio") or 0) * 100))]))

    # ---- S5 被 material morph 引用：可切换件（给证据，不单独定类）
    if ev["material_morph_refs"]:
        notes.append("E3 被 material morph 引用：%s —— 该材质会被 morph 切换显示/透明度"
                     % "、".join(ev["material_morph_refs"][:3]))

    # ---- S6 静态大件：交回语义/属性层（不在这里猜）
    if ev["morph_static"] and (ev["triangles"] or 0) >= 500:
        notes.append("E1/E3 完全不被 morph 引用且三角数 %d —— 是常规定义几何体，"
                     "不是切换件" % ev["triangles"])

    # ---- S7 小件
    if (ev["triangles"] or 0) < TH["small_tri"]:
        notes.append("E7 三角数仅 %d，属极小件，结构证据不足" % (ev["triangles"] or 0))

    cands.sort(key=lambda x: -x[1])
    return {"candidates": cands, "abstain": not cands, "notes": notes}


def build_peer_index(name_to_feats, name_to_class):
    """
    name_to_class: {材质名: (klass, confidence)}（来自已可信分类的材质）
    返回 {贴图名: [(材质名, klass, conf)]}
    """
    idx = {}
    for nm, f in name_to_feats.items():
        tex = (f or {}).get("texture_name")
        if not tex:
            continue
        kc = name_to_class.get(nm)
        if not kc:
            continue
        idx.setdefault(tex, []).append((nm, kc[0], kc[1]))
    return idx
