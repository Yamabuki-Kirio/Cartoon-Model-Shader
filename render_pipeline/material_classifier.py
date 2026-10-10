# -*- coding: utf-8 -*-
"""
材质分类器（v3 · 分层决策）
=====================================================================================
把 MMD 材质名映射到一个【逻辑分类】，再由 class_to_group 映射到源工程实有的节点组。

分层层级（前一层命中即返回，不再下沉）：
  L1  model_map         模型级侧车映射（用户确认过，最高优先）
  L2  exact_alias       规范化 stem 与别名【完全相等】
  L3  normalized_alias  规范化 compact 以别名【开头/结尾】
  L4  semantic_rule     规范化 compact 上的正则
  L5  attribute_rule    PMX 结构特征（面数 / 颜色 / 透明度）
  L6  unresolved        无法确定 → 交给策略层决定（strict 拒绝 / diagnostic 洋红）

本模块【不复制规则】—— 全部从 material_rules.json 读取，与主脚本同源。
本模块不依赖 bpy，可在纯 Python 下做离线预检与单元测试。
"""
import json
import os
import re
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES_PATH = os.path.join(_HERE, "material_rules.json")


def default_maps_dir():
    """
    侧车映射的默认存放目录 —— **绝不落在仓库内的 model_material_maps/**。

    仓库里那份只放随代码分发的只读样例（example.material-map.json，由
    tests/test_render_pipeline_repo_guard.py 守卫）。用户确认结果与「待填写」
    模板都属于运行数据，按项目既有约定落到用户数据目录（与预设存储同源：
    %LOCALAPPDATA%\\CartoonModelShader）。

    优先级：TOON_MAPS_DIR > --maps-dir（调用方传入）> 本函数默认值。
    """
    env = os.environ.get("TOON_MAPS_DIR")
    if env:
        return os.path.abspath(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "CartoonModelShader", "model_material_maps")

# 规范化时要抹掉的分隔符
_SEP_RE = re.compile(r"[\s_\-\.\+\(\)\[\]\{\}（）【】·・、,，:：/\\|]+")
_TRAIL_NUM_RE = re.compile(r"\d+$")
_BLENDER_EXT_RE = re.compile(r"\.\d{3}$")


def _to_halfwidth(s):
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def strip_blender_ext(name):
    """剥掉 Blender 的重名后缀 .001 / .002（只剥 3 位数字后缀，避免误伤 1.02 这类版本号）"""
    return _BLENDER_EXT_RE.sub("", name or "")


class Rules:
    """material_rules.json 的只读封装。"""

    def __init__(self, path=None):
        self.path = path or DEFAULT_RULES_PATH
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.raw = raw
        self.schema = raw.get("schema")
        self.version = raw.get("version")
        self.class_to_group = dict(raw.get("class_to_group", {}))
        self.class_labels = dict(raw.get("class_labels", {}))
        self.variant_map = {k: v for k, v in raw.get("variant_map", {}).items()
                            if not k.startswith("_")}
        self.ignore_patterns = list(raw.get("ignore_patterns", []))
        self.unresolved_patterns = [{"pattern": re.compile(u["pattern"], re.IGNORECASE),
                                     "pattern_src": u["pattern"],
                                     "hint": u.get("hint", "")}
                                    for u in raw.get("unresolved_patterns", [])]
        self.head_hair_patterns = list(raw.get("head_hair_patterns", []))
        self.confidence = dict(raw.get("confidence", {}))
        self.policy = dict(raw.get("policy", {}))
        self.attribute_rules = dict(raw.get("attribute_rules", {}))

        # ---- 变体字多字优先（必须在 aliases 之前建好：别名表的键也要过变体归一）
        self._variant_keys_desc = sorted(self.variant_map.keys(), key=len, reverse=True)

        # ---- 别名表：key → class；同一个 key 不能映射到两个类（加载即校验）
        self.aliases = {}
        dup = []
        for klass, keys in raw.get("exact_aliases", {}).items():
            if klass.startswith("_"):
                continue
            for k in keys:
                nk = self.normalize_key(k)
                if nk in self.aliases and self.aliases[nk] != klass:
                    dup.append((nk, self.aliases[nk], klass))
                self.aliases[nk] = klass
        self.alias_conflicts = dup
        # L3 按 key 长度降序，长的先试，避免「毛」抢在「睫毛」前面
        self._alias_keys_desc = sorted(self.aliases.keys(), key=len, reverse=True)

        # ---- 语义规则
        self.semantic_rules = []
        for r in raw.get("semantic_rules", []):
            self.semantic_rules.append({
                "id": r.get("id"),
                "pattern": re.compile(r["pattern"], re.IGNORECASE),
                "pattern_src": r["pattern"],
                # 排除词（可选）：命中它则【本条规则不适用】，继续往下试别的层级。
                # 用途：像「眼」这种单字 token 会命中「神之眼」/「眼镜」等非眼睛材质。
                "unless": (re.compile(r["unless"], re.IGNORECASE) if r.get("unless") else None),
                "unless_src": r.get("unless"),
                "class": r.get("class"),
                "confidence": float(r.get("confidence", self.confidence.get("semantic_rule", 0.75))),
                "note": r.get("note", ""),
            })

    # ---------------------------------------------------------------- 规范化
    def apply_variants(self, s):
        """异体字/简繁/日文汉字 → 正字。只用于匹配，不改原材质名。"""
        for k in self._variant_keys_desc:
            if k in s:
                s = s.replace(k, self.variant_map[k])
        return s

    def normalize(self, name):
        """
        返回三种规范化形式（都只用于匹配，绝不回写材质名）：
          nfc     —— NFC + 变体归一 + 去 Blender 后缀（保留大小写与分隔符）
          compact —— nfc 再：全角→半角 + casefold + 抹掉分隔符（保留数字）
          stem    —— compact 再去掉尾部数字
        """
        raw = name or ""
        nfc = self.apply_variants(unicodedata.normalize("NFC", strip_blender_ext(raw)))
        half = _to_halfwidth(nfc)
        compact = _SEP_RE.sub("", half).casefold()
        stem = _TRAIL_NUM_RE.sub("", compact)
        return {"raw": raw, "nfc": nfc, "compact": compact, "stem": stem or compact}

    def normalize_key(self, key):
        """别名表的键也走同一套规范化，保证两边可比。"""
        nfc = self.apply_variants(unicodedata.normalize("NFC", key or ""))
        half = _to_halfwidth(nfc)
        compact = _SEP_RE.sub("", half).casefold()
        return _TRAIL_NUM_RE.sub("", compact) or compact

    def group_of(self, klass):
        return self.class_to_group.get(klass)

    def is_ignore(self, name):
        for p in self.ignore_patterns:
            if re.search(p, name or "", re.IGNORECASE):
                return p
        for p in self.ignore_patterns:
            if re.search(p, self.normalize(name)["compact"], re.IGNORECASE):
                return p
        return None


class Classification(object):
    __slots__ = ("original_name", "normalized", "klass", "group", "matched_by",
                 "rule", "confidence", "needs_review", "note", "layer")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {
            "original_name": self.original_name,
            "normalized_name": self.normalized,
            "class": self.klass,
            "group": self.group,
            "matched_by": self.matched_by,
            "rule": self.rule,
            "confidence": round(float(self.confidence or 0.0), 3),
            "layer": self.layer,
            "needs_review": bool(self.needs_review),
            "note": self.note,
        }

    def __repr__(self):
        return "<%s %s -> %s (%s %.2f)>" % (
            self.__class__.__name__, self.original_name, self.klass,
            self.matched_by, self.confidence or 0.0)


class MaterialClassifier(object):
    def __init__(self, rules_path=None):
        self.rules = Rules(rules_path)
        self._cache = {}

    # ------------------------------------------------------------------ 主入口
    def classify(self, name, model_map=None, ctx=None):
        """
        name      材质名（可带 Blender 的 .001 后缀）
        model_map dict: 材质名 → 逻辑类（用户确认过的侧车映射）
        ctx       dict: 可选结构特征，用于 L5
        """
        r = self.rules
        n = r.normalize(name)
        key = (name, id(model_map), json.dumps(ctx, sort_keys=True) if ctx else None)
        if key in self._cache:
            return self._cache[key]
        res = self._classify_impl(name, n, model_map, ctx)
        self._cache[key] = res
        return res

    def _mk(self, name, n, klass, matched_by, rule, conf, layer, note="", review=None):
        thr = float(self.rules.confidence.get("low_confidence_threshold", 0.60))
        group = self.rules.group_of(klass) if klass not in ("ignore", "unresolved") else None
        needs = (klass == "unresolved") if review is None else review
        if conf is not None and conf < thr and klass not in ("ignore", "unresolved"):
            needs = True
        return Classification(original_name=name, normalized=n["stem"], klass=klass,
                              group=group, matched_by=matched_by, rule=rule,
                              confidence=conf, needs_review=needs, note=note, layer=layer)

    def _classify_impl(self, name, n, model_map, ctx):
        r = self.rules

        # ---- 先看是不是按设计就该忽略的（指示器/描边/表情切换件）
        ign = r.is_ignore(name)
        if ign:
            return self._mk(name, n, "ignore", "ignore_pattern", ign, 1.0, "L0",
                            note="按设计不出图")

        # ---- L1 模型侧车映射
        if model_map:
            for cand in (name, n["nfc"], n["compact"], n["stem"]):
                if cand in model_map:
                    v = model_map[cand]
                    val = v.get("group") if isinstance(v, dict) else v
                    src = v.get("source", "user_confirmed") if isinstance(v, dict) else "user_confirmed"
                    klass, group = self._resolve_map_value(val)
                    c = self._mk(name, n, klass, "model_map", cand, 1.0, "L1",
                                 note="侧车映射(%s → %s)" % (src, val), review=False)
                    c.group = group
                    return c

        # ---- L1.5 已知"必须人工确认"的模式（不猜、也不静默忽略）
        for up in r.unresolved_patterns:
            if up["pattern"].search(name) or up["pattern"].search(n["compact"]):
                return self._mk(name, n, "unresolved", "unresolved_pattern",
                                up["pattern_src"], 0.0, "L6", note=up["hint"], review=True)

        # ---- L2 精确别名
        if n["stem"] in r.aliases:
            k = n["stem"]
            return self._mk(name, n, r.aliases[k], "exact_alias", k,
                            float(r.confidence.get("exact_alias", 0.95)), "L2")
        if n["compact"] in r.aliases:
            k = n["compact"]
            return self._mk(name, n, r.aliases[k], "exact_alias", k,
                            float(r.confidence.get("exact_alias", 0.95)), "L2")

        # ---- L3 规范化匹配（开头/结尾；长键优先）
        for k in r._alias_keys_desc:
            if len(k) < 2:
                continue
            c = n["compact"]
            if c.startswith(k) or c.endswith(k):
                return self._mk(name, n, r.aliases[k], "normalized_alias", k,
                                float(r.confidence.get("normalized_alias", 0.85)), "L3")

        # ---- L4 语义正则（对 compact 与 nfc 都试）
        for rule in r.semantic_rules:
            if not (rule["pattern"].search(n["compact"]) or rule["pattern"].search(n["nfc"])):
                continue
            unless = rule.get("unless")
            if unless and (unless.search(n["compact"]) or unless.search(n["nfc"])):
                # 排除词命中：本条规则不适用（例：「神之眼」不因含「眼」被判成眼睛），
                # 继续往下试其余规则与层级，绝不在这里硬塞一个分类。
                continue
            return self._mk(name, n, rule["class"], "semantic_rule", rule["id"],
                            rule["confidence"], "L4", note=rule["note"])

        # ---- L5 结构特征
        if ctx:
            for ar in r.attribute_rules.get("rules", []):
                if self._attr_match(ar.get("when", {}), ctx):
                    return self._mk(name, n, ar["class"], "attribute_rule", ar["id"],
                                    float(ar.get("confidence", 0.40)), "L5",
                                    note=ar.get("note", "结构特征推断·低置信度"))

        # ---- L6 无法确定
        return self._mk(name, n, "unresolved", "unresolved", None, 0.0, "L6")

    def _resolve_map_value(self, val):
        """
        侧车映射的值允许两种写法：
          逻辑类   "skin"      → 用 class_to_group 换出节点组
          节点组名 "Cel_Skin"  → 直接用（方案示例就是这种写法）
        非法值一律落 unresolved，由 strict 拦下（预检器还会把它记进资源问题）。
        """
        if not val:
            return "unresolved", None
        v = str(val).strip()
        if v.lower() == "ignore":
            return "ignore", None
        if v.lower() == "unresolved":
            return "unresolved", None
        if v in self.rules.class_to_group:
            return v, self.rules.class_to_group[v]
        for k, g in self.rules.class_to_group.items():
            if g == v:
                return k, g
        return "unresolved", None

    @staticmethod
    def _attr_match(when, ctx):
        for k, v in when.items():
            if k == "surface_count_below":
                if not (ctx.get("surface_count") is not None and ctx["surface_count"] < v):
                    return False
            elif k == "alpha_below":
                a = ctx.get("alpha")
                if not (a is not None and a < v):
                    return False
            elif k == "diffuse_all_channels_high":
                rgb = ctx.get("diffuse_rgb") or [0, 0, 0]
                if not all(c >= 0.5 for c in rgb):
                    return False
            else:
                return False
        return True

    # ------------------------------------------------------------------ 批量
    def classify_all(self, names, model_map=None, ctx_map=None):
        """names: [材质名]; ctx_map: {材质名: ctx}"""
        out = []
        for nm in names:
            ctx = (ctx_map or {}).get(nm)
            out.append(self.classify(nm, model_map=model_map, ctx=ctx))
        return out

    @staticmethod
    def summarize(classifications, policy="strict"):
        """汇总成 run_manifest 里 material_classification 需要的结构"""
        from collections import Counter
        by_src = Counter()
        by_class = Counter()
        unresolved, low, ignore = [], [], []
        for c in classifications:
            by_src[c.matched_by] += 1
            by_class[c.klass] += 1
            if c.klass == "ignore":
                ignore.append(c)
            elif c.klass == "unresolved":
                unresolved.append(c)
            elif c.needs_review:
                low.append(c)
        total = len(classifications)
        resolved = total - len(unresolved) - len(ignore)
        return {
            "total": total,
            "resolved": resolved,
            "unresolved": len(unresolved),
            "low_confidence": len(low),
            "ignored": len(ignore),
            "mapping_source_counts": dict(by_src),
            "class_counts": dict(by_class),
            "policy": policy,
            "unresolved_names": [c.original_name for c in unresolved],
            "low_confidence_names": [c.original_name for c in low],
        }


def load_classifier(rules_path=None):
    return MaterialClassifier(rules_path)


# ---------------------------------------------------------------- 侧车映射
def map_path_for(model_dir, fingerprint, maps_dir=None):
    """
    侧车映射文件路径。身份【只看指纹】，不看路径 —— 移动模型目录不会丢映射。
    默认目录见 default_maps_dir()：**不是**仓库内的 model_material_maps/。
    """
    maps_dir = maps_dir or default_maps_dir()
    return os.path.join(maps_dir, "%s.material-map.json" % fingerprint)


def load_model_map(model_dir=None, fingerprint=None, maps_dir=None, explicit_path=None):
    """
    返回 (assignments_dict, meta_dict)。找不到返回 (None, None)。
    assignments: {材质名: {"group": "Cel_Skin", "source": "user_confirmed"}}
    """
    path = explicit_path or (map_path_for(model_dir, fingerprint, maps_dir) if fingerprint else None)
    if not path or not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    raw = data.get("assignments", {}) or {}
    assignments = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            assignments[k] = {"group": v.get("group") or v.get("class"),
                              "source": v.get("source", "user_confirmed")}
        else:
            assignments[k] = {"group": v, "source": "user_confirmed"}
    return assignments, data


def write_map_template(path, entries, model_meta=None):
    """
    生成「待填写」模板：所有待确认材质先写成 UNRESOLVED。

    ★ source 必须写 template_unconfirmed，不能写 user_confirmed ——
      模板是管线生成的**待办清单**，不是用户确认结果；标成 user_confirmed 会让
      下游报告与审计误以为「这些归类已经有人确认过」。
    """
    data = {
        "schema": "toon-material-map/1",
        "kind": "template",
        "status": "unconfirmed",
        "model": model_meta or {},
        "assignments": {name: {"group": "UNRESOLVED", "source": "template_unconfirmed",
                               "hint": hint} for name, hint in entries}
    }
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
