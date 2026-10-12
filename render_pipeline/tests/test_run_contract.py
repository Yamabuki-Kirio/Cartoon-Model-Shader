# -*- coding: utf-8 -*-
"""
终态契约回归测试：**诊断结果绝不能进入成品流程**
=====================================================================================
这是「不会错着成功」的最后一道闸门，必须长期可回归。

覆盖两条通道：
  1. --done 文件里的大写 token
  2. run_manifest.json / run_result.json 里的 outcome + shippable
以及契约模块本身的一致性。
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_V3 = os.path.dirname(_HERE)
sys.path.insert(0, _V3)

import run_contract as RC  # noqa: E402


class TestOutcomeContract(unittest.TestCase):

    def test_only_success_is_shippable(self):
        self.assertTrue(RC.is_shippable(RC.OUTCOME_SUCCESS))
        for bad in (RC.OUTCOME_DIAGNOSTIC, RC.OUTCOME_REJECTED, RC.OUTCOME_FAILED):
            self.assertFalse(RC.is_shippable(bad), bad)

    def test_shippable_whitelist_has_exactly_one(self):
        self.assertEqual(list(RC.SHIPPABLE_OUTCOMES), [RC.OUTCOME_SUCCESS])

    def test_unknown_value_is_not_shippable(self):
        for junk in (None, "", "OK", "ok success", "SUCCESS!", "passed", "已完成", 0, [], {}):
            self.assertFalse(RC.is_shippable(junk), repr(junk))

    def test_assert_shippable_raises_for_non_success(self):
        self.assertTrue(RC.assert_shippable(RC.OUTCOME_SUCCESS))
        for bad in (RC.OUTCOME_DIAGNOSTIC, RC.OUTCOME_REJECTED, RC.OUTCOME_FAILED, "weird"):
            with self.assertRaises(RuntimeError):
                RC.assert_shippable(bad)

    def test_done_token_never_starts_with_OK(self):
        """历史启动器常用「是否以 OK 开头」判断成败。
        诊断结果绝不能写成 OK 前缀，否则会被误当成正式成功。"""
        for o in RC.OUTCOME_SET:
            tok = RC.done_token(o)
            self.assertFalse(tok.startswith("OK"), "%s -> %s" % (o, tok))
            self.assertEqual(tok, tok.upper())

    def test_done_tokens_are_the_four_expected(self):
        self.assertEqual(RC.done_token(RC.OUTCOME_SUCCESS), "SUCCESS")
        self.assertEqual(RC.done_token(RC.OUTCOME_REJECTED), "REJECTED")
        self.assertEqual(RC.done_token(RC.OUTCOME_DIAGNOSTIC), "DIAGNOSTIC")
        self.assertEqual(RC.done_token(RC.OUTCOME_FAILED), "FAILED")
        self.assertEqual(RC.done_token("garbage"), "FAILED")

    def test_result_payload_shape(self):
        p = RC.result_payload(RC.OUTCOME_DIAGNOSTIC, {"extra": 1})
        self.assertEqual(p["schema"], "toon-run-result/1")
        self.assertEqual(p["outcome"], "diagnostic")
        self.assertEqual(p["done_token"], "DIAGNOSTIC")
        self.assertFalse(p["shippable"])
        self.assertEqual(p["extra"], 1)

    def test_normalize_maps_junk_to_failed(self):
        self.assertEqual(RC.normalize("nonsense"), RC.OUTCOME_FAILED)
        self.assertEqual(RC.normalize("SUCCESS"), RC.OUTCOME_SUCCESS)


class TestPipelineSourceGuard(unittest.TestCase):
    """源码级守卫：主脚本与驱动里不允许再出现裸写的 OK 前缀终态。"""

    def _read(self, name):
        with open(os.path.join(_V3, name), encoding="utf-8") as f:
            return f.read()

    def test_driver_uses_contract(self):
        s = self._read("一键渲染_通用驱动.py")
        self.assertIn("import run_contract as RC", s)
        self.assertIn("write_result(", s)
        self.assertNotIn('f.write("OK ', s, "驱动里仍有裸写 OK 前缀的终态")
        self.assertNotIn('f.write("ERROR', s, "驱动里仍有旧的 ERROR 终态")

    def test_main_script_uses_contract(self):
        s = self._read("一键卡通渲染.py")
        self.assertIn("import run_contract as RC", s)
        self.assertNotIn('RUN_OUTCOME = "success"', s, "主脚本里仍有硬编码终态字符串")
        self.assertNotIn('RUN_OUTCOME = "diagnostic"', s)

    def test_no_silent_cel_dark_fallback(self):
        """静默兜底到 Cel_Dark 的行为必须彻底消失。"""
        s = self._read("一键卡通渲染.py")
        self.assertNotIn("已按 STRICT_MATERIAL_MATCH=False 兜底到", s)
        self.assertIn("OUTCOME_REJECTED", s)


class TestRealArtifacts(unittest.TestCase):
    """
    对端到端产物做断言（若存在）。
    这是最贴近真实的一条：诊断模式跑出来的东西，绝不能是 shippable。
    """

    def _find_runs(self):
        base = os.path.join(_V3, "_e2e")
        if not os.path.isdir(base):
            return []
        out = []
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d, "run_result.json")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        out.append((d, json.load(f)))
                except Exception:
                    pass
        return out

    def test_diagnostic_run_is_not_shippable(self):
        runs = self._find_runs()
        if not runs:
            self.skipTest("尚无 run_result.json 产物（先跑一次端到端）")
        diag = [r for r in runs if r[1].get("outcome") == "diagnostic"]
        for name, pay in diag:
            self.assertFalse(pay.get("shippable"), "%s 的诊断产物被标成可交付" % name)
            self.assertEqual(pay.get("done_token"), "DIAGNOSTIC")

    def test_rejected_run_has_no_output_image(self):
        base = os.path.join(_V3, "_e2e")
        if not os.path.isdir(base):
            self.skipTest("尚无端到端产物")
        checked = 0
        for d in sorted(os.listdir(base)):
            rr = os.path.join(base, d, "run_result.json")
            if not os.path.isfile(rr):
                continue
            with open(rr, encoding="utf-8") as f:
                pay = json.load(f)
            if pay.get("outcome") != "rejected":
                continue
            checked += 1
            self.assertFalse(os.path.exists(os.path.join(base, d, "一键渲染.png")),
                             "%s：被拒绝却出了成品 PNG" % d)
            self.assertFalse(os.path.exists(os.path.join(base, d, "一键卡通渲染_成品.blend")),
                             "%s：被拒绝却保存了成品 .blend" % d)
        if checked == 0:
            self.skipTest("尚无 rejected 产物")

    def test_every_artifact_outcome_is_known(self):
        for name, pay in self._find_runs():
            self.assertIn(pay.get("outcome"), RC.OUTCOME_SET, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
