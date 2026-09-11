# -*- coding: utf-8 -*-
"""E7 ai：归一化提示解析 / 授权闸 / 云端适配不可用时行为 / 语义桩。"""
import unittest

from engine import ai as A
from engine.errors import EngineError
from engine.tests import support as S


class ParseTest(unittest.TestCase):
    def test_ok_norm(self):
        r = A.parse_norm_reply("0.30 0.42")
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["xy"][0], 0.30)
        self.assertAlmostEqual(r["xy"][1], 0.42)

    def test_chinese_comma(self):
        r = A.parse_norm_reply("0.3，0.4")
        self.assertTrue(r["ok"])
        self.assertEqual(r["xy"], (0.3, 0.4))

    def test_absent(self):
        for t in ("没有", "图中不存在该目标", "未找到，无法给出"):
            r = A.parse_norm_reply(t)
            self.assertFalse(r["ok"])
            self.assertEqual(r["reason"], "absent")

    def test_too_few_numbers(self):
        r = A.parse_norm_reply("0.3")
        self.assertEqual(r["reason"], "unparsable")

    def test_out_of_range(self):
        r = A.parse_norm_reply("1.5 0.4")
        self.assertEqual(r["reason"], "unparsable")


class GateTest(unittest.TestCase):
    def test_unauthorized_raises(self):
        vlm = A.ZhipuVLM(api_key="fake-key")
        self.assertFalse(vlm.enabled)
        with self.assertRaises(EngineError) as cm:
            vlm.confirm_target(None, None, "登录")
        self.assertEqual(cm.exception.code, "ai_not_authorized")

    def test_authorize_with_notify(self):
        seen = []

        def notify(msg):
            seen.append(msg)
            return True

        vlm = A.ZhipuVLM(api_key="fake-key")
        self.assertTrue(vlm.authorize(notify=notify))
        self.assertTrue(seen and "智谱" in seen[0] or "截屏" in seen[0] or seen)
        vlm.revoke()
        self.assertFalse(vlm.enabled)

    def test_no_key_no_authorize(self):
        vlm = A.ZhipuVLM(api_key="")
        vlm.gate._authorized = True          # 模拟已授权但无 key
        self.assertFalse(vlm.enabled)


class SemanticStubTest(unittest.TestCase):
    def test_hit(self):
        page, boxes = S.login_page()
        stub = A.SemanticStub()
        r = stub.confirm_target(None, page, "用户名")
        self.assertTrue(r["ok"], r)
        bx = boxes["user_label"]
        exp = (bx[0] + bx[2] / 2, bx[1] + bx[3] / 2)
        dev = max(abs(r["xy"][0] - exp[0]), abs(r["xy"][1] - exp[1]))
        self.assertLessEqual(dev, 20, f"stub xy={r['xy']} exp={exp}")

    def test_absent(self):
        page, _ = S.login_page()
        r = A.SemanticStub().confirm_target(None, page, "绝无此词XYZ")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "absent")

    def test_alias_fallback(self):
        page, _ = S.erp_v2_small_page()
        stub = A.SemanticStub(aliases=["库存中心"])
        r = stub.confirm_target(None, page, "库存查询")
        self.assertTrue(r["ok"], r)
        self.assertIn("库存中心", r["note"])


class CloudMeterTest(unittest.TestCase):
    """云端用量计量（验收口径：调用次数/延迟/成本要计入指标，用户 2026-09-11 定）。

    为什么值得单测：这些数字会写进验收文档，算错了没人看得出来——
    而它们又只能靠"喂假响应"来验（真发请求不可复现、还要花钱）。
    """

    def test_counts_calls_tokens_and_latency(self):
        m = A.CloudMeter()
        m.record(True, {"prompt_tokens": 1000, "completion_tokens": 50,
                        "total_tokens": 1050}, 2000)
        m.record(True, {"prompt_tokens": 1200, "completion_tokens": 30,
                        "total_tokens": 1230}, 4000)
        s = m.snapshot()
        self.assertEqual(s["calls"], 2)
        self.assertEqual(s["errors"], 0)
        self.assertEqual(s["prompt_tokens"], 2200)
        self.assertEqual(s["total_tokens"], 2280)
        self.assertEqual(s["ms_avg"], 3000.0)
        self.assertEqual(s["tokens_avg"], 1140.0)

    def test_failed_call_counts_too(self):
        m = A.CloudMeter()
        m.record(False, None, 800)
        s = m.snapshot()
        self.assertEqual(s["calls"], 1, "失败也要计入调用次数（同样花时间/可能计费）")
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["total_tokens"], 0)

    def test_missing_usage_is_not_fatal(self):
        m = A.CloudMeter()
        m.record(True, None, 100)
        m.record(True, {}, 100)
        self.assertEqual(m.snapshot()["calls"], 2)
        self.assertEqual(m.snapshot()["total_tokens"], 0)

    def test_reset(self):
        m = A.CloudMeter()
        m.record(True, {"total_tokens": 10}, 100)
        m.reset()
        self.assertEqual(m.snapshot()["calls"], 0)
        self.assertEqual(m.snapshot()["ms_avg"], 0.0)

    def test_vlm_owns_a_meter_and_reports_stats(self):
        v = A.ZhipuVLM(api_key="k")
        v.gate.authorize()
        self.assertEqual(v.stats()["calls"], 0)
        v.meter.record(True, {"total_tokens": 7}, 500)
        st = v.stats()
        self.assertEqual(st["calls"], 1)
        self.assertEqual(st["model"], v.model)
        v.reset_stats()
        self.assertEqual(v.stats()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
