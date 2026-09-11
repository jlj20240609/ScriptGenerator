# -*- coding: utf-8 -*-
"""M3-WP3 结果语义判定 D3 的离线测试：分层、本地优先、降级不崩。

这里钉住的都是"错了会很难查"的行为：
  * 本地 D2 说成功时，**一次云端请求都不许发**（本地优先不是口号，要能测出来）
  * 云端没授权/超时/回一堆废话时，必须降级成"交给人看"，不能假装判出来了
  * D3 的输出里**不许有坐标**（架构边界：云端永不作为运行时坐标源）
"""
import unittest

from engine import outcome as O


class _FakeVLM:
    """假云端：记下被问了几次、问的是什么，按脚本回答。"""

    def __init__(self, reply="密码错", enabled=True, ok=True, error=None, delay_ms=20):
        self.reply = reply
        self.enabled = enabled
        self._ok = ok
        self.error = error
        self.delay_ms = delay_ms
        self.calls = []

    def ask(self, prompt, images_bgr, max_w=None):
        self.calls.append({"prompt": prompt, "n_img": len(images_bgr or [])})
        if self.error:
            return {"ok": False, "text": "", "elapsed_ms": self.delay_ms,
                    "error": self.error}
        return {"ok": self._ok, "text": self.reply, "elapsed_ms": self.delay_ms}


def _no_ocr(_img):
    return []


class ParseReplyTest(unittest.TestCase):
    def test_chinese_labels(self):
        for text, kind in [("密码错", O.KIND_PASSWORD), ("密码错误", O.KIND_PASSWORD),
                           ("验证码错", O.KIND_CAPTCHA), ("断网", O.KIND_NETWORK),
                           ("成功", O.KIND_OK), ("没出现", O.KIND_NOT_FOUND),
                           ("不确定", O.KIND_UNKNOWN)]:
            self.assertEqual(O.parse_kind_reply(text)["kind"], kind, text)

    def test_english_and_noise(self):
        self.assertEqual(O.parse_kind_reply("password_wrong")["kind"], O.KIND_PASSWORD)
        self.assertEqual(O.parse_kind_reply("  No_Network.\n")["kind"], O.KIND_NETWORK)
        self.assertEqual(O.parse_kind_reply('"验证码错误"')["kind"], O.KIND_CAPTCHA)

    def test_json_wrapped(self):
        r = O.parse_kind_reply('{"kind": "密码错"}')
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], O.KIND_PASSWORD)

    def test_longest_match_wins(self):
        """回复里夹了别的话时，按最长匹配取，别被短词带偏。"""
        r = O.parse_kind_reply("结论是：网络连接失败导致")
        self.assertEqual(r["kind"], O.KIND_NETWORK)

    def test_unparsable(self):
        self.assertFalse(O.parse_kind_reply("今天天气不错")["ok"])
        self.assertEqual(O.parse_kind_reply("")["reason"], "empty")


class KindFromTextTest(unittest.TestCase):
    """本地文字预判：屏幕上有现成的话就别去问云端。"""

    def test_common_messages(self):
        cases = [("用户名或密码错误", O.KIND_PASSWORD),
                 ("密码不正确，请重试", O.KIND_PASSWORD),
                 ("验证码已失效，请重新获取", O.KIND_CAPTCHA),
                 ("网络连接失败，请检查网络", O.KIND_NETWORK),
                 ("请求超时，请稍后重试", O.KIND_NETWORK),
                 ("登录成功，欢迎回来", O.KIND_OK)]
        for text, want in cases:
            self.assertEqual(O.kind_from_text([text]), want, text)

    def test_captcha_before_password(self):
        """一页上同时出现验证码与凭据提示时，认更具体的那个。"""
        got = O.kind_from_text(["验证码错误", "用户名或密码错误"])
        self.assertEqual(got, O.KIND_CAPTCHA)

    def test_unknown_text(self):
        self.assertEqual(O.kind_from_text(["系统维护中"]), "")
        self.assertEqual(O.kind_from_text([]), "")


class JudgeLayeringTest(unittest.TestCase):
    """三层：本地 D2 → 本地文字 → 云端。每一层都要有明确的不越级行为。"""

    def test_local_ok_never_calls_cloud(self):
        vlm = _FakeVLM()
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(None, "登录", local=O.local_verdict(True))
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], O.KIND_OK)
        self.assertEqual(out["source"], "local")
        self.assertEqual(vlm.calls, [], "本地已确认成功时一次云端请求都不许发")

    def test_text_first_avoids_cloud(self):
        vlm = _FakeVLM()
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=lambda img: ["用户名或密码错误"])
        self.assertEqual(out["kind"], O.KIND_PASSWORD)
        self.assertEqual(out["source"], "local_text")
        self.assertEqual(vlm.calls, [], "屏幕文字已说明原因时不该问云端")

    def test_falls_back_to_cloud(self):
        vlm = _FakeVLM(reply="断网")
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=_no_ocr)
        self.assertEqual(out["kind"], O.KIND_NETWORK)
        self.assertEqual(out["source"], "ai")
        self.assertEqual(len(vlm.calls), 1, "只问一次")
        self.assertEqual(vlm.calls[0]["n_img"], 1, "只发一张当前屏幕截图")
        self.assertIn("登录", vlm.calls[0]["prompt"], "要把这一步的意图告诉模型")

    def test_text_first_can_be_turned_off(self):
        vlm = _FakeVLM(reply="断网")
        j = O.OutcomeJudge(vlm=vlm, text_first=False)
        out = j.judge(object(), "登录", local=O.local_verdict(False),
                      ocr_fn=lambda img: ["用户名或密码错误"])
        self.assertEqual(out["source"], "ai", "关掉预判后一律走云端")
        self.assertEqual(out["kind"], O.KIND_NETWORK)

    def test_not_authorized_degrades(self):
        vlm = _FakeVLM(enabled=False)
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=_no_ocr)
        self.assertFalse(out["ok"])
        self.assertEqual(out["kind"], O.KIND_NOT_FOUND)
        self.assertEqual(vlm.calls, [], "没授权就一次都不许问")
        self.assertIn("云端不可用", out["note"])

    def test_no_vlm_at_all(self):
        j = O.OutcomeJudge(vlm=None)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=_no_ocr)
        self.assertEqual(out["source"], "local")
        self.assertFalse(out["ok"])

    def test_cloud_failure_degrades_not_crash(self):
        vlm = _FakeVLM(error="URLError(timeout)")
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=_no_ocr)
        self.assertEqual(out["kind"], O.KIND_UNKNOWN)
        self.assertIn("云端判定失败", out["note"])

    def test_unparsable_reply_degrades(self):
        vlm = _FakeVLM(reply="我看了半天也说不准")
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "timeout"),
                      ocr_fn=_no_ocr)
        self.assertEqual(out["kind"], O.KIND_UNKNOWN)
        self.assertIn("无法解析", out["note"])

    def test_reply_prefers_ai_latency(self):
        vlm = _FakeVLM(reply="成功", delay_ms=2500)
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False, "page_gone"),
                      ocr_fn=_no_ocr)
        self.assertEqual(out["kind"], O.KIND_OK)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["elapsed_ms"], 2500, "耗时用云端回报的真实值")

    def test_no_ocr_crash_is_swallowed(self):
        def boom(_img):
            raise RuntimeError("OCR 挂了")

        vlm = _FakeVLM(reply="密码错")
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False), ocr_fn=boom)
        self.assertEqual(out["kind"], O.KIND_PASSWORD, "OCR 挂了不该让判定整体失败")


class ArchitectureBoundaryTest(unittest.TestCase):
    """架构边界：D3 只出语义标签，绝不出坐标。"""

    def test_verdict_has_no_coordinates(self):
        vlm = _FakeVLM(reply="密码错")
        j = O.OutcomeJudge(vlm=vlm)
        out = j.judge(object(), "登录", local=O.local_verdict(False), ocr_fn=_no_ocr)
        for key in ("xy", "point", "rect", "box", "center", "norm", "coords"):
            self.assertNotIn(key, out, f"D3 的判定结果里不许出现坐标字段：{key}")

    def test_prompt_never_asks_for_coordinates(self):
        p = O.OUTCOME_PROMPT.format(intent="登录")
        for word in ("坐标", "位置", "像素", "框"):
            self.assertNotIn(word, p, f"提示词里不该要求模型给{word}")

    def test_labels_are_the_documented_set(self):
        self.assertEqual(set(O.KINDS),
                         {"ok", "password_wrong", "captcha_wrong", "no_network",
                          "not_found", "unknown"})


if __name__ == "__main__":
    unittest.main()
