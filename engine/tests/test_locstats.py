# -*- coding: utf-8 -*-
"""locstats 单测：口径、聚合、失败原因提炼。

为什么钉住这些：识别率是靠这些数字下判断的。只要口径一漂（比如把第 3 层兜底也算成"认出
来了"），后面所有优化决策都会跟着漂 —— 所以三个口径各有一条断言守着。
"""
import unittest

from engine import locstats as L


def row(step="s1", event="locate_widget", method="ocr_text", ok=True, level=2,
        conf=1.0, ms=700, **kw):
    r = {"ts": "2026-09-12T11:47:45.000", "step_id": step, "event": event,
         "method": method, "ok": ok, "confidence": conf, "elapsed_ms": ms}
    if level is not None:
        r["level"] = level
    r.update(kw)
    return r


class LevelTest(unittest.TestCase):
    def test_level_mapping(self):
        self.assertEqual(L.level_of("uia"), "l1")
        self.assertEqual(L.level_of("ocr_text"), "l2")
        self.assertEqual(L.level_of("tpl"), "l2")
        self.assertEqual(L.level_of("tpl_ring"), "l2")
        self.assertEqual(L.level_of("page_coord"), "l3")
        self.assertEqual(L.level_of("page_tpl"), "")     # 页面定位不参与部件三级口径
        self.assertEqual(L.level_of(None), "")


class AnalyzeTest(unittest.TestCase):
    """一份"跨程序脚本失败"的合成日志：s1 靠第 3 层兜底勉强成功，s3 页面定位彻底失败。"""

    def _rows(self):
        return [
            row("s1", "locate_page", "feature", True, None, 0.9, 250, page_ok=True),
            row("s1", "locate_widget", "page_coord", True, 3, 0.5, 5,
                why={"reason": "no_uia", "note": "UI 树里没有这个东西"}),
            row("s1", "click_guard", "tpl_fallback", True, None, 0.66, 60),
            row("s3", "locate_page", "page_tpl", False, None, 0.0, 200, page_ok=False,
                why={"reason": "page_template_miss"}),
            row("s3", "procedural_fail", "ai", True, None, 0.0, None, reason="page_not_found"),
            row("s3", "locate_page", "page_tpl", False, None, 0.0, 210, page_ok=False),
            row("s3", "procedural_fail", "ai", True, None, 0.0, None, reason="page_not_found"),
        ]

    def test_hit_rate_and_blind_rate(self):
        w = L.analyze(self._rows())["overall"]["widget"]
        self.assertEqual(w["attempts"], 1)
        self.assertEqual(w["hit_rate"], 100.0)
        self.assertEqual(w["blind"], 1)
        self.assertEqual(w["blind_rate"], 100.0, "第 3 层兜底必须算成盲点")

    def test_page_hit_rate(self):
        p = L.analyze(self._rows())["overall"]["page"]
        self.assertEqual(p["attempts"], 3)
        self.assertEqual(p["ok"], 1)
        self.assertEqual(p["hit_rate"], 33.3)

    def test_fail_reasons_aggregated(self):
        rep = L.analyze(self._rows())
        self.assertEqual(rep["overall"]["fail_reasons"].get("page_not_found"), 2)
        self.assertEqual(rep["steps"]["s3"]["fails"].get("page_not_found"), 2)

    def test_why_reasons_aggregated(self):
        o = L.analyze(self._rows())["overall"]
        self.assertEqual(o["why_reasons"].get("no_uia"), 1)
        self.assertEqual(o["why_reasons"].get("page_template_miss"), 1)

    def test_render_smoke(self):
        txt = L.render_text(L.analyze(self._rows()))
        self.assertIn("一次命中率", txt)
        self.assertIn("盲点率", txt)
        md = L.render_markdown(L.analyze(self._rows()))
        self.assertIn("| 步骤 |", md)


class SummarizeTest(unittest.TestCase):
    def test_summarize_nearby_reject(self):
        det = {
            "l1": {"ok": False, "n_hits": 0},
            "l2_ocr": {"ok": False, "confidence": 0.0,
                       "top3": [[[10, 20, 30, 40], 0.78, 12, False]],
                       "nearby_rejected": {"候选": [10, 20, 30, 40], "对的上的": 0}},
            "l2_tpl_nearby_rejected": True,
            "l3": {"verified": True, "verify_method": "nearby", "nearby_ok": True},
        }
        w = L.summarize_detail(det)
        self.assertEqual(w["reason"], "nearby_rejected")
        self.assertIn("旁边", w["note"])
        self.assertIn("l2_tpl_nearby_rejected", w["rejects"])
        self.assertEqual(w["l2_ocr"]["cands"], 1)
        self.assertTrue(w["l3"]["verified"])

    def test_summarize_bounded_and_never_raises(self):
        w = L.summarize_detail({"l2_ocr": {"top3": [[[0, 0, 1, 1], 0.9, 1, True]] * 9}})
        self.assertLessEqual(len(w["l2_ocr"]["top3"]), 3, "候选最多留 3 个，日志不能膨胀")
        for junk in (None, 123, "x", {}, {"l2_ocr": None}):
            self.assertIsInstance(L.summarize_detail(junk), dict)

    def test_summarize_no_text(self):
        w = L.summarize_detail({"l2_ocr": {"ok": False, "confidence": 0.0}})
        self.assertEqual(w["reason"], "no_text_found")

    def test_summarize_out_of_page(self):
        w = L.summarize_detail({"l2_tpl_out_of_page": [1, 2, 3, 4]})
        self.assertEqual(w["reason"], "out_of_page")


if __name__ == "__main__":
    unittest.main()
