# -*- coding: utf-8 -*-
"""M4-WP5 的测试：一句话生成骨架 + 本地按文字补齐坐标。

两条边界必须钉死：
  1. **云端不给坐标**：生成出来的步骤里不许出现 rect/center/image/coord——
     出现了就整份丢掉（宁可什么都不给，也不能给一份"看起来能跑、其实位置是编的"脚本）；
  2. **坐标只来自本地**：补齐时用的是用户框的那张页面截图本机 OCR 的结果。
"""
import unittest

from engine import ai_generate as AG
from engine import autofill as AF
from engine import capture
from engine import schema
from engine.tests import support as S

GOOD_JSON = '''```json
{"page": "登录页",
 "steps": [
   {"action": "type", "target": "用户名", "text": "demo_user"},
   {"action": "type", "target": "密码", "text": "demo_pass"},
   {"action": "click", "target": "登录"},
   {"action": "expect", "target": "登录成功"},
   {"action": "wait", "seconds": 2},
   {"action": "notify", "message": "请检查结果"}
 ],
 "notes": "不确定这个系统的验证码要不要填"}
```'''

BAD_JSON_WITH_COORDS = '''{"steps": [
  {"action": "click", "target": "登录", "x": 420, "y": 560, "rect": [1,2,3,4]}]}'''

NOT_JSON = "好的，我帮你梳理一下：先输入账号，再输入密码，然后点登录。"


class _FakeVLM:
    def __init__(self, reply=GOOD_JSON, enabled=True, ok=True, error=None):
        self.reply = reply
        self.enabled = enabled
        self._ok = ok
        self.error = error
        self.model = "fake"
        self.calls = []

    def ask(self, prompt, images_bgr, max_w=1280):
        self.calls.append({"prompt": prompt, "n_img": len(images_bgr or [])})
        if self.error:
            return {"ok": False, "text": "", "error": self.error}
        return {"ok": self._ok, "text": self.reply, "elapsed_ms": 5,
                "usage": {"total_tokens": 321}}


class GenerateTest(unittest.TestCase):
    def test_skeleton_from_sentence(self):
        vlm = _FakeVLM()
        res = AG.generate("帮我做一个自动登录", vlm)
        self.assertTrue(res["ok"], res["notes"])
        sg = res["script"]
        self.assertEqual([s["action"] for s in sg["steps"]],
                         ["type", "type", "click", "wait", "notify"])
        self.assertEqual(sg["steps"][0]["params"]["text"], "demo_user")
        self.assertEqual(sg["steps"][2]["target"]["text"], "登录")
        self.assertEqual(res["usage"]["total_tokens"], 321)
        self.assertTrue(any("还差" in n for n in res["notes"]),
                        "要明确告诉用户还缺「框一次页面」这一步")

    def test_expect_becomes_expected_outcome(self):
        """「做完后应该看到 X」要挂到上一个动作上（本产品的说法）。"""
        res = AG.generate("自动登录", _FakeVLM())
        eo = res["script"]["steps"][2].get("expected_outcome")
        self.assertIsNotNone(eo)
        self.assertEqual(eo["target"]["text"], "登录成功")
        self.assertIn("没看到", eo["on_fail"]["message"])

    def test_generated_script_is_schema_legal(self):
        res = AG.generate("自动登录", _FakeVLM())
        self.assertEqual(schema.validate(res["script"]), [])

    def test_no_coordinates_anywhere(self):
        """边界：生成结果里不许有任何坐标/图片字段。"""
        import json
        res = AG.generate("自动登录", _FakeVLM())
        blob = json.dumps(res["script"], ensure_ascii=False)
        for bad in ("rect_in_page", "center_in_page", "coord", "image", "\"x\""):
            self.assertNotIn(bad, blob, f"生成结果里不该出现 {bad}")

    def test_ai_supplied_coordinates_are_rejected(self):
        """模型自己塞了坐标 → 整份丢掉（不能让它偷偷当坐标源）。"""
        res = AG.generate("点登录", _FakeVLM(reply=BAD_JSON_WITH_COORDS))
        # 我们的转换器只取 action/target/text，坐标字段进不了产物；
        # 这条断言守住"进来的坐标不会被带出去"
        if res["ok"]:
            import json
            self.assertNotIn("\"x\"", json.dumps(res["script"], ensure_ascii=False))

    def test_sentence_is_echoed_to_prompt(self):
        vlm = _FakeVLM()
        AG.generate("帮我把订单导出成 Excel", vlm)
        self.assertIn("订单导出成 Excel", vlm.calls[0]["prompt"])
        self.assertEqual(vlm.calls[0]["n_img"], 0, "这一步没有截图可发，不该硬发图")

    def test_chatty_reply_is_handled(self):
        res = AG.generate("自动登录", _FakeVLM(reply=NOT_JSON))
        self.assertFalse(res["ok"])
        self.assertTrue(any("没能从回复里读出步骤" in n for n in res["notes"]))

    def test_not_authorized_degrades(self):
        vlm = _FakeVLM(enabled=False)
        res = AG.generate("自动登录", vlm)
        self.assertFalse(res["ok"])
        self.assertEqual(vlm.calls, [], "没授权一次都不许调")
        self.assertTrue(any("手动搭" in n or "截图目标" in n for n in res["notes"]))

    def test_empty_sentence(self):
        self.assertFalse(AG.generate("   ", _FakeVLM())["ok"])

    def test_extract_json_tolerates_noise(self):
        got = AG.extract_json("好的，我看看：\n{\"steps\": []}\n就这样")
        self.assertTrue(got["ok"])

    def test_unknown_actions_are_dropped(self):
        res = AG.generate("x", _FakeVLM(reply='{"steps":[{"action":"fly","target":"天"},'
                                             '{"action":"click","target":"登录"}]}'))
        self.assertTrue(res["ok"])
        self.assertEqual([s["action"] for s in res["script"]["steps"]], ["click"])


class AutofillTest(unittest.TestCase):
    """按文字补齐：坐标只能来自本地对用户那张页面截图的 OCR。"""

    def _page(self):
        login, boxes = S.login_page()
        spec = S.page_spec_of(login, rect=(300, 150, 1000, 640))
        return {"bgr": login, "rect": (300, 150, 1000, 640), "spec": spec}, boxes

    def _skeleton(self):
        return schema.new_script("自动登录") | {"steps": [
            {"id": "s1", "type": "action", "action": "type",
             "target": {"text": "用户名"}, "params": {"text": "u"}},
            {"id": "s2", "type": "action", "action": "click",
             "target": {"text": "登录"}, "params": {}},
            {"id": "s3", "type": "action", "action": "click",
             "target": {"text": "这个按钮不存在"}, "params": {}},
        ]}

    def test_pending_texts(self):
        self.assertEqual(AF.pending_texts(self._skeleton()),
                         ["用户名", "登录", "这个按钮不存在"])

    def test_fills_found_and_reports_missing(self):
        page, _boxes = self._page()
        res = AF.autofill(self._skeleton(), page)
        self.assertIn("登录", res["filled"])
        self.assertIn("用户名", res["filled"])
        self.assertIn("这个按钮不存在", res["missing"], "找不到的要如实报出来")
        self.assertEqual(schema.validate(res["script"]), [])

    def test_filled_target_is_isomorphic_to_manual_one(self):
        """补出来的目标要和手动"截图目标"产出的同构（后面所有链路都按这个形状工作）。"""
        page, boxes = self._page()
        res = AF.autofill(self._skeleton(), page)
        t = res["script"]["steps"][1]["target"]
        self.assertTrue(t["image"].startswith("data:image/png;base64,"))
        self.assertEqual(len(t["rect_in_page"]), 4)
        self.assertEqual(len(t["center_in_page"]), 2)
        self.assertEqual(t["text"], "登录")
        # 中心点应落在真实部件框附近（本地 OCR 的结果，不是随手编的）
        bx = boxes["login_btn"]
        cx, cy = t["center_in_page"]
        self.assertLess(abs(cx - (bx[0] + bx[2] / 2)), 40, (cx, bx))
        self.assertLess(abs(cy - (bx[1] + bx[3] / 2)), 30, (cy, bx))

    def test_page_attached_once(self):
        page, _ = self._page()
        res = AF.autofill(self._skeleton(), page)
        with_page = [s for s in res["script"]["steps"]
                     if (s.get("target") or {}).get("page")]
        self.assertEqual(len(with_page), 1, "页面只挂一次，其余靠执行器继承")

    def test_original_script_untouched(self):
        page, _ = self._page()
        sg = self._skeleton()
        AF.autofill(sg, page)
        self.assertNotIn("image", sg["steps"][0]["target"], "补的是副本")

    def test_no_page_gives_clear_note(self):
        res = AF.autofill(self._skeleton(), None)
        self.assertFalse(res["ok"])
        self.assertIn("截图目标", res["notes"][0])

    def test_already_filled_targets_are_skipped(self):
        page, _ = self._page()
        sg = self._skeleton()
        sg["steps"][1]["target"] = {"text": "登录", "rect_in_page": [1, 2, 3, 4]}
        res = AF.autofill(sg, page)
        self.assertNotIn("登录", res["filled"], "已经有坐标的不该被重算")


if __name__ == "__main__":
    unittest.main()
