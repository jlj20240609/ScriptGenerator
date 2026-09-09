# -*- coding: utf-8 -*-
"""E8 schema：.sgscript.json v1.0 读写/校验测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from engine import schema as S
from engine.errors import EngineError

PNG1 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n1sAAAAASUVORK5CYII="


def page(image=PNG1, size=(100, 60), ctx=None):
    return {"context": ctx or {"process": "msedge.exe", "title": "*库存*", "class": "Chrome_WidgetWin_1"},
            "image": image, "size": list(size), "rect_in_screen": [13, 13, 113, 73],
            "anchors": [], "scale_range": [0.8, 1.25],
            "capture_meta": {"dpi": 120, "ts": "2026-09-09T00:00:00.000"}}


class SchemaValidTest(unittest.TestCase):
    def test_full_script_roundtrip(self):
        """清单 §4 / 分析 §11 示例结构 + anchors 可写回并读回一致。"""
        sg = {
            "version": "1.0", "name": "自动登录", "targets_rev": 3,
            "steps": [
                {"id": "s1", "type": "action", "action": "type",
                 "target": {"page": page(), "text": "用户名", "match": "text_first"},
                 "params": {"text": "admin"}},
                {"id": "s2", "type": "action", "action": "click",
                 "target": {"image": PNG1, "text": "登录", "match": "auto",
                            "semantic": "登录按钮", "rect_in_page": [293, 49, 60, 40],
                            "center_in_page": [323, 69],
                            "uia": {"name": "登录", "automation_id": "btnLogin"}},
                 "expected_outcome": {
                     "target": {"page": page(size=(120, 80)), "text": "登录成功"},
                     "on_fail": {"strategy": "notify", "message": "用户名或密码可能不对"}}},
                {"id": "s3", "type": "condition",
                 "condition": {"target": {"text": "网络连接失败"}, "exists": True},
                 "then": [{"id": "s3a", "type": "action", "action": "notify",
                           "params": {"message": "请检查网络后点继续"}}],
                 "else": []},
                {"id": "s4", "type": "loop",
                 "loop": {"mode": "until",
                          "target": {"text": "下一页", "rect_in_page": [10, 10, 40, 20]}},
                 "body": [{"id": "s4a", "type": "action", "action": "hotkey",
                           "params": {"keys": "ctrl+s"}}]},
            ],
        }
        self.assertEqual(S.validate(sg), [])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "demo.sgscript.json"
            S.dump(sg, p)
            sg2 = S.load(p)
        self.assertEqual(sg2["steps"][1]["target"]["semantic"], "登录按钮")
        self.assertEqual(sg2["steps"][1]["target"]["uia"]["automation_id"], "btnLogin")
        self.assertEqual(sg2["targets_rev"], 3)
        self.assertEqual(sg2["steps"][3]["loop"]["mode"], "until")

    def test_anchors_spec(self):
        a = S.anchor_spec(image_dataurl=PNG1, rect_in_page=[293, 49, 356, 43],
                          stable_at="2026-09-09T00:00:00")
        self.assertEqual(a["rect_in_page"][2], 356)
        self.assertIn("stable_at", a)

    def test_page_helpers_drop_empty_optional(self):
        ps = S.page_spec(image_dataurl=PNG1, size=(100, 60))
        self.assertNotIn("rect_in_screen", ps)
        self.assertNotIn("capture_meta", ps)
        self.assertEqual(ps["scale_range"], [0.8, 1.25])
        self.assertEqual(ps["anchors"], [])

    def test_widget_target_helper(self):
        t = S.widget_target(page=page(), text="登录", rect_in_page=[1, 2, 3, 4],
                            center_in_page=[2, 4])
        self.assertEqual(t["rect_in_page"], [1, 2, 3, 4])


class SchemaInvalidTest(unittest.TestCase):
    def _problems(self, sg):
        return S.validate(sg)

    def test_bad_version(self):
        self.assertIn("version", self._problems({"version": "0.9", "steps": []})[0])

    def test_steps_required(self):
        self.assertTrue(self._problems({"version": "1.0"}))

    def test_unknown_action(self):
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "action", "action": "drag"}]}
        self.assertTrue(any("action" in p for p in self._problems(sg)))

    def test_duplicate_ids(self):
        sg = {"version": "1.0", "steps": [
            {"id": "a", "type": "action", "action": "wait", "params": {"seconds": 1}},
            {"id": "a", "type": "action", "action": "wait", "params": {"seconds": 1}}]}
        self.assertTrue(any("重复" in p for p in self._problems(sg)))

    def test_type_requires_text(self):
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "action", "action": "type",
                                           "params": {}}]}
        self.assertTrue(any("params.text" in p for p in self._problems(sg)))

    def test_target_requires_signal_or_page(self):
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "action", "action": "click",
                                           "target": {}}]}
        self.assertTrue(any("至少需要" in p for p in self._problems(sg)))

    def test_on_fail_strategy(self):
        eo = {"target": {"text": "X"}, "on_fail": {"strategy": "explode"}}
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "action", "action": "click",
                                           "target": {"text": "登录"},
                                           "expected_outcome": eo}]}
        self.assertTrue(any("on_fail.strategy" in p for p in self._problems(sg)))

    def test_loop_modes(self):
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "loop",
                                           "loop": {"mode": "while_even"}, "body": []}]}
        self.assertTrue(any("loop.mode" in p for p in self._problems(sg)))

    def test_match_pref(self):
        sg = {"version": "1.0", "steps": [{"id": "a", "type": "action", "action": "click",
                                           "target": {"text": "登录", "match": "ocr_only"}}]}
        self.assertTrue(any("target.match" in p for p in self._problems(sg)))

    def test_check_raises(self):
        with self.assertRaises(EngineError) as cm:
            S.check({"version": "1.0", "steps": []})
        self.assertEqual(cm.exception.code, "schema_invalid")
        self.assertIn("errors", cm.exception.details)

    def test_load_missing_file(self):
        with self.assertRaises(EngineError) as cm:
            S.load("nope/missing.sgscript.json")
        self.assertEqual(cm.exception.code, "script_not_found")


class SchemaSampleTest(unittest.TestCase):
    def test_analysis_example_valid(self):
        """§11 正文 JSON 示例（手工脚本，target 仅 text 无 page）应能通过校验。"""
        with open(Path(__file__).parent / "assets" / "analysis_example.sgscript.json",
                  encoding="utf-8") as f:
            sg = json.load(f)
        self.assertEqual(S.validate(sg), [])


if __name__ == "__main__":
    unittest.main()
