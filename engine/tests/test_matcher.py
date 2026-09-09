# -*- coding: utf-8 -*-
"""E3 matcher：data-url / 模板多尺度 / 文本相似方向规则 / OCR 条带。"""
import time
import unittest
from pathlib import Path

import numpy as np

from engine import matcher
from engine.tests import support as S


class CodecTest(unittest.TestCase):
    def test_dataurl_roundtrip(self):
        rng = np.random.default_rng(3)
        bgr = rng.integers(0, 255, (60, 90, 3), dtype=np.uint8)
        url = matcher.bgr_to_dataurl(bgr)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        back = matcher.dataurl_to_bgr(url)
        self.assertTrue(np.array_equal(bgr, back))

    def test_bad_url(self):
        with self.assertRaises(ValueError):
            matcher.dataurl_to_bgr("data:image/png;base64,!!!notbase64!!!")


class TextSimTest(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(matcher.text_similar("库存查询", "库存查询"), 1.0)

    def test_direction_substring(self):
        """长 token 含短词 → 命中（OCR 整行连读场景）。"""
        self.assertGreaterEqual(matcher.text_similar("进销存管理台库存查询", "库存查询"), 0.98)

    def test_direction_reverse_no_false_hit(self):
        """短 token 只是长目标的片段 → 不算命中（M0 实测修正的防误配规则）。"""
        self.assertLess(matcher.text_similar("查询", "库存查询"), 0.75)
        self.assertLess(matcher.text_similar("中心", "库存查询"), 0.75)

    def test_similar_but_different(self):
        self.assertLess(matcher.text_similar("库存中心", "库存查询"), 0.9)


class TemplateTest(unittest.TestCase):
    def setUp(self):
        img = S.make_page(header_text="模板测试页", w=420, h=240)
        img[60:120, 40:380] = (66, 133, 244)          # 蓝色块
        self.tpl = img

    def _canvas(self, x, y, scale=1.0):
        import cv2
        canvas = np.full((900, 1400, 3), 18, np.uint8)
        h, w = self.tpl.shape[:2]
        if scale == 1.0:
            pw, ph = w, h
            small = self.tpl
        else:
            pw, ph = int(w * scale), int(h * scale)
            small = cv2.resize(self.tpl, (pw, ph), interpolation=cv2.INTER_AREA)
        canvas[y:y + ph, x:x + pw] = small
        return canvas, (x, y, pw, ph)

    def test_hit_offset(self):
        canvas, (x, y, pw, ph) = self._canvas(333, 77)
        r = matcher.find_template(canvas, self.tpl, scales=(1.0,))
        self.assertTrue(r["ok"])
        self.assertEqual(r["rect"], (333, 77, pw, ph))

    def test_hit_scaled_0_9_and_1_2(self):
        for sc in (0.9, 1.2):
            canvas, (x, y, pw, ph) = self._canvas(200, 120, sc)
            r = matcher.find_template(canvas, self.tpl)
            self.assertTrue(r["ok"], f"scale={sc} score={r.get('score')}")
            dev = max(abs(r["rect"][0] - x), abs(r["rect"][1] - y))
            self.assertLessEqual(dev, 3, f"scale={sc} rect={r['rect']}")

    def test_missing_low_score(self):
        canvas = np.full((900, 1400, 3), 18, np.uint8)
        r = matcher.find_template(canvas, self.tpl)
        self.assertFalse(r["ok"])
        self.assertLess(r["best_score"], 0.7)

    def test_search_region(self):
        canvas, (x, y, pw, ph) = self._canvas(333, 77)
        # 区域限制到错的位置 → 找不到
        r = matcher.find_template(canvas, self.tpl, scales=(1.0,), search=(10, 10, 200, 100))
        self.assertFalse(r["ok"])
        # 区域覆盖目标 → 找到且坐标还原为整屏绝对坐标
        r2 = matcher.find_template(canvas, self.tpl, scales=(1.0,), search=(200, 40, 600, 300))
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["rect"], (333, 77, pw, ph))

    def test_speed_widget_tpl(self):
        """验收 §5.7：单步部件定位 ≤500ms（模板路径，稳态）。"""
        canvas, (x, y, pw, ph) = self._canvas(333, 77)
        tpl = self.tpl[60:120, 40:380]
        matcher.find_template(canvas, tpl, scales=(1.0,))      # warm
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            r = matcher.find_template(canvas, tpl, scales=(1.0,))
            ms = (time.perf_counter() - t0) * 1000
            best = ms if best is None else min(best, ms)
            self.assertTrue(r["ok"])
        self.assertLess(best, 500)


class OcrStripTest(unittest.TestCase):
    """合成中文条带（真实 msyh 字体渲染）→ RapidOCR 找词。"""

    def setUp(self):
        self.strip = S.make_page(w=300, h=90)
        # 深色文字 库存查询
        from PIL import ImageDraw
        self.strip = S.pil_to_bgr(_redraw(self.strip))
        # warm（引擎级单例，只冷启动一次）
        matcher.find_text_ocr(self.strip, "库存查询")

    def test_find_chinese_word(self):
        f = matcher.find_text_ocr(self.strip, "库存查询")
        self.assertTrue(f["ok"], f"未找到: {f}")
        self.assertGreaterEqual(matcher.text_similar(f["matched_text"] or "", "库存查询"), 0.8)
        self.assertIsNotNone(f["center"])

    def test_absent_word_false(self):
        f = matcher.find_text_ocr(self.strip, "不存在的词XYZ")
        self.assertFalse(f["ok"])

    def test_speed_strip(self):
        """稳态 OCR 条带（含 det+cls+rec）应 ≤800ms（波次1 max-960 定案值 90–140ms）。"""
        for _ in range(2):
            matcher.find_text_ocr(self.strip, "库存查询")
        t0 = time.perf_counter()
        f = matcher.find_text_ocr(self.strip, "库存查询")
        ms = (time.perf_counter() - t0) * 1000
        self.assertTrue(f["ok"])
        self.assertLess(ms, 800, f"OCR 条带过慢 {ms:.0f}ms")

    def test_real_erp_asset(self):
        """真实 M0 资产（ERP 页面菜单条带）识别（回归 m0 演示场景的 OCR 路径）。"""
        p = Path(__file__).resolve().parents[2] / "smoke" / "data" / "target_img" / "erp_page.png"
        if not p.exists():
            self.skipTest("smoke 资产不存在")
        import cv2
        page = cv2.imread(str(p), cv2.IMREAD_COLOR)
        crop = page[170:240, 0:260]
        f = matcher.find_text_ocr(crop, "库存查询")
        self.assertTrue(f["ok"], f"真实页面条带未识别: {f}")
        self.assertGreaterEqual(matcher.text_similar(f["matched_text"] or "", "库存查询"), 0.8)


def _redraw(bgr):
    """在 bgr 画布上加中文（避免 PIL 直接写 BGR 数组）。"""
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(bgr[:, :, ::-1])
    d = ImageDraw.Draw(pil)
    f = ImageFont.truetype(S.FONT_PATH, 24)
    d.text((20, 25), "库存查询", font=f, fill=(20, 20, 20))
    return pil


if __name__ == "__main__":
    unittest.main()
