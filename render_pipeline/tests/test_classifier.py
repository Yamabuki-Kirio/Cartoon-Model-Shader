# -*- coding: utf-8 -*-
"""
材质分类器单元测试（v3）

跑法：
  python -m unittest discover -s tests -v
  （在 通用材质适配_v3/ 目录下执行）
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_V3 = os.path.dirname(_HERE)
sys.path.insert(0, _V3)

from material_classifier import (MaterialClassifier, Rules,  # noqa: E402
                                 map_path_for, load_model_map, write_map_template)
from pmx_material_probe import render_md  # noqa: E402


class TestNormalization(unittest.TestCase):
    """规范化：简繁异体 / 全角半角 / 大小写 / NFC / 分隔符 / .001 后缀"""

    @classmethod
    def setUpClass(cls):
        cls.c = MaterialClassifier()

    def cls_of(self, name, **kw):
        return self.c.classify(name, **kw).klass

    def test_variant_yan(self):
        """顔(日文/繁体) 与 颜(简体) 必须同类 —— 旧版一字之差拖垮 7 个模型"""
        self.assertEqual(self.cls_of("顔"), self.cls_of("颜"))
        self.assertEqual(self.cls_of("颜"), "face")
        self.assertEqual(self.cls_of("顔2"), "face")

    def test_variant_skin(self):
        self.assertEqual(self.cls_of("皮膚"), self.cls_of("皮肤"))
        self.assertEqual(self.cls_of("皮肤"), "skin")
        self.assertEqual(self.cls_of("身_皮肤"), "skin")

    def test_lash_variants(self):
        self.assertEqual(self.cls_of("睫"), "face_detail")
        self.assertEqual(self.cls_of("睫毛"), "face_detail")
        self.assertEqual(self.cls_of("睫2"), "face_detail")

    def test_blender_ext_suffix(self):
        """.001 后缀必须被剥掉（旧版 ^...$ 尾锚点就是被它打掉的）"""
        self.assertEqual(self.cls_of("颜.001"), "face")
        self.assertEqual(self.cls_of("身_皮肤.001"), "skin")
        self.assertEqual(self.cls_of("头_Material1.001"), "hair")

    def test_ext_suffix_not_eating_version(self):
        """只剥 3 位数字后缀，不能把 1.02 这类版本号也剥了"""
        r = Rules()
        self.assertEqual(r.normalize("璐璐卡1.02")["stem"], r.normalize("璐璐卡1")["stem"])

    def test_fullwidth_and_case(self):
        """全角与大小写归一"""
        self.assertEqual(self.cls_of("ＦＡＣＥ"), "face")
        self.assertEqual(self.cls_of("FACE"), "face")
        self.assertEqual(self.cls_of("Skin_Body"), self.cls_of("skinbody"))

    def test_unicode_nfc(self):
        r = Rules()
        nfd = "が"          # か + 浊点（可分解）
        import unicodedata
        self.assertEqual(r.normalize(nfd)["compact"], r.normalize(unicodedata.normalize("NFC", nfd))["compact"])

    def test_separators_ignored(self):
        """空格 / 下划线 / 点 / 加号 / 括号 都不应影响匹配"""
        for nm in ("身_皮肤", "身 皮肤", "身-皮肤", "身·皮肤", "身（皮肤）"):
            self.assertEqual(self.cls_of(nm), "skin", nm)

    def test_normalize_does_not_mutate_name(self):
        """规范化只用于匹配，绝不改原材质名"""
        c = self.c.classify("顔.001")
        self.assertEqual(c.original_name, "顔.001")
        self.assertEqual(c.normalized, "颜")


class TestLayering(unittest.TestCase):
    """分层决策：L1 侧车 > L2 精确 > L3 归一 > L4 语义 > L5 结构 > L6 未解决"""

    @classmethod
    def setUpClass(cls):
        cls.c = MaterialClassifier()

    def test_order_rule_legring_not_skin(self):
        """「腿环」必须走 cloth —— 旧版会被 skin 规则的「腿」抢先吃成 Cel_Skin"""
        c = self.c.classify("身_腿环")
        self.assertEqual(c.klass, "cloth")
        self.assertEqual(c.group, "Cel_Cloth")

    def test_model_map_beats_everything(self):
        """L1 侧车映射优先级最高：即使语义规则也想吃掉它"""
        nm = "顔"
        self.assertEqual(self.c.classify(nm).klass, "face")
        c = self.c.classify(nm, model_map={nm: {"group": "Cel_Hair", "source": "user_confirmed"}})
        self.assertEqual(c.matched_by, "model_map")
        self.assertEqual(c.group, "Cel_Hair")
        self.assertEqual(c.confidence, 1.0)
        self.assertFalse(c.needs_review)

    def test_model_map_key_with_ext(self):
        """侧车键可以带 .001 后缀，也能对上"""
        c = self.c.classify("颜.001", model_map={"颜.001": {"group": "Cel_Skin"}})
        self.assertEqual(c.matched_by, "model_map")

    def test_l2_reports_layer_and_rule(self):
        c = self.c.classify("颜")
        self.assertEqual(c.layer, "L2")
        self.assertEqual(c.matched_by, "exact_alias")
        self.assertEqual(c.rule, "颜")

    def test_l4_semantic_for_unseen_name(self):
        """没进别名表的语义名，应由 L4 语义层接住"""
        c = self.c.classify("EyelashUpper")   # 不在 exact_aliases
        self.assertIn(c.layer, ("L3", "L4"))
        self.assertEqual(c.klass, "face_detail")

    def test_head_hair_pattern(self):
        self.assertEqual(self.c.classify("头_Material6").klass, "hair")
        self.assertEqual(self.c.classify("头_Material10.001").klass, "hair")

    def test_body_shell_is_cloth(self):
        """源工程里「身_体」是躯干外壳，走 Cel_Cloth 不是 skin"""
        self.assertEqual(self.c.classify("身_体").klass, "cloth")


class TestUnresolvedAndIgnore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = MaterialClassifier()

    def test_emotion_is_unresolved_not_ignored(self):
        """表情件绝不能静默忽略：阿芙的「表情」有 7155 面、贴图 表情1.png"""
        for nm in ("表情", "表情1", "Emotion1", "Emotion2"):
            c = self.c.classify(nm)
            self.assertEqual(c.klass, "unresolved", nm)
            self.assertTrue(c.needs_review, nm)
            self.assertEqual(c.matched_by, "unresolved_pattern")

    def test_shinki_is_unresolved(self):
        """「新規」实测贴的是 体.png（15792 面），是躯干，绝不能忽略"""
        c = self.c.classify("新規")
        self.assertEqual(c.klass, "unresolved")
        self.assertIn("体.png", c.note)

    def test_material_n_never_guessed(self):
        """MaterialN 无语义：不猜、也不忽略，交给人工"""
        for i in (0, 2, 4, 13):
            c = self.c.classify("Material%d" % i)
            self.assertEqual(c.klass, "unresolved", "Material%d" % i)
            self.assertTrue(c.needs_review)
        # 但 Blender 默认名 "Material" 本身按设计忽略
        self.assertEqual(self.c.classify("Material").klass, "ignore")

    def test_ignore_patterns(self):
        for nm in ("Outline", "描边", "インジケータ", "ダミー"):
            self.assertEqual(self.c.classify(nm).klass, "ignore", nm)

    def test_low_confidence_marked_for_review(self):
        """结构层推断出的低置信度结果必须要求人工确认"""
        ctx = {"surface_count": 6, "alpha": 1.0, "diffuse_rgb": [0.2, 0.2, 0.2]}
        c = self.c.classify("汗", ctx=ctx)
        self.assertEqual(c.layer, "L5")
        self.assertLess(c.confidence, 0.60)
        self.assertTrue(c.needs_review)

    def test_invisible_material_is_ignored(self):
        """MMD alpha≈0 的材质本来就不可见，属结构确定判断"""
        ctx = {"surface_count": 600, "alpha": 0.0, "diffuse_rgb": [1, 1, 1]}
        c = self.c.classify("某个没见过的名字", ctx=ctx)
        self.assertEqual(c.klass, "ignore")
        self.assertFalse(c.needs_review)


class TestConflictDetection(unittest.TestCase):
    """同一别名映射到两个类 → 加载时就必须暴露，不能默默取其一"""

    def test_alias_conflict_detected(self):
        with open(os.path.join(_V3, "material_rules.json"), encoding="utf-8") as f:
            rules = json.load(f)
        rules["exact_aliases"]["skin"].append("冲突词")
        rules["exact_aliases"]["cloth"].append("冲突词")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "r.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(rules, f, ensure_ascii=False)
            r = Rules(p)
            self.assertTrue(any(x[0] == "冲突词" for x in r.alias_conflicts),
                            "重复别名未被检出：%s" % r.alias_conflicts)

    def test_shipped_rules_have_no_conflict(self):
        r = Rules()
        self.assertEqual(r.alias_conflicts, [],
                         "出厂规则里存在冲突别名：%s" % r.alias_conflicts)


class TestGeneralization(unittest.TestCase):
    """
    留出验证：这些名字【不在】 exact_aliases 里，用来证明"规范化 + 语义层"
    确实在泛化，而不是只把 29 个已知模型的词表背了下来。
    """

    @classmethod
    def setUpClass(cls):
        cls.c = MaterialClassifier()

    def test_holdout_names(self):
        cases = {
            "Skin_Body_01": "skin",
            "Face_00": "face",
            "Cloth03": "cloth",
            "HAIR_バリエーション": "hair",
            "EyeWhite": "eye",
            "Eyelash2": "face_detail",
            "金属パーツ1": "metal",
            "ｓｋｉｎ": "skin",
            "裙_01": "cloth",
        }
        bad = []
        for nm, want in cases.items():
            got = self.c.classify(nm).klass
            if got != want:
                bad.append("%s: 期望 %s 实际 %s" % (nm, want, got))
        self.assertEqual(bad, [], "泛化失败：\n" + "\n".join(bad))


class TestWindowsPathsAndLogs(unittest.TestCase):
    """Windows 保留字符 / 报告不泄漏绝对路径"""

    @classmethod
    def setUpClass(cls):
        cls.c = MaterialClassifier()

    def test_windows_reserved_chars(self):
        for nm in ('a<b', 'a>b', 'a:b', 'a"b', 'a/b', 'a\\b', 'a|b', 'a?b', 'a*b', 'CON', 'NUL'):
            c = self.c.classify(nm)      # 不得抛异常
            self.assertIsNotNone(c.klass)

    def test_md_report_has_no_absolute_path(self):
        rec = {
            "pmx": "模型.pmx", "pmx_path": r"X:\fixtures\某模型\模型.pmx",
            "parse_ok": True, "model_name": "测试", "version": 2.0, "encoding": "UTF-16LE",
            "vertex_count": 100, "material_count": 1, "texture_count": 1,
            "fingerprint_file": "sha256:abc", "fingerprint_structure": "sha256:def",
            "map_used": None, "would_pass_strict": True,
            "summary": {"total": 1, "resolved": 1, "unresolved": 0, "low_confidence": 0,
                        "ignored": 0, "mapping_source_counts": {"exact_alias": 1}},
            "resource_issues": [], "class_options": ["skin"],
            "materials": [{"index": 0, "original_name": "颜", "surface_count": 10,
                           "alpha": 1.0, "double_sided": False, "texture": "a.png",
                           "class": "face", "layer": "L2", "rule": "颜", "confidence": 0.95,
                           "needs_review": False, "diffuse_rgb": [1, 1, 1], "note": ""}],
        }
        md = render_md(rec)
        for bad in ("X:\\", "X:/", "fixtures"):
            self.assertNotIn(bad, md, "报告泄漏了本机路径片段：%s" % bad)


class TestSidecarMap(unittest.TestCase):
    """侧车映射：路径无关（只认指纹），可跨目录移动复用"""

    def test_map_path_depends_only_on_fingerprint(self):
        p1 = map_path_for(r"X:\fixture-a", "abc123")
        p2 = map_path_for(r"Y:\fixture-b", "abc123")
        self.assertEqual(p1, p2, "映射路径不能依赖模型所在目录")

    def test_template_is_unresolved_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.material-map.json")
            write_map_template(p, [("口腔", "hint"), ("表情", "hint")],
                               {"display_name": "demo", "fingerprint": "sha256:zz"})
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            for k in ("口腔", "表情"):
                self.assertEqual(data["assignments"][k]["group"], "UNRESOLVED")

    def test_load_map_normalizes_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"schema": "toon-material-map/1", "assignments": {
                    "口腔": {"group": "Cel_Eyes", "source": "user_confirmed"},
                    "表情": "Cel_Dark",
                }}, f, ensure_ascii=False)
            a, meta = load_model_map(explicit_path=p)
            self.assertEqual(a["口腔"]["group"], "Cel_Eyes")
            self.assertEqual(a["表情"]["group"], "Cel_Dark")
            self.assertEqual(a["表情"]["source"], "user_confirmed")


class TestSummary(unittest.TestCase):
    def test_summary_counts(self):
        c = MaterialClassifier()
        names = ["身_皮肤", "颜", "表情", "Outline", "Material3"]
        cls = [c.classify(n) for n in names]
        s = MaterialClassifier.summarize(cls)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["unresolved"], 2)
        self.assertEqual(s["ignored"], 1)
        self.assertEqual(s["resolved"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
