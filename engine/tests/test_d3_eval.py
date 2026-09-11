# -*- coding: utf-8 -*-
"""M3 D3 评测脚本自身的测试：把**判定口径**与用例集的前提钉住。

为什么值得给一个"评测脚本"写测试：
1. 口径是会被人改的。用户 2026-09-11 明确定了「答『不确定』算判错、不给保守加分」，
   日后有人觉得"模型说不知道挺负责的"就顺手加个宽松分支，准确率会悄悄虚高——
   验收数字一旦被这样污染，比没有数字更糟。
2. 用例集的**前提**也要守住：标成"含蓄"（本地一定认不出）的那些用例，如果哪天
   被换成直白文案，云端那一路的准确率就失去意义了（拿现成关键词能判的题去考语义，
   等于白考）。
"""
import unittest

from engine import outcome as O
from engine.scripts import d3_eval as E


class ScoringRuleTest(unittest.TestCase):
    """判定口径：精确相等才算对，答「不确定」算判错。"""

    def test_exact_match_is_correct(self):
        for kind in O.KINDS:
            self.assertTrue(E.is_correct(kind, kind), kind)

    def test_unknown_is_wrong(self):
        """本条就是用户定的口径，别改成"保守算对"。"""
        self.assertFalse(E.is_correct(O.KIND_PASSWORD, O.KIND_UNKNOWN))
        self.assertFalse(E.is_correct(O.KIND_NETWORK, O.KIND_UNKNOWN))
        self.assertFalse(E.is_correct(O.KIND_OK, O.KIND_UNKNOWN))

    def test_not_found_is_not_a_catch_all(self):
        """「没出现」和「不确定」都表示说不清，但按口径**都**不算对。"""
        self.assertFalse(E.is_correct(O.KIND_PASSWORD, O.KIND_NOT_FOUND))
        self.assertFalse(E.is_correct(O.KIND_NOT_FOUND, O.KIND_UNKNOWN))

    def test_near_miss_is_wrong(self):
        self.assertFalse(E.is_correct(O.KIND_PASSWORD, O.KIND_CAPTCHA))
        self.assertFalse(E.is_correct(O.KIND_NETWORK, O.KIND_NOT_FOUND))


class CaseSetPremiseTest(unittest.TestCase):
    """用例集的前提必须**可检验**，不能只靠口头标注。

    这是这一版加 `text` 字段的原因：标成"直白"的用例，本地关键词表就该判得出来；
    标成"含蓄"的，就该判不出来。前提错了，云端那一路的准确率就没意义了
    （拿现成关键词能判的题去考语义，等于白考）。
    """

    def setUp(self):
        self.cases = E.cases()

    def test_truths_are_valid_kinds(self):
        for c in self.cases:
            self.assertIn(c["truth"], O.KINDS, c["name"])
            self.assertTrue(c.get("intent"), f"{c['name']} 缺少「这一步想做什么」")

    def test_every_kind_is_covered(self):
        got = {c["truth"] for c in self.cases}
        for kind in (O.KIND_OK, O.KIND_PASSWORD, O.KIND_CAPTCHA, O.KIND_NETWORK,
                     O.KIND_NOT_FOUND):
            self.assertIn(kind, got, f"用例集缺少这一类：{kind}")

    def test_direct_cases_really_are_decidable_by_keywords(self):
        """标成直白的用例：本地关键词表必须判得出**正确**的那一类。"""
        for c in self.cases:
            if not c["local_text"]:
                continue
            got = O.kind_from_text([c["text"]])
            self.assertEqual(got, c["truth"],
                             f"{c['name']} 标了 local_text=True，"
                             f"但关键词表判出 {got!r}（屏幕上写着：{c['text']!r}）")

    def test_implicit_cases_are_not_decidable_by_keywords(self):
        """标成含蓄的用例：关键词表必须**判不出来**，否则它不是含蓄用例。"""
        for c in self.cases:
            if not c["implicit"]:
                continue
            got = O.kind_from_text([c["text"]])
            self.assertEqual(got, "",
                             f"{c['name']} 标了含蓄，但关键词表已能判出 {got!r}——"
                             f"这条用例考不出语义能力")

    def test_implicit_cases_are_a_real_semantic_challenge(self):
        """含蓄用例的答案不该是「没出现」那个兜底答案（那是白送分）。"""
        implicit = [c for c in self.cases if c["implicit"]]
        self.assertGreaterEqual(len(implicit), 5, "含蓄用例太少，D3 的价值测不出来")
        for c in implicit:
            self.assertNotEqual(c["truth"], O.KIND_NOT_FOUND, c["name"])

    def test_both_direct_and_implicit_cases_exist(self):
        direct = [c for c in self.cases if c["local_text"]]
        implicit = [c for c in self.cases if c["implicit"]]
        self.assertGreaterEqual(len(direct), 3, "直白用例（测本地预判）太少")
        self.assertGreaterEqual(len(implicit), 5, "含蓄用例（测语义判定）太少")

    def test_all_cases_have_images(self):
        for c in self.cases:
            self.assertIsNotNone(c["img"], c["name"])
            self.assertGreater(c["img"].shape[0], 100, c["name"])


if __name__ == "__main__":
    unittest.main()
