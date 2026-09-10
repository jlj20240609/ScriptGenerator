# -*- coding: utf-8 -*-
"""E1 capture：纯几何/采集构件（离线可测部分）。"""
import unittest

import numpy as np

from engine import capture, matcher, schema
from engine.tests import support as S


class GeometryTest(unittest.TestCase):
    def test_page_rect_from_anchor_identity(self):
        """锚命中矩形 = 页内框 × 缩放 → 页面原点还原。"""
        page_size = (1000, 640)
        anchor_rect = [293, 49, 356, 43]
        # 页面摆到屏幕 (400, 250)，缩放 1.0 → 锚绝对位置
        s = 1.0
        hit = (400 + 293 * s, 250 + 49 * s, 356 * s, 43 * s)
        r = capture.page_rect_from_anchor(hit, anchor_rect, page_size)
        self.assertTrue(r["ok"])
        self.assertEqual(r["rect"], (400, 250, 1000, 640))

    def test_page_rect_from_anchor_scaled(self):
        page_size = (1000, 640)
        anchor_rect = [100, 50, 200, 30]
        sc = 1.25
        # 页面原点 (700,300)，锚页内 (100,50) → 屏幕 (825, 362.5)
        hit = (700 + 100 * sc, 300 + 50 * sc, 200 * sc, 30 * sc)
        r = capture.page_rect_from_anchor(hit, anchor_rect, page_size)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["scale"], 1.25, places=3)
        self.assertEqual(r["rect"], (700, 300, round(1000 * sc), round(640 * sc)))

    def test_bad_anchor_geometry(self):
        r = capture.page_rect_from_anchor((10, 10, 0, 0), [1, 1, 2, 2], (100, 60))
        self.assertFalse(r["ok"])

    def test_crop_rect_clamp(self):
        img = np.full((100, 200, 3), 7, np.uint8)
        c = capture.crop_rect(img, (-10, -10, 300, 300))
        self.assertEqual(c.shape, (100, 200, 3))
        self.assertIsNone(capture.crop_rect(img, (200, 0, 50, 50)))    # 完全越界 → None
        self.assertIsNone(capture.crop_rect(img, (0, 100, 50, 50)))

    def test_static_score(self):
        a = np.random.default_rng(1).integers(0, 255, (120, 200, 3), dtype=np.uint8)
        self.assertGreaterEqual(capture.static_score(a, a.copy()), 0.99)


class VerifyStaticTest(unittest.TestCase):
    def test_stable_true(self):
        a = np.random.default_rng(2).integers(0, 255, (60, 90, 3), dtype=np.uint8)
        frames = iter([a.copy(), a.copy()])
        r = capture.verify_static(a, lambda: next(frames), dt_s=0.0, thr=0.85)
        self.assertTrue(r["ok"])

    def test_changed_false(self):
        a = np.random.default_rng(3).integers(0, 255, (60, 90, 3), dtype=np.uint8)
        b = np.random.default_rng(4).integers(0, 255, (60, 90, 3), dtype=np.uint8)
        r = capture.verify_static(a, lambda: b, dt_s=0.0, thr=0.85)
        self.assertFalse(r["ok"])


class WindowForRectTest(unittest.TestCase):
    """框选区域归属窗口判定：交叠面积最大者（截图前要先把它切到前面）。"""

    @staticmethod
    def _patch(wins):
        from unittest import mock
        return mock.patch.object(capture, "top_windows", lambda *a, **k: wins)

    def test_picks_largest_overlap(self):
        wins = [
            (11, (0, 0, 800, 600), "背景", "Cls"),
            (22, (0, 0, 1000, 700), "目标", "Cls"),
        ]
        with self._patch(wins):
            hwnd, cover = capture.window_for_rect([0, 0, 900, 650])
        self.assertEqual(hwnd, 22)
        self.assertGreater(cover, 0.9)

    def test_partial_cover_reported(self):
        wins = [(33, (0, 0, 500, 500), "半个窗口", "Cls")]
        with self._patch(wins):
            hwnd, cover = capture.window_for_rect([0, 0, 1000, 500])
        self.assertEqual(hwnd, 33)
        self.assertAlmostEqual(cover, 0.5, places=2)

    def test_no_window(self):
        with self._patch([]):
            hwnd, cover = capture.window_for_rect([0, 0, 100, 100])
        self.assertEqual((hwnd, cover), (0, 0.0))

    def test_tie_prefers_topmost(self):
        """两个窗口都完整覆盖 → 取最上面那个（用户看到的就是它）。"""
        wins = [
            (44, (0, 0, 900, 700), "最上面", "Cls"),
            (55, (0, 0, 1600, 1000), "被压在下面", "Cls"),
        ]
        with self._patch(wins):
            hwnd, cover = capture.window_for_rect([0, 0, 900, 700])
        self.assertEqual(hwnd, 44)
        self.assertAlmostEqual(cover, 1.0, places=2)


class SpecBuildingTest(unittest.TestCase):
    def test_make_page_spec_fields(self):
        bgr = S.make_page(header_text="锚测试", w=400, h=260)
        spec = capture.make_page_spec(bgr=bgr, rect_in_screen=[13, 13, 413, 273],
                                      context={"process": "a.exe"}, dpi=120)
        self.assertEqual(spec["size"], [400, 260])
        self.assertEqual(spec["context"]["process"], "a.exe")
        self.assertEqual(spec["capture_meta"]["dpi"], 120)
        # image 可解码回原尺寸
        back = matcher.dataurl_to_bgr(spec["image"])
        self.assertEqual(back.shape[:2], (260, 400))
        self.assertEqual(schema.validate({"version": "1.0", "steps": [
            {"id": "x", "type": "action", "action": "click", "target": {"page": spec,
                                                                        "text": "t"}}]}), [])

    def test_anchor_spec_crop(self):
        page = S.make_page(header_text="静态顶栏", w=500, h=300)
        # 顶栏区域 0,0,500,40
        a = capture.crop_anchor_spec(page, [0, 0, 500, 40])
        self.assertIn("stable_at", a)
        crop = matcher.dataurl_to_bgr(a["image"])
        self.assertEqual(crop.shape[:2], (40, 500))

    def test_anchor_out_of_page_raises(self):
        page = S.make_page(w=100, h=60)
        with self.assertRaises(ValueError):
            capture.crop_anchor_spec(page, [120, 0, 40, 10])   # 完全越出页面

    def test_crop_widget(self):
        page, boxes = S.login_page()
        bx = boxes["login_btn"]
        crop = capture.crop_widget(page, bx)
        self.assertEqual(crop.shape[:2], (bx[3], bx[2]))


if __name__ == "__main__":
    unittest.main()
