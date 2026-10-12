# -*- coding: utf-8 -*-
"""
PMX 结构特征提取（v3.1）
=====================================================================================
为什么要有这个模块
------------------
v3 的材质分类只吃「材质名 + 少量 PMX 标量（alpha/面数/颜色）」。冷启动 20/29，
剩下 9 个模型卡在语义确实无法从名字判断的材质上（表情/Emotion/新規/汗）。

再加上关键词只会重蹈"逐字打补丁"的覆辙。所以这一版改为**挖结构证据**：

  1. 几何位置   —— 材质对应的顶点在模型包围盒里的位置（高度、是否在头部/口腔区域）
  2. 关联骨骼   —— 这些顶点主要受哪些骨骼驱动（口/眼/头/手/足…）
  3. morph 引用 —— 这些顶点是否被顶点 morph 大幅移动；
                   这个【材质】是否被 material morph 引用（即"切换显示/透明度"的件）
  4. 贴图/标量  —— 贴图名、alpha、漫反射色、双面

第 3 条是最强的：一个材质如果被 material morph 引用，或者它的顶点被顶点 morph
大量位移，那它本来就是"表情切换件" —— 这是**结构事实**，不是名字猜测。

本模块只依赖标准库，可在纯 Python 下离线跑全部 29 个 PMX。
"""
import math
import os
import struct
from collections import Counter, defaultdict

SIG = b"PMX "

# 骨骼 flag
BONE_FLAG_TAIL_IS_BONE = 0x0001
BONE_FLAG_IK = 0x0020
BONE_FLAG_INHERIT_ROT = 0x0100
BONE_FLAG_INHERIT_TRANS = 0x0200
BONE_FLAG_FIXED_AXIS = 0x0400
BONE_FLAG_LOCAL_AXIS = 0x0800
BONE_FLAG_EXTERNAL_PARENT = 0x2000

# morph 类型
MORPH_GROUP = 0
MORPH_VERTEX = 1
MORPH_BONE = 2
MORPH_UV = (3, 4, 5, 6, 7)
MORPH_MATERIAL = 8
MORPH_FLIP = 9
MORPH_IMPULSE = 10

# 面部/口腔相关骨骼关键词（只用于给"位置+骨骼"结果加一个可读标签，不参与判定）
FACE_BONE_HINTS = ("口", "くち", "mouth", "jaw", "あご", "歯", "舌", "唇", "ほお", "頬",
                   "目", "eye", "まぶた", "瞳", "眉", "mayu", "顔", "face", "頭", "head",
                   "髪", "hair", "首", "neck")


class _R(object):
    def __init__(self, path):
        with open(path, "rb") as f:
            self.b = f.read()
        self.p = 0

    def need(self, n):
        if self.p + n > len(self.b):
            raise EOFError("越界 @%d(+%d) / %d" % (self.p, n, len(self.b)))

    def u8(self):
        self.need(1)
        v = self.b[self.p]
        self.p += 1
        return v

    def u16(self):
        self.need(2)
        v = struct.unpack_from("<H", self.b, self.p)[0]
        self.p += 2
        return v

    def i32(self):
        self.need(4)
        v = struct.unpack_from("<i", self.b, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        self.need(4)
        v = struct.unpack_from("<f", self.b, self.p)[0]
        self.p += 4
        return v

    def vec(self, n):
        return [self.f32() for _ in range(n)]

    def skip(self, n):
        self.need(n)
        self.p += n

    def text(self, enc):
        n = self.i32()
        if n <= 0:
            return ""
        self.need(n)
        raw = self.b[self.p:self.p + n]
        self.p += n
        return raw.decode("utf-16-le" if enc == 0 else "utf-8", "replace")

    def idx(self, size):
        if size == 1:
            v = self.u8()
            return v - 256 if v >= 128 else v
        if size == 2:
            v = self.u16()
            return v - 65536 if v >= 32768 else v
        return self.i32()


def load_pmx(path, collect_geometry=True):
    """
    解析 PMX。返回 dict：
      header / textures / materials / bones / morph_material_refs / morph_vertex_hits
      / geometry（顶点位置与骨骼权重，collect_geometry=False 时不收集）
      / warnings（解析降级说明）
    解析失败时尽量保留已拿到的部分（分阶段 try）。
    """
    r = _R(path)
    out = {"path": path, "file": os.path.basename(path), "ok": False, "warnings": [],
           "vertices": [], "faces": [], "textures": [], "materials": [], "bones": [],
           "material_morph_refs": {}, "vertex_morph_hits": {}, "morph_names": {},
           "morph_available": False, "bones_available": False}
    try:
        if r.b[:4] != SIG:
            out["error"] = "签名非 PMX"
            return out
        r.p = 4
        out["version"] = round(r.f32(), 2)
        gcount = r.u8()
        g = [r.u8() for _ in range(gcount)]
        enc, add_uv, vidx, tidx, midx, bidx = g[0], g[1], g[2], g[3], g[4], g[5]
        # PMX globals[6] = morph index size, globals[7] = rigidbody index size。
        # ★ 这两个不能拿 bone/material index size 顶替：
        #   group / flip morph 的索引是【morph index size】、impulse 的是【rigidbody index size】。
        #   阿芙 2.0 含 group morph，用错尺寸会把整个 morph 段读歪（银狼没有 group morph，
        #   所以这个错只在部分模型上暴露 —— 典型的"样本不够就测不出来"）。
        moidx = g[6] if gcount > 6 else bidx
        ridx = g[7] if gcount > 7 else bidx
        out["encoding"] = "UTF-16LE" if enc == 0 else "UTF-8"
        out["index_sizes"] = {"vertex": vidx, "texture": tidx, "material": midx,
                              "bone": bidx, "morph": moidx, "rigidbody": ridx}
        out["model_name"] = r.text(enc)
        out["model_name_en"] = r.text(enc)
        r.text(enc); r.text(enc)

        vcount = r.i32()
        out["vertex_count"] = vcount
        for _ in range(vcount):
            pos = r.vec(3)
            r.skip(12 + 8 + add_uv * 16)          # normal + uv + additional uv
            wt = r.u8()
            bones = []
            if wt == 0:
                bones = [r.idx(bidx)]
            elif wt == 1:
                bones = [r.idx(bidx), r.idx(bidx)]; r.skip(4)
            elif wt == 2:
                bones = [r.idx(bidx) for _ in range(4)]; r.skip(16)
            elif wt == 3:
                bones = [r.idx(bidx), r.idx(bidx)]; r.skip(4 + 36)
            elif wt == 4:
                bones = [r.idx(bidx) for _ in range(4)]; r.skip(16)
            else:
                raise ValueError("weight type %d" % wt)
            r.skip(4)                              # edge scale
            if collect_geometry:
                out["vertices"].append((pos, bones))
            else:
                out["vertices"].append((pos, ()))

        fcount = r.i32()
        out["face_index_count"] = fcount
        if collect_geometry:
            for _ in range(fcount):
                out["faces"].append(r.idx(vidx))
        else:
            r.skip(fcount * vidx)

        tcount = r.i32()
        out["textures"] = [r.text(enc) for _ in range(tcount)]

        mcount = r.i32()
        for i in range(mcount):
            name = r.text(enc); name_en = r.text(enc)
            diffuse = r.vec(4)
            sp = r.vec(3); shin = r.f32(); amb = r.vec(3)
            flag = r.u8()
            edge_color = r.vec(4); edge_size = r.f32()
            ti = r.idx(tidx); ei = r.idx(tidx); em = r.u8(); tflag = r.u8()
            if tflag == 0:
                r.idx(tidx)
            else:
                r.u8()
            memo = r.text(enc)
            surf = r.i32()
            out["materials"].append({
                "index": i, "name": name, "name_en": name_en,
                "diffuse": diffuse, "specular": sp, "shininess": shin, "ambient": amb,
                "draw_flag": flag, "double_sided": bool(flag & 0x01), "has_edge": bool(flag & 0x10),
                "texture_index": ti, "env_index": ei, "env_mode": em,
                "texture": out["textures"][ti] if 0 <= ti < len(out["textures"]) else None,
                "env_texture": out["textures"][ei] if 0 <= ei < len(out["textures"]) else None,
                "memo": memo, "surface_count": surf,
            })
        out["material_count"] = mcount

        # ---- 校验：材质 surface_count 之和应等于面索引总数
        s = sum(m["surface_count"] for m in out["materials"])
        out["surface_count_sum"] = s
        out["surface_count_sums_to_faces"] = (s == fcount)

        # ---- 骨骼（失败不致命）
        try:
            bcount = r.i32()
            for _ in range(bcount):
                nm = r.text(enc); nm_en = r.text(enc)
                pos = r.vec(3)
                parent = r.idx(bidx)
                r.i32()                            # layer
                flags = r.u16()
                if flags & BONE_FLAG_TAIL_IS_BONE:
                    r.idx(bidx)
                else:
                    r.skip(12)
                if flags & (BONE_FLAG_INHERIT_ROT | BONE_FLAG_INHERIT_TRANS):
                    r.idx(bidx); r.skip(4)
                if flags & BONE_FLAG_FIXED_AXIS:
                    r.skip(12)
                if flags & BONE_FLAG_LOCAL_AXIS:
                    r.skip(24)
                if flags & BONE_FLAG_EXTERNAL_PARENT:
                    r.i32()
                if flags & BONE_FLAG_IK:
                    r.idx(bidx); r.i32(); r.skip(4)
                    n = r.i32()
                    for _k in range(n):
                        r.idx(bidx)
                        if r.u8() == 1:
                            r.skip(24)
                out["bones"].append({"name": nm, "name_en": nm_en, "position": pos,
                                     "parent": parent, "flags": flags})
            out["bone_count"] = len(out["bones"])
            out["bones_available"] = True
        except Exception as e:
            out["warnings"].append("骨骼段解析降级：%s: %s" % (type(e).__name__, e))

        # ---- morph（失败不致命，但 morph 是核心证据，失败要显式标出）
        try:
            mcount2 = r.i32()
            vmorph = defaultdict(list)             # vertex_index -> [(morph_name, dist)]
            mrefs = defaultdict(list)              # material_index -> [(morph_name, panel, calc)]
            mnames = {}
            for mi in range(mcount2):
                nm = r.text(enc); nm_en = r.text(enc)
                panel = r.u8(); mt = r.u8()
                mnames[mi] = nm
                cnt = r.i32()
                if mt == MORPH_GROUP:
                    r.skip(cnt * (moidx + 4))
                elif mt == MORPH_VERTEX:
                    for _k in range(cnt):
                        vi = r.idx(vidx)
                        off = r.vec(3)
                        d = math.sqrt(off[0] ** 2 + off[1] ** 2 + off[2] ** 2)
                        vmorph[vi].append((nm, d, panel))
                elif mt == MORPH_BONE:
                    r.skip(cnt * (bidx + 28))
                elif mt in MORPH_UV:
                    r.skip(cnt * (vidx + 16))
                elif mt == MORPH_MATERIAL:
                    for _k in range(cnt):
                        mm = r.idx(midx)
                        calc = r.u8()
                        r.skip(16 + 12 + 4 + 12 + 16 + 4 + 16 + 16 + 16)
                        mrefs[mm].append((nm, panel, calc))
                elif mt == MORPH_FLIP:
                    r.skip(cnt * (moidx + 4))
                elif mt == MORPH_IMPULSE:
                    r.skip(cnt * (ridx + 1 + 24))
                else:
                    raise ValueError("未知 morph type %d（无法安全跳过）" % mt)
            out["vertex_morph_hits"] = dict(vmorph)
            out["material_morph_refs"] = dict(mrefs)
            out["morph_names"] = mnames
            out["morph_count"] = mcount2
            out["morph_available"] = True
        except Exception as e:
            out["warnings"].append("morph 段解析降级：%s: %s" % (type(e).__name__, e))

        out["ok"] = True
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def material_face_ranges(model):
    """PMX 里材质按顺序连续占用面索引。返回 {mat_index: (start, end_exclusive)}。"""
    out = {}
    cur = 0
    for m in model["materials"]:
        n = m["surface_count"]
        out[m["index"]] = (cur, cur + n)
        cur += n
    return out


def compute_features(model, collect_geometry=True):
    """
    为每个材质算结构特征。需要 collect_geometry=True。
    返回 {mat_index: features}
    """
    verts = model["vertices"]
    faces = model["faces"]
    mats = model["materials"]
    if not verts or not mats:
        return {}

    # 模型整体包围盒
    xs = [v[0][0] for v in verts]
    ys = [v[0][1] for v in verts]
    zs = [v[0][2] for v in verts]
    bb_min = (min(xs), min(ys), min(zs))
    bb_max = (max(xs), max(ys), max(zs))
    size = tuple(bb_max[i] - bb_min[i] for i in range(3))
    diag = math.sqrt(sum(s * s for s in size)) or 1.0

    bones = model.get("bones") or []
    mor = model.get("vertex_morph_hits") or {}
    mrefs = model.get("material_morph_refs") or {}
    ranges = material_face_ranges(model)

    feats = {}
    for m in mats:
        i = m["index"]
        s, e = ranges.get(i, (0, 0))
        idxs = set(faces[s:e])
        if not idxs:
            feats[i] = {"empty": True}
            continue
        px = [verts[v][0] for v in idxs]
        mn = tuple(min(p[k] for p in px) for k in range(3))
        mx = tuple(max(p[k] for p in px) for k in range(3))
        ctr = tuple((mn[k] + mx[k]) / 2.0 for k in range(3))
        # 归一化到模型包围盒
        rel_ctr = tuple((ctr[k] - bb_min[k]) / (size[k] or 1.0) for k in range(3))
        rel_size = tuple((mx[k] - mn[k]) / (size[k] or 1.0) for k in range(3))

        bc = Counter()
        for v in idxs:
            for b in verts[v][1]:
                if b is not None and b >= 0:
                    bc[b] += 1
        top_bones = [(bones[b]["name"] if b < len(bones) else "#%d" % b, c)
                     for b, c in bc.most_common(5)]
        # ★ 主要骨骼的归一化高度：判断"这个材质挂在身体哪个部位"比用材质自身包围盒
        #   更准。实测反例：娜娜莉 Emotion1 的包围盒高度中心 0.70（看着像躯干），
        #   但它的驱动骨骼是「上半身2 / 右肩 / 右手首」—— 是上半身/手臂件。
        bone_ys = []
        for b, _c in bc.most_common(5):
            if 0 <= b < len(bones):
                bone_ys.append((bones[b]["position"][1] - bb_min[1]) / (size[1] or 1.0))
        bone_y_center = round(sum(bone_ys) / len(bone_ys), 4) if bone_ys else None

        hit = 0
        maxd = 0.0
        names = Counter()
        panels = Counter()
        for v in idxs:
            h = mor.get(v)
            if h:
                hit += 1
                for nm, d, panel in h:
                    if d > maxd:
                        maxd = d
                    names[nm] += 1
                    panels[panel] += 1
        mref = mrefs.get(i, [])

        feats[i] = {
            "empty": False,
            "vertex_count": len(idxs),
            "triangles": m["surface_count"] // 3,
            "texture_name": m.get("texture"),
            "alpha": round(float(m["diffuse"][3]), 4),
            "diffuse_rgb": [round(float(c), 4) for c in m["diffuse"][:3]],
            "double_sided": m.get("double_sided"),
            "bbox_rel_center": [round(c, 4) for c in rel_ctr],
            "bbox_rel_size": [round(c, 4) for c in rel_size],
            # ★ PMX 是 Y-up：高度必须取 Y 轴（早先用 Z 算过一次，把头发算到了"低位"）
            "height_center": round(rel_ctr[1], 4),
            "height_top": round((mx[1] - bb_min[1]) / (size[1] or 1.0), 4),
            "height_bottom": round((mn[1] - bb_min[1]) / (size[1] or 1.0), 4),
            # 前后轴（Z）：越大越靠前（脸朝 +Z 的模型里，靠前=正面件）
            "depth_center": round(rel_ctr[2], 4),
            "top_bones": top_bones,
            "bone_y_center": bone_y_center,
            "morph_vertex_hit_ratio": round(hit / len(idxs), 4),
            "morph_max_displacement_rel": round(maxd / diag, 5),
            "morph_names": [n for n, _ in names.most_common(6)],
            "morph_panels": dict(panels),
            "material_morph_refs": [{"morph": n, "panel": p, "calc": c} for n, p, c in mref],
            "touched_by_morph": bool(hit or mref),
        }
    return feats


def face_bone_hint(top_bones):
    """给 top_bones 打个可读标签，仅用于报告展示。"""
    hits = []
    for nm, _c in top_bones or []:
        low = nm.lower()
        for kw in FACE_BONE_HINTS:
            if kw in low:
                hits.append(kw)
                break
    return sorted(set(hits))


def analyse(path, collect_geometry=True):
    """一次性拿到模型 + 特征 + 汇总。"""
    model = load_pmx(path, collect_geometry=collect_geometry)
    res = {"file": model.get("file"), "path": path, "ok": model.get("ok"),
           "model_name": model.get("model_name"),
           "warnings": model.get("warnings", []),
           "morph_available": model.get("morph_available"),
           "bones_available": model.get("bones_available"),
           "surface_count_sums_to_faces": model.get("surface_count_sums_to_faces")}
    if not model.get("ok"):
        res["error"] = model.get("error")
        return res, model
    feats = compute_features(model, collect_geometry=collect_geometry) if collect_geometry else {}
    res["features"] = feats
    return res, model
