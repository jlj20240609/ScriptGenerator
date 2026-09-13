# -*- coding: utf-8 -*-
"""整页兜底带的行为：只跑一次、且覆盖整页（2026-09-13）。

为什么钉住：以前整页兜底带按搜索点居中，于是每个搜索点各扫一次半页
（实测 2×670k 像素 = 7.3 秒，占一次定位的 61%），而两次窗口各只覆盖页面约一半高度 ——
目标落在窗口之外时这一级**永远扫不到**（不是慢，是漏）。这条测试保证：
整页扫描至多真正付一次时间，而且那一次必须覆盖整页。
"""
import unittest

from engine import locator
from engine.tests import support as S


class FullPageFallbackTest(unittest.TestCase):
    def test_page_wide_band_is_paid_once_and_covers_page(self):
        page, boxes = S.login_page()
        h, w = page.shape[:2]
        spec = S.page_spec_of(page, rect=(0, 0, w, h))
        # 用页面上绝对不存在的词 → 强制逐级升级，直到整页兜底
        tgt = S.widget_target(page, spec, boxes["login_btn"], text="绝无此物XYZ")
        res = locator.locate_widget_on_screen(page, (0, 0, w, h), tgt, exists=False)
        bands = (res.get("detail") or {}).get("l2_ocr_bands") or []
        self.assertTrue(bands, "应当留下逐带明细")
        wide = [b for b in bands if b["px"] >= 0.9 * w * h]
        paid = [b for b in wide if b["ms"] > 200]      # 缓存命中约 1ms，不算"付了时间"
        self.assertLessEqual(len(paid), 1,
                             f"整页兜底带最多只能真正跑一次，实际 {len(paid)} 次：{wide}")
        for b in paid:
            self.assertEqual((b["w"], b["h"]), (w, h), f"整页兜底带必须覆盖整页：{b}")


if __name__ == "__main__":
    unittest.main()
