# -*- coding: utf-8 -*-
"""M3-WP4 自校准 AI 化的离线测试：第二问「它现在叫什么」。

为什么要有第二问：界面改版常常只是**改了名字**（库存查询 → 库存中心）。这时第一问
给出的粗圈是对的，但圈区里用旧词 OCR 一定找不到 —— 少的就是"新名字"。
桩（SemanticStub）一直靠外部喂 aliases 模拟这个词，真实云端却没人问过；
这两条测试把"问出来"这一步补上，并钉住它**只在需要时才问**（不白花一次往返）。
"""
import unittest

from engine import ai as A
from engine import calibrator as C
from engine.tests import support as S
from engine.tests.test_calibrator import _canvas_of

PX, PY = 300, 150
CANVAS = (1500, 1050)


class ParseRenameReplyTest(unittest.TestCase):
    def test_plain_word(self):
        r = A.parse_rename_reply("库存中心")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "库存中心")

    def test_strips_noise(self):
        for raw, want in [("「库存中心」", "库存中心"),
                          ("**库存中心**", "库存中心"),
                          ("答案：库存中心", "答案：库存中心"),   # 前缀算式超长则由长度守卫处理
                          ("  库存中心。  ", "库存中心"),
                          ('"库存中心"', "库存中心")]:
            got = A.parse_rename_reply(raw)
            self.assertTrue(got["ok"], raw)
            self.assertTrue(got["text"].endswith("库存中心"), f"{raw} → {got['text']}")

    def test_skips_leading_chatter(self):
        r = A.parse_rename_reply("好的，我看看。\n库存中心")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "库存中心", "模型先说一句话时取真正那一行")

    def test_absent(self):
        for raw in ("没有", "找不到", "未找到", ""):
            self.assertFalse(A.parse_rename_reply(raw)["ok"], raw)

    def test_too_long_is_not_a_name(self):
        r = A.parse_rename_reply("图2里对应的按钮现在叫做库存中心，位置在左侧菜单第二项")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "too_long")


class _NoRenameAI:
    """只有第一问、没有第二问的旧式 AI：校准器不该因此崩，应照旧走人工兜底。"""

    def confirm_target(self, old_widget, screen, semantic, hint_xy=None):
        return {"ok": True, "xy": (PX + 40, PY + 205), "norm": (0.1, 0.3),
                "note": "旧式 AI（只会给粗圈）", "elapsed_ms": 1.0}


class PrefixRenameTest(unittest.TestCase):
    """前缀兼容的改名（库存查询→库存中心）**不需要问 AI**：本地短前缀就找到了。

    这条是上面一次失败的测试教给我的：我原以为这个场景必须问云端，实测发现
    `_localize` 的短前缀启发式（"库存"）直接就命中了。既然不用花钱，就得钉住
    "确实没问"——否则将来有人把前缀启发式改坏了，也不会有测试叫。
    """

    def setUp(self):
        self.v1, b1 = S.erp_v1_page()
        self.v2, _ = S.erp_v2_small_page()
        self.v2_canvas, self.v2_rect = _canvas_of(self.v2)
        self.spec1 = S.page_spec_of(self.v1)
        self.target = S.widget_target(self.v1, self.spec1, b1["menu"], text="库存查询")

    def test_prefix_rename_needs_no_ai(self):
        ai = _CountingStub(aliases=["库存中心"])
        cal = C.Calibrator(S.FakeDriver(lambda: self.v2_canvas), ai=ai)
        res = cal({"reason": "widget_not_found", "target": self.target,
                   "page_spec": self.spec1, "page_rect": self.v2_rect,
                   "loc_rows": [], "ctx": {}})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.target["text"], "库存中心")
        self.assertEqual(ai.rename_calls, 0,
                         "共同前缀就能找到时不该多问一次云端")


class _CountingStub(A.SemanticStub):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rename_calls = 0

    def ask_rename(self, old, screen, semantic):
        self.rename_calls += 1
        return super().ask_rename(old, screen, semantic)


class _RealLikeAI:
    """模拟**真实云端**的行为：`confirm_target` 只给一个粗圈、**不带 matched**。

    为什么需要它：桩（SemanticStub）是靠 aliases 直接给出 `matched`（新词）的，
    所以桩根本走不到 `ask_rename` 这条路 —— 拿桩测"第二问"是测不到的。
    真机 ZhipuVLM.confirm_target 的返回里没有 matched，只问得出坐标；
    名字必须靠第二问。这个类就是按真机的返回形状写的。
    """

    def __init__(self, rename="存货台账", coarse=(PX + 40, PY + 205), fail=False):
        self.rename = rename
        self.coarse = coarse
        self.fail = fail
        self.rename_calls = 0

    def confirm_target(self, old_widget, screen, semantic, hint_xy=None):
        return {"ok": True, "xy": self.coarse, "norm": (0.1, 0.3),
                "note": "云端粗圈（只给坐标）", "elapsed_ms": 2.0}

    def ask_rename(self, old_widget, screen, semantic):
        self.rename_calls += 1
        if self.fail:
            raise RuntimeError("云端挂了")
        if not self.rename:
            return {"ok": False, "text": "", "reason": "absent", "note": "云端说找不到"}
        return {"ok": True, "text": self.rename,
                "note": f"AI 说它现在叫「{self.rename}」", "elapsed_ms": 2.0}


class RenameRecoveryTest(unittest.TestCase):
    """前缀对不上的改名（库存查询→存货台账）：这才是第二问真正要解决的场景。"""

    def setUp(self):
        self.v1, b1 = S.erp_v1_page()
        self.v2, b2 = S.erp_renamed_hard_page()
        self.v2_canvas, self.v2_rect = _canvas_of(self.v2)
        self.spec1 = S.page_spec_of(self.v1)
        self.target = S.widget_target(self.v1, self.spec1, b1["menu"], text="库存查询")

    def _cal(self, ai):
        return C.Calibrator(S.FakeDriver(lambda: self.v2_canvas), ai=ai)

    def _req(self):
        return {"reason": "widget_not_found", "target": self.target,
                "page_spec": self.spec1, "page_rect": self.v2_rect,
                "loc_rows": [], "ctx": {}}

    def test_prefix_heuristic_alone_would_fail(self):
        """前置：只给粗圈（真机形状）而不给新名字时，本地恢复不了。"""
        cal = self._cal(_RealLikeAI(rename=None))
        res = cal(self._req())
        self.assertFalse(res["ok"], "没有新名字时应恢复失败（低置信交给人）")
        self.assertEqual(self.target["text"], "库存查询", "没恢复就不许改脚本")

    def test_real_like_ai_recovers_via_rename(self):
        ai = _RealLikeAI(rename="存货台账")
        cal = self._cal(ai)
        res = cal(self._req())
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["updated"], res)
        self.assertEqual(self.target["text"], "存货台账")
        self.assertEqual(ai.rename_calls, 1, "名字必须问出来（真机 confirm_target 不给词）")
        self.assertIn("存货台账", str(res.get("detail", {}).get("rename", "")))

    def test_rename_ai_crash_degrades(self):
        cal = self._cal(_RealLikeAI(fail=True))
        res = cal(self._req())
        self.assertFalse(res["ok"])
        self.assertIn("AI 调用异常", str(res.get("detail", {}).get("rename", "")))
        self.assertEqual(self.target["text"], "库存查询")

    def test_rename_absent_degrades(self):
        """云端说"找不到对应项" → 不许瞎猜，照旧人工兜底。"""
        cal = self._cal(_RealLikeAI(rename=None))
        res = cal(self._req())
        self.assertFalse(res["ok"])
        self.assertEqual(self.target["text"], "库存查询")
        self.assertFalse(res.get("updated"))


if __name__ == "__main__":
    unittest.main()
