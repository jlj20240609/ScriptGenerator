# -*- coding: utf-8 -*-
"""M4-WP4 AI 代码转译的测试：产物形状、引用完整性拦截、单向标注、降级。

注什么重要：转译的正确性（引用必须存在、必须标注单向导出、没授权要能降级）
全是**确定性逻辑**，不该被网络和模型波动挡住。所以这里全部注入假模型离线测，
真机那一路只负责回答"模型给的代码好不好看"。
"""
import unittest

from engine import ai_export as AX
from engine.tests.test_exporter import _login_script

GOOD_CODE = '''```python
"""自动登录的源码版。"""

def run(username="admin", password="pass123"):
    type_text("用户名", username)      # 1. 在用户名框里输入账号
    type_text("密码", password)        # 2. 在密码框里输入密码
    click("登录")                      # 3. 点登录
    verify_result("登录成功")          # 4. 校验有没有登录成功
```'''

BAD_CODE = '''```python
def run():
    click("登录按纽")   # 名字编错了（脚本里没有这个部件）
```'''

CRASH_CODE = "这不是代码，只是一段解释。" * 3


class _FakeVLM:
    def __init__(self, reply=GOOD_CODE, enabled=True, ok=True, error=None, model="fake-vlm"):
        self.reply = reply
        self.enabled = enabled
        self._ok = ok
        self.error = error
        self.model = model
        self.calls = []

    def ask(self, prompt, images_bgr, max_w=1280):
        self.calls.append({"prompt": prompt, "n_img": len(images_bgr or [])})
        if self.error:
            return {"ok": False, "text": "", "error": self.error}
        return {"ok": self._ok, "text": self.reply, "elapsed_ms": 12,
                "usage": {"total_tokens": 4242}}


class ExtractCodeTest(unittest.TestCase):
    def test_fenced_block(self):
        got = AX.extract_code(GOOD_CODE)
        self.assertTrue(got["ok"])
        self.assertIn("def run(", got["code"])
        self.assertNotIn("```", got["code"])

    def test_bare_code(self):
        got = AX.extract_code("def run():\n    pass\n")
        self.assertTrue(got["ok"])

    def test_not_code(self):
        self.assertFalse(AX.extract_code("我觉得这个脚本挺简单的")["ok"])
        self.assertEqual(AX.extract_code("")["reason"], "empty")


class TranslateTest(unittest.TestCase):
    def test_good_translation(self):
        vlm = _FakeVLM()
        res = AX.translate(_login_script(), vlm)
        self.assertTrue(res["ok"], res["notes"])
        self.assertEqual(len(res["files"]), 1)
        path, src = next(iter(res["files"].items()))
        self.assertTrue(path.startswith("scripts/"))
        self.assertIn("def run(", src)
        self.assertIn("单向导出", src, "必须标注单向导出（DoD 第 3 条）")
        self.assertIn("修改脚本后请重新导出", src)
        self.assertIn("坐标仍由本机引擎", src, "要写明坐标不出自这份代码")
        self.assertEqual(res["usage"]["total_tokens"], 4242, "用量要带出来（成本口径）")

    def test_prompt_carries_steps_widgets_and_names(self):
        vlm = _FakeVLM()
        AX.translate(_login_script(), vlm)
        p = vlm.calls[0]["prompt"]
        self.assertIn("用户名", p)
        self.assertIn("登录", p)
        self.assertIn("click", p, "要告诉模型能用哪些原语")
        self.assertIn("type_text", p)
        self.assertGreaterEqual(vlm.calls[0]["n_img"], 1, "要把截图发给它看")

    def test_hallucinated_widget_is_blocked(self):
        """AI 编了一个不存在的部件 → 导出前拦下（DoD：转译产物通过引用完整性校验）。"""
        res = AX.translate(_login_script(), _FakeVLM(reply=BAD_CODE))
        self.assertFalse(res["ok"], res)
        self.assertTrue(any("不存在的部件" in p for p in res["problems"]), res["problems"])
        self.assertFalse(res["files"], "拦下时不该给出产物")

    def test_unusable_reply_is_reported(self):
        res = AX.translate(_login_script(), _FakeVLM(reply=CRASH_CODE))
        self.assertFalse(res["ok"])
        self.assertTrue(any("没有给出可用的源码" in n for n in res["notes"]), res["notes"])

    def test_not_authorized_degrades(self):
        vlm = _FakeVLM(enabled=False)
        res = AX.translate(_login_script(), vlm)
        self.assertFalse(res["ok"])
        self.assertEqual(vlm.calls, [], "没授权就一次都不许上传")
        self.assertTrue(any("出口 ①" in n for n in res["notes"]), res["notes"])

    def test_cloud_error_degrades(self):
        res = AX.translate(_login_script(), _FakeVLM(error="URLError(timeout)"))
        self.assertFalse(res["ok"])
        self.assertTrue(any("云端调用失败" in n for n in res["notes"]))

    def test_no_vlm(self):
        res = AX.translate(_login_script(), None)
        self.assertFalse(res["ok"])
        self.assertTrue(any("没有接通" in n for n in res["notes"]))

    def test_invalid_script_is_refused(self):
        bad = {"version": "1.0", "name": "x", "steps": [{"id": "s", "type": "action",
                                                        "action": "飞"}]}
        res = AX.translate(bad, _FakeVLM())
        self.assertFalse(res["ok"])

    def test_images_are_bounded(self):
        """截图要限量：一次把几十张图发上去既慢又贵。"""
        vlm = _FakeVLM()
        AX.translate(_login_script(), vlm, {"max_images": 2})
        self.assertLessEqual(vlm.calls[0]["n_img"], 3, "页面图最多再加一张")


if __name__ == "__main__":
    unittest.main()
