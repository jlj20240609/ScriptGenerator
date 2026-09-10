# -*- coding: utf-8 -*-
"""E3 matcher：data-url / 模板多尺度 / 文本相似方向规则 / OCR 条带。"""
import time
import unittest
from pathlib import Path

import numpy as np

from engine import matcher
from engine.tests import support as S


class RingTemplateTest(unittest.TestCase):
    """环带模板（只比外圈边框/底色，中心内容不参与）：解决"占位提示被填入的数据顶替"。"""

    @staticmethod
    def _box(w=150, h=52):
        box = np.full((h, w, 3), 255, np.uint8)
        box[0:3, :] = (150, 150, 150)
        box[-3:, :] = (150, 150, 150)
        box[:, 0:3] = (150, 150, 150)
        box[:, -3:] = (150, 150, 150)
        box[18:34, 12:90] = (170, 170, 170)      # 占位提示
        return box

    def test_ring_mask_keeps_border_only(self):
        m, ring = matcher.ring_mask((52, 150))
        self.assertGreaterEqual(ring, 3)
        self.assertEqual(m.shape, (52, 150))
        self.assertEqual(int(m[26, 75]), 0)      # 中心内容区不参与
        self.assertEqual(int(m[0, 0]), 255)      # 四边参与
        self.assertEqual(int(m[-1, -1]), 255)

    def test_hit_when_input_filled(self):
        """框里被填了数据（占位提示没了）→ 环带仍命中，位置准确。"""
        tpl = self._box()
        screen = np.full((400, 600, 3), 245, np.uint8)
        screen[200:252, 300:450] = tpl.copy()
        screen[218:234, 312:390] = (40, 40, 40)  # 已填内容
        r = matcher.find_template_ring(screen, tpl, score_thr=0.60)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["center"], (375, 226))
        self.assertLess(r["rmse"], 1.0)

    def test_reject_when_frame_changed(self):
        """页面改版（底色/边框都变了）→ 必须拒绝，不能靠环带蒙混。"""
        tpl = self._box()
        other = np.full((52, 150, 3), 180, np.uint8)
        screen = np.full((400, 600, 3), 245, np.uint8)
        screen[200:252, 300:450] = other
        r = matcher.find_template_ring(screen, tpl, score_thr=0.60)
        self.assertFalse(r["ok"], r)
        self.assertLess(r["score"], 0.6)

    def test_scale_follows_page_scale(self):
        import cv2
        tpl = self._box()
        big = cv2.resize(tpl, None, fx=1.2, fy=1.2, interpolation=cv2.INTER_CUBIC)
        screen = np.full((520, 820, 3), 245, np.uint8)
        screen[100:100 + big.shape[0], 120:120 + big.shape[1]] = big
        r = matcher.find_template_ring(screen, tpl, scale=1.2, score_thr=0.55)
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(r["center"][0], 120 + big.shape[1] // 2, delta=3)


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


class OcrAutoTest(unittest.TestCase):
    """部件级 OCR（ocr_run_auto）：小图先原图、认不出再放大（双截图第二步用）。"""

    def test_large_crop_single_pass(self):
        import numpy as np
        from unittest import mock
        img = np.zeros((200, 300, 3), dtype=np.uint8)
        calls = []

        def fake(bgr, timeout_s=None):
            calls.append(bgr.shape[:2])
            return {"txts": ["登录"], "boxes": [(10, 10, 40, 20)], "scores": [0.9],
                    "elapsed_ms": 12.0, "engine": "fake", "ok": True}

        with mock.patch.object(matcher, "ocr_run", fake):
            r = matcher.ocr_run_auto(img)
        self.assertEqual(calls, [(200, 300)])          # 大图只跑一次
        self.assertEqual(r["txts"], ["登录"])
        self.assertEqual(r["scale_used"], 0)

    def test_small_crop_upscale_fallback(self):
        import numpy as np
        from unittest import mock
        img = np.zeros((50, 80, 3), dtype=np.uint8)
        seen = []

        def fake(bgr, timeout_s=None):
            seen.append(bgr.shape[:2])
            if len(seen) == 1:                          # 原图：只认出单字
                return {"txts": [], "boxes": [], "scores": [], "elapsed_ms": 900.0,
                        "engine": "fake", "ok": True}
            return {"txts": ["登录"], "boxes": [(20, 40, 80, 30)], "scores": [0.88],
                    "elapsed_ms": 120.0, "engine": "fake", "ok": True}

        with mock.patch.object(matcher, "ocr_run", fake):
            r = matcher.ocr_run_auto(img)
        self.assertEqual(seen[0], (50, 80))
        self.assertEqual(seen[1], (50 * r["scale_used"], 80 * r["scale_used"]))
        self.assertGreaterEqual(r["scale_used"], 2)
        self.assertEqual(r["txts"], ["登录"])
        self.assertEqual(r["boxes"][0], (20 // r["scale_used"], 40 // r["scale_used"],
                                        80 // r["scale_used"], 30 // r["scale_used"]))
        self.assertGreaterEqual(r["elapsed_ms"], 1000.0)   # 两次耗时相加

    def test_small_crop_fallback_failure_keeps_first(self):
        import numpy as np
        from unittest import mock
        img = np.zeros((40, 60, 3), dtype=np.uint8)
        first = {"txts": [], "boxes": [], "scores": [], "elapsed_ms": 5.0,
                 "engine": "rapidocr", "ok": False, "error": "timeout"}
        with mock.patch.object(matcher, "ocr_run", lambda bgr, timeout_s=None: dict(first)) as _m:
            r = matcher.ocr_run_auto(img)
        self.assertEqual(r["error"], "timeout")            # 放大也没认出来 → 保留原结果
        self.assertEqual(r["txts"], [])
        self.assertIn("scale_tried", r)

    def test_single_char_gets_upscaled(self):
        """只认出孤零零一个字（“登”）→ 放大后拿到完整词（“登录”）。"""
        import numpy as np
        from unittest import mock
        img = np.zeros((52, 76, 3), dtype=np.uint8)
        seen = []

        def fake(bgr, timeout_s=None):
            seen.append(bgr.shape[:2])
            if len(seen) == 1:
                return {"txts": ["登", "录"], "boxes": [(1, 1, 20, 20), (24, 1, 20, 20)],
                        "scores": [0.9, 0.9], "elapsed_ms": 200.0, "engine": "fake", "ok": True}
            return {"txts": ["登录"], "boxes": [(6, 6, 90, 60)], "scores": [0.95],
                    "elapsed_ms": 150.0, "engine": "fake", "ok": True}

        with mock.patch.object(matcher, "ocr_run", fake):
            r = matcher.ocr_run_auto(img)
        self.assertEqual(len(seen), 2)                   # 单字结果也会触发放大
        self.assertEqual(r["txts"], ["登录"])
        self.assertGreaterEqual(r["scale_used"], 2)

    def test_real_small_widget(self):
        """真实小部件（ERP 菜单 70px 高）→ 放大路径能读出文字。"""
        p = Path(__file__).resolve().parents[2] / "smoke" / "data" / "target_img" / "erp_page.png"
        if not p.exists():
            self.skipTest("smoke 资产不存在")
        import cv2
        page = cv2.imread(str(p), cv2.IMREAD_COLOR)
        crop = page[170:240, 0:260]
        matcher.ocr_run_auto(crop)                        # warm
        r = matcher.ocr_run_auto(crop)
        self.assertTrue(r["ok"], r)
        self.assertTrue([t for t in r["txts"] if t.strip()], f"小部件未识别出文字: {r}")


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
