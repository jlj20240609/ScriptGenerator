# -*- coding: utf-8 -*-
"""E2 locator：页面定位（模板→锚）+ 页内三级部件定位 + 范围约束。"""
import unittest

import numpy as np

from engine import locator
from engine.locator import LocConfig
from engine.tests import support as S

CANVAS = (1500, 1050)


def login_scene(scale=1.0):
    page, boxes = S.login_page()
    screen, rect = S.scene_of(page, 300, 150, scale, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
    return screen, rect, page, boxes


class PageLocateTest(unittest.TestCase):
    def test_page_tpl_hit(self):
        page, boxes = S.login_page()
        for sc, pos in ((1.0, (300, 150)), (1.2, (260, 120)), (0.9, (400, 250))):
            with self.subTest(scale=sc):
                screen, rect = S.scene_of(page, pos[0], pos[1], sc,
                                          canvas_w=CANVAS[0], canvas_h=CANVAS[1])
                spec = S.page_spec_of(page, rect=rect)
                r = locator.locate_page(screen, spec)
                self.assertTrue(r["ok"], f"scale={sc} {r}")
                self.assertEqual(r["method"], locator.M_PAGE_TPL)
                dev = max(abs(r["rect"][0] - rect[0]), abs(r["rect"][1] - rect[1]))
                self.assertLessEqual(dev, 4, f"scale={sc} rect={r['rect']} exp={rect}")
                self.assertGreater(r["confidence"], 0.8)

    def test_page_missing_fail(self):
        page, _ = S.login_page()
        screen = np.full((CANVAS[1], CANVAS[0], 3), 18, np.uint8)
        spec = S.page_spec_of(page)
        r = locator.locate_page(screen, spec)
        self.assertFalse(r["ok"])

    def test_prev_hint_reuse(self):
        screen, rect, _, _ = login_scene()
        spec = S.page_spec_of(S.login_page()[0], rect=rect)
        r1 = locator.locate_page(screen, spec)
        r2 = locator.locate_page(screen, spec, prev_hint=r1["rect"])
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertTrue(r2["reused"])
        self.assertLess(r2["elapsed_ms"], r1["elapsed_ms"] + 5)


class PageSimSoftTest(unittest.TestCase):
    """同源确认折中档（用户审查要求）：默认口径下同页可过、异页必须被挡。

    实测（smoke/diag/probe_page_sim.py）：同页改动后同源 0.93+；异页 home 0.772、
    纯白页 0.884（但它的整窗模板分数是 0.000）—— 所以折中档定 0.80 且要求模板分数 ≥0.85。
    """

    def test_same_page_slightly_changed_is_accepted(self):
        page, _ = S.login_page()
        light = page.copy()
        light[520:560, 200:600] = (150, 150, 150)            # 页脚改色
        screen, rect = S.scene_of(light, 300, 150, 1.0,
                                  canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        spec = S.page_spec_of(page, rect=rect)
        r = locator.locate_page(screen, spec)                # 默认配置
        self.assertTrue(r["ok"], r.get("detail"))
        self.assertEqual(r["method"], locator.M_PAGE_TPL)
        self.assertFalse(r.get("soft"), f"这么高相似度不该走折中档：{r.get('sim')}")

    def test_different_page_is_rejected(self):
        """异页即使同源相似度不低（实测 0.772），也必须被挡下。"""
        page, _ = S.login_page()
        other, _ = S.home_page()
        screen, rect = S.scene_of(other, 300, 150, 1.0,
                                  canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        spec = S.page_spec_of(page, rect=rect)
        r = locator.locate_page(screen, spec)                # 默认配置
        self.assertFalse(r["ok"], f"异页不能过：sim={r.get('sim')} conf={r.get('confidence')}")

    def test_blank_page_is_rejected(self):
        """低纹理纯白页：同源分数可能不低，但整窗模板分数为 0 → 必须被挡。"""
        page, _ = S.login_page()
        blank = page.copy()
        blank[:] = 250
        screen, rect = S.scene_of(blank, 300, 150, 1.0,
                                  canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        spec = S.page_spec_of(page, rect=rect)
        r = locator.locate_page(screen, spec)
        self.assertFalse(r["ok"], f"纯白页不能过：sim={r.get('sim')}")

    def test_soft_band_accepts_with_warning(self):
        """人为把稳线抬到 0.99 → 中等改版页落到折中档：采纳 + soft 标记 + 置信度打折。"""
        page, _ = S.login_page()
        mid = page.copy()
        mid[300:420, 150:560] = (235, 235, 235)
        screen, rect = S.scene_of(mid, 300, 150, 1.0,
                                  canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        spec = S.page_spec_of(page, rect=rect)
        cfg = LocConfig(page_sim_min=0.99, page_sim_soft=0.80)
        r = locator.locate_page(screen, spec, cfg=cfg)
        self.assertTrue(r["ok"], r.get("detail"))
        self.assertTrue(r.get("soft"), r)
        self.assertEqual(r["detail"]["page_tpl"]["verdict"], "warn")


class AnchorLocateTest(unittest.TestCase):
    @staticmethod
    def _logo_block():
        """顶部静态区（两页共有的“logo 带”），保证与页面其他区域显著不同。"""
        blk = np.zeros((46, 320, 3), np.uint8)
        blk[:] = (30, 40, 60)                       # 深底
        blk[8:38, 12:60] = (230, 180, 40)           # 黄块
        blk[10:36, 66:100] = (240, 240, 240)        # 白块
        blk[10:36, 104:300] = (70, 90, 130)         # 蓝灰条
        return blk

    def test_anchor_recovery_after_template_mismatch(self):
        """整窗模板失配（屏幕为反色改版页）→ 静态锚恢复页面原点。"""
        page1, _ = S.login_page()
        logo = self._logo_block()
        page1[0:46, 0:320] = logo
        # 反色改版页（模板必失配）+ 同一静态 logo 覆盖
        import cv2
        page2 = cv2.bitwise_not(page1.copy())
        page2[0:46, 0:320] = logo
        spec = S.page_spec_of(page1)
        anchor_crop = page1[0:46, 0:320]
        spec["anchors"] = [{"image": S._b64(anchor_crop), "rect_in_page": [0, 0, 320, 46]}]
        screen, rect = S.scene_of(page2, 410, 260, 1.0,
                                  canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        r = locator.locate_page(screen, spec)
        tpl_score = (r.get("detail") or {}).get("page_tpl", {}).get("score", 0.0)
        self.assertLess(tpl_score, 0.72, f"整窗模板不应命中: {tpl_score}")
        self.assertTrue(r["ok"], f"锚应命中: {r}")
        self.assertEqual(r["method"], locator.M_ANCHOR)
        self.assertEqual(r["rect"], rect)

    def test_no_anchor_no_recovery(self):
        page1, _ = S.login_page()
        logo = self._logo_block()
        page1[0:46, 0:320] = logo
        import cv2
        page2 = cv2.bitwise_not(page1.copy())
        page2[0:46, 0:320] = logo
        spec = S.page_spec_of(page1)   # 无 anchors
        screen, _ = S.scene_of(page2, 410, 260, 1.0,
                               canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        r = locator.locate_page(screen, spec)
        self.assertFalse(r["ok"])


class AnchorConsensusTest(unittest.TestCase):
    """M2-WP2 多锚共识（判决层，纯函数）。

    M1 是"命中第一个锚就返回"——单个锚误命中时没有任何东西能纠正它。多锚共识让锚互相印证：
    一致组取中位（抗一个离群锚）、矛盾时诚实标 disagree 并打折置信度。
    """

    @staticmethod
    def _cand(x, y, score, scale=1.0):
        return {"rect": (x, y, 800, 600), "scale": scale, "score": score, "i": 0}

    def test_consistent_group_beats_outlier(self):
        cands = [self._cand(100, 50, 0.99), self._cand(104, 52, 0.96),
                 self._cand(900, 700, 0.97)]        # 第三个分数最高，但它离群
        con = locator.anchor_consensus(cands, page_size=[800, 600])
        self.assertEqual(con["consensus"], 2, con)
        self.assertTrue(con["disagree"])
        self.assertEqual(con["rect"], (102, 51, 800, 600), "一致组应取中位")
        self.assertAlmostEqual(con["confidence"], 0.975 * 0.85, places=4)

    def test_single_anchor_same_as_m1(self):
        """只录了一个锚（老脚本）→ 矩形/置信度与 M1 单锚逐字段一致。"""
        c = self._cand(300, 220, 0.88)
        con = locator.anchor_consensus([c], page_size=[800, 600])
        self.assertEqual(con["consensus"], 1)
        self.assertFalse(con["disagree"])
        self.assertEqual(con["rect"], c["rect"])
        self.assertEqual(con["scale"], 1.0)
        self.assertAlmostEqual(con["confidence"], 0.88, places=6)

    def test_conflict_takes_highest_with_disagree(self):
        """两个锚互相矛盾 → 判断不了谁对：取高分者，但明确标 disagree 并打折。"""
        con = locator.anchor_consensus([self._cand(100, 50, 0.80),
                                        self._cand(900, 700, 0.95)], page_size=[800, 600])
        self.assertEqual(con["consensus"], 1)
        self.assertTrue(con["disagree"])
        self.assertEqual(con["rect"][0], 900)
        self.assertAlmostEqual(con["confidence"], 0.95 * 0.85, places=4)

    def test_scale_mismatch_not_grouped(self):
        """原点接近但缩放差 3% 以上 → 不算一致（可能是两个不同的匹配）。"""
        con = locator.anchor_consensus([self._cand(100, 50, 0.95, scale=1.0),
                                        self._cand(102, 52, 0.95, scale=1.20)],
                                       page_size=[800, 600])
        self.assertEqual(con["consensus"], 1)
        self.assertTrue(con["disagree"])

    def test_has_consensus_early_stop(self):
        a, b, c = (self._cand(100, 50, 0.9), self._cand(104, 51, 0.9),
                   self._cand(900, 700, 0.9))
        self.assertFalse(locator.has_consensus([a]))
        self.assertTrue(locator.has_consensus([a, b]), "两个一致即可早停")
        self.assertTrue(locator.has_consensus([c, a, b]))
        self.assertFalse(locator.has_consensus([a, c]), "互相矛盾不算共识")

    def test_no_candidates_returns_none(self):
        self.assertIsNone(locator.anchor_consensus([], page_size=[800, 600]))


class MultiAnchorLocateTest(unittest.TestCase):
    """M2-WP2：动态页（顶栏恒定 + 主区每帧变）上两个锚互相印证还原页面原点。"""

    W, H = 1100, 760

    def _scene_with_two_bands(self):
        scene = S.FeedScene(w=self.W, h=self.H, top_h=46)
        spec = S.page_spec_of(scene.screen(), rect=(0, 0, self.W, self.H))
        bands = [(0, 0, 300, 46), (self.W - 300, 0, 300, 46)]   # 顶栏左段 + 右段（含时钟）
        frame = scene.screen()
        spec["anchors"] = [
            {"image": S._b64(frame[b[1]:b[1] + b[3], b[0]:b[0] + b[2]]),
             "rect_in_page": list(b)} for b in bands]
        scene.next_frame()
        scene.next_frame()                     # 主区变了 → 整窗模板必然失配
        return scene, spec

    def test_two_anchors_consensus_on_screen(self):
        scene, spec = self._scene_with_two_bands()
        r = locator.locate_page(scene.screen(), spec)
        tpl_score = (r.get("detail") or {}).get("page_tpl", {}).get("score", 1.0)
        self.assertLess(tpl_score, 0.80, f"前置：动态帧整窗模板不该命中 {tpl_score}")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_ANCHOR)
        self.assertEqual(r.get("consensus"), 2, f"两个锚应互相印证 {r}")
        self.assertFalse(r.get("disagree"))
        self.assertLessEqual(max(abs(r["rect"][0]), abs(r["rect"][1])), 3, r["rect"])

    def test_two_anchors_restore_offset_page(self):
        """页面被挪到画布另一处：两个锚仍还原原点（而不是靠"页面在左上角"这个假设）。"""
        scene, spec = self._scene_with_two_bands()
        canvas = np.full((900, 1400, 3), 12, np.uint8)
        canvas[30:30 + self.H, 60:60 + self.W] = scene.screen()
        r = locator.locate_page(canvas, spec)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("consensus"), 2, r)
        self.assertLessEqual(abs(r["rect"][0] - 60), 3, r["rect"])
        self.assertLessEqual(abs(r["rect"][1] - 30), 3, r["rect"])


class NearbyHardGateTest(unittest.TestCase):
    """有邻居记录、但所有候选的邻居都对不上 → 不采纳文字定位（宁可失败也不误点）。

    场景来源（真机实测）：页面大幅重排 + 同页同名时，②a 的搜索带会在"带1 命中干扰"后停止
    扩大，候选里可能只剩干扰；而 nearby_ok 原先只参与排序、不是门槛，于是会直接点到干扰上——
    click_guard 也挡不住（干扰恰恰"长得像目标"）。
    """

    def setUp(self):
        self.screen, self.rect, self.page, self.boxes = login_scene()
        self.page_spec = S.page_spec_of(self.page, rect=self.rect)

    def _target(self, nearby):
        bx = self.boxes["login_btn"]
        t = S.widget_target(self.page, self.page_spec, bx, text="登录")
        if nearby is not None:
            t["nearby"] = nearby
        return t

    def test_wrong_nearby_rejects_text_path(self):
        # 记一个页面上根本没有的邻居 → 所有候选都对不上 → 不该拿文字定位的框
        t = self._target([{"text": "服务条款", "offset": [0, 0],
                           "rect_in_page": [10, 10, 120, 24]}])
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertNotEqual(r.get("method"), locator.M_OCR_TEXT,
                            f"邻居全对不上时不该用文字定位 {r}")
        if r.get("ok"):
            # 走到别的级别也可以（模板/环带/坐标），但要能看出来是"降级"而不是"照旧"
            self.assertIn(r.get("method"),
                          (locator.M_TPL, locator.M_TPL_RING, locator.M_PAGE_COORD))

    def test_correct_nearby_still_uses_text_path(self):
        """邻居对得上时行为不变（别把正常路径也挡了）。"""
        lb, bb = self.boxes["user_label"], self.boxes["login_btn"]
        off = [lb[0] + lb[2] // 2 - (bb[0] + bb[2] // 2),
               lb[1] + lb[3] // 2 - (bb[1] + bb[3] // 2)]
        t = self._target([{"text": "用户名", "offset": off, "rect_in_page": list(lb)}])
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("method"), locator.M_OCR_TEXT, f"正常路径应仍走文字定位 {r}")

    def test_wrong_nearby_rejects_template_and_ring_too(self):
        """②a 被拒之后，②b 模板/②c 环带不能把干扰捡回来。

        模板是"在录点附近找长得像的"，而同页同名干扰恰恰就在录点附近——三级都指向干扰时，
        click_guard 也挡不住（干扰"长得像目标"），所以三级都要过邻居这一关。
        """
        t = self._target([{"text": "服务条款", "offset": [0, 0],
                           "rect_in_page": [10, 10, 120, 24]}])
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertNotIn(r.get("method"),
                         (locator.M_OCR_TEXT, locator.M_TPL, locator.M_TPL_RING),
                         f"文字/模板/环带都不该采纳 {r}")
        d = r.get("detail") or {}
        self.assertTrue(d.get("l2_tpl_nearby_rejected") or d.get("l2_tpl", {}).get("ok") is False,
                        f"模板应因邻居不符被拒 {d.get('l2_tpl')}")

    def test_no_nearby_record_behavior_unchanged(self):
        """老脚本没记邻居 → 行为与之前完全一样（门槛不生效）。"""
        t = self._target(None)
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("method"), locator.M_OCR_TEXT)


class FeatureFallbackTest(unittest.TestCase):
    """整窗模板与静态锚**都**失败时的局部特征兜底（ORB + RANSAC，M2-WP2 第二步）。

    为什么需要：强动态页整窗必然失配，锚也可能不在（页面被大幅重排/滚动/整体换布局）。
    这里用"页面整体旋转 8°"构造这个局面——模板匹配不含旋转，必然失败；局部特征对旋转不敏感。
    口径仍然是"宁可失败也不误点"：不相干的画面必须返回 not found，绝不硬给一个矩形。
    """

    @staticmethod
    def _rotated_scene(angle=8.0):
        import cv2
        rng = np.random.default_rng(7)
        page = rng.integers(60, 200, (300, 420, 3), dtype=np.uint8)
        cv2.rectangle(page, (20, 20), (400, 60), (240, 240, 240), -1)
        cv2.putText(page, "A1B2C3 查询", (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (30, 30, 30), 2)
        canvas = np.full((700, 900, 3), 28, np.uint8)
        m = cv2.getRotationMatrix2D((210, 150), angle, 1.0)
        m[0, 2] += 260
        m[1, 2] += 180
        cv2.warpAffine(page, m, (900, 700), dst=canvas, borderValue=(28, 28, 28))
        return page, canvas

    def test_feature_fallback_recovers_rotated_page(self):
        page, canvas = self._rotated_scene()
        spec = S.page_spec_of(page)
        r = locator.locate_page(canvas, spec)
        tpl = (r.get("detail") or {}).get("page_tpl", {}).get("score", 1.0)
        self.assertLess(tpl, 0.72, f"前置：旋转后整窗模板应失配 {tpl}")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_FEATURE, r)
        self.assertLessEqual(max(abs(r["rect"][0] - 241), abs(r["rect"][1] - 152)), 16,
                             f"页面原点应接近真值 {r['rect']}")

    def test_negative_scene_still_fails(self):
        """不相干的画面：特征兜底必须说"没找到"而不是硬给一个矩形。"""
        page, _ = self._rotated_scene()
        rng = np.random.default_rng(11)
        noise = rng.integers(60, 200, (700, 900, 3), dtype=np.uint8)
        spec = S.page_spec_of(page)
        r = locator.locate_page(noise, spec)
        self.assertFalse(r["ok"], f"不相干的画面不该被认成页面 {r}")
        self.assertIn("page_feature", (r.get("detail") or {}), "要留下尝试痕迹")

    def test_can_be_disabled(self):
        """关掉兜底 → 行为回到 M1（整窗+锚都不行就是找不到）。"""
        page, canvas = self._rotated_scene()
        spec = S.page_spec_of(page)
        r = locator.locate_page(canvas, spec, cfg=LocConfig(use_feature_fallback=False))
        self.assertFalse(r["ok"], r)
        self.assertNotIn("page_feature", (r.get("detail") or {}))


class CrossScaleTest(unittest.TestCase):
    """跨显示缩放（DPI）下的页面定位 —— WP8 的**算法面**验证。

    换显示缩放等价于页面图像整体按比例放大/缩小：125%→150% 是 1.2×，100%→150% 是 1.5×。
    M1 的多尺度只覆盖 0.80~1.25，所以 1.5× 必须靠"按 DPI 比例预判尺度"才认得出来
    （一味扩大范围会让定位变慢，而 M2 要求单步 ≤500ms）。
    真机验证（真的把系统缩放改到 150% 再跑一批）需要用户授权，这里先把算法面的行为钉住。
    """

    @staticmethod
    def _scene(scale):
        page, _boxes = S.login_page()
        screen, rect = S.scene_of(page, 300, 150, scale, canvas_w=1900, canvas_h=1300)
        return page, screen, rect

    @staticmethod
    def _spec_with_dpi(page, rect, dpi):
        spec = S.page_spec_of(page, rect=rect)
        spec["capture_meta"] = dict(spec.get("capture_meta") or {}, dpi=dpi)
        return spec

    def test_same_dpi_uses_default_scales(self):
        """录制与运行同 DPI：走默认多尺度，行为与 M1 一致。"""
        from unittest import mock
        page, screen, rect = self._scene(1.0)
        spec = self._spec_with_dpi(page, rect, 120)
        with mock.patch("engine.capture.dpi_of", return_value=120):
            r = locator.locate_page(screen, spec)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_PAGE_TPL)
        self.assertNotIn("page_scales", r.get("detail") or {}, "同 DPI 不该启用预判")

    def test_dpi_150_without_hint_is_the_control(self):
        """对照组：1.5× 但**不**启用 DPI 预判 → 用来确认"预判到底是不是必要的"。

        实测结论：默认 0.80~1.25 也能命中（模板匹配对尺度偏差的容忍度比预期高，
        score 仍有 0.9x）。所以预判不是"救命"用的，它的价值在于**把跨 DPI 的匹配
        拉回正确的尺度上、避免在错误尺度上逼近阈值**；这组对照就是为了把这个事实固定下来，
        免得以后有人以为"没有预判就一定失败"。
        """
        from unittest import mock
        page, screen, rect = self._scene(1.5)
        spec = self._spec_with_dpi(page, rect, 96)
        with mock.patch("engine.capture.dpi_of", return_value=96):     # ratio=1.0 → 不预判
            r = locator.locate_page(screen, spec)
        self.assertNotIn("page_scales", r.get("detail") or {})
        self.assertTrue(r["ok"], f"对照组：默认尺度下 1.5× 其实也能命中 {r}")
        dev = max(abs(r["rect"][0] - rect[0]), abs(r["rect"][1] - rect[1]))
        self.assertLessEqual(dev, 12, f"对照组原点误差 {dev}")

    def test_dpi_100_to_150_uses_ratio_hint(self):
        """录制 96 DPI（100%）、运行时 144 DPI（150%）→ 页面被放大 1.5×（超出默认范围）。"""
        from unittest import mock
        page, screen, rect = self._scene(1.5)
        spec = self._spec_with_dpi(page, rect, 96)
        with mock.patch("engine.capture.dpi_of", return_value=144):
            r = locator.locate_page(screen, spec)
        self.assertTrue(r["ok"], f"1.5× 应靠 DPI 比例预判认出来 {r}")
        self.assertEqual(r["method"], locator.M_PAGE_TPL, r)
        self.assertIn("page_scales", r.get("detail") or {})
        dev = max(abs(r["rect"][0] - rect[0]), abs(r["rect"][1] - rect[1]))
        self.assertLessEqual(dev, 6, f"页面原点误差 {dev}")

    def test_dpi_125_to_150_uses_ratio_hint(self):
        """最常见的跨缩放：125% → 150%（1.2×），也在默认范围边缘，靠预判更稳。"""
        from unittest import mock
        page, screen, rect = self._scene(1.2)
        spec = self._spec_with_dpi(page, rect, 120)
        with mock.patch("engine.capture.dpi_of", return_value=144):
            r = locator.locate_page(screen, spec)
        self.assertTrue(r["ok"], r)
        dev = max(abs(r["rect"][0] - rect[0]), abs(r["rect"][1] - rect[1]))
        self.assertLessEqual(dev, 6, f"页面原点误差 {dev}")


class WidgetLocateTest(unittest.TestCase):
    def setUp(self):
        self.screen, self.rect, self.page, self.boxes = login_scene()
        self.page_spec = S.page_spec_of(self.page, rect=self.rect)

    def _target(self, key, **kw):
        bx = self.boxes[key]
        return S.widget_target(self.page, self.page_spec, bx, **kw)

    def test_text_first_level2_ocr(self):
        t = self._target("login_btn", text="登录")
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertTrue(r["ok"], f"定位失败 {r}")
        self.assertEqual(r["level"], 2)
        self.assertIn(r["method"], (locator.M_OCR_TEXT, locator.M_TPL))
        bx = self.boxes["login_btn"]
        exp_c = (self.rect[0] + bx[0] + bx[2] // 2, self.rect[1] + bx[1] + bx[3] // 2)
        dev = max(abs(r["center"][0] - exp_c[0]), abs(r["center"][1] - exp_c[1]))
        self.assertLessEqual(dev, 14, f"center={r['center']} exp={exp_c}")
        # 永不越出页面
        x, y, w, h = r["box"]
        self.assertGreaterEqual(x, self.rect[0])
        self.assertGreaterEqual(y, self.rect[1])
        self.assertLessEqual(x + w, self.rect[0] + self.rect[2])
        self.assertLessEqual(y + h, self.rect[1] + self.rect[3])

    def test_image_only_template(self):
        t = self._target("login_btn", text="", include_text=False, match="image_first")
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], locator.M_TPL)

    def test_out_of_page_decoy_not_chosen(self):
        """页面外有完全相同的部件图（负例诱饵）→ 仍只命中页内实例。"""
        t = self._target("login_btn", text="", include_text=False, match="image_first")
        crop = self.page[self.boxes["login_btn"][1]:self.boxes["login_btn"][1] +
                         self.boxes["login_btn"][3],
                         self.boxes["login_btn"][0]:self.boxes["login_btn"][0] +
                         self.boxes["login_btn"][2]]
        # 页面外 6 处诱饵
        for (dx, dy) in ((40, 20), (200, 20), (1200, 20), (40, 900), (900, 900), (40, 500)):
            h, w = crop.shape[:2]
            self.screen[dy:dy + h, dx:dx + w] = crop
        r = locator.locate_widget_on_screen(self.screen, self.rect, t)
        self.assertTrue(r["ok"])
        bx = self.boxes["login_btn"]
        exp = (self.rect[0] + bx[0], self.rect[1] + bx[1])
        dev = max(abs(r["box"][0] - exp[0]), abs(r["box"][1] - exp[1]))
        self.assertLessEqual(dev, 4, f"box={r['box']} 应在页内 {exp}（诱饵不应选中）")

    def test_page_coord_fallback_and_exists_exclusion(self):
        """② 识别不出（改版）→ ③ 页内坐标兜底；exists 语义下 ③ 不算“看到”。"""
        home1, boxes1 = S.home_page()
        home2, _ = S.home_page_v2()
        spec1 = S.page_spec_of(home1)
        # 旧部件：按 v1 采集的“库存查询”菜单（记录坐标 = v1 菜单位置）
        b1 = boxes1["menu"]
        t = S.widget_target(home1, spec1, b1, text="库存查询")
        # 现场是 v2：文字改名 + 侧栏色大变 → ② 应失败
        screen, rect = S.scene_of(home2, 200, 100, 1.0, canvas_w=CANVAS[0],
                                  canvas_h=CANVAS[1])
        # 存在性：不得以坐标兜底报“看到”
        rex = locator.locate_widget_on_screen(screen, rect, t, exists=True)
        self.assertFalse(rex["ok"])
        # 动作定位：② 失败退到 ③（几何兜底）
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], f"{r}")
        self.assertEqual(r["level"], 3)
        self.assertEqual(r["method"], locator.M_PAGE_COORD)

    def test_visibility_false_when_only_coords_survive(self):
        """部件确实不在（无文字无模板）→ exists 全级失败。"""
        screen, rect, page, _ = login_scene()
        spec = S.page_spec_of(page, rect=rect)
        t = S.widget_target(page, spec, [600, 560, 50, 24], text="绝无此物",
                            include_image=False)
        r = locator.locate_widget_on_screen(screen, rect, t, exists=True)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["level"])


class FilledInputLocateTest(unittest.TestCase):
    """输入框被填过数据（占位提示被顶替）→ 仍能定位（②c 环带路径）。

    真人反馈（2026-09-10）：录制时框里是空的（灰色占位提示），跑过一次后框里有了内容，
    占位文字没了、整块图案也对不上 → 只剩坐标 → 被点击安全闸拦下报"没找到"。
    """

    def test_locate_after_input_filled(self):
        page, boxes = S.login_page()
        spec = S.page_spec_of(page)
        bx = boxes["pwd_box"]                       # (120,365,400,60) 白底灰框输入框
        t = S.widget_target(page, spec, bx, text="")     # 不靠文字，只靠图
        # 现场：框里已被填了内容（占位提示消失）
        img = S.Image.fromarray(page[:, :, ::-1])
        S.draw_text(img, bx[0] + 24, bx[1] + 20, "wrong-pass", size=20, fill=(30, 30, 30))
        live = S.pil_to_bgr(img)
        screen, rect = S.scene_of(live, 300, 150, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])

        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], f"填入内容后定位失败：{r.get('detail')}")
        self.assertEqual(r["method"], locator.M_TPL_RING)
        exp = (rect[0] + bx[0] + bx[2] // 2, rect[1] + bx[1] + bx[3] // 2)
        dev = max(abs(r["center"][0] - exp[0]), abs(r["center"][1] - exp[1]))
        self.assertLessEqual(dev, 8, f"center={r['center']} exp={exp}")

    def test_exists_still_requires_strong_evidence(self):
        """存在性（如果看到）不认环带：内容与边框都变了就不能算"看到"。"""
        page, boxes = S.login_page()
        spec = S.page_spec_of(page)
        bx = boxes["pwd_box"]
        t = S.widget_target(page, spec, bx, text="")      # 只靠图，排除文字路径
        img = S.Image.fromarray(page[:, :, ::-1])
        S.ImageDraw.Draw(img).rectangle(
            (bx[0] + 2, bx[1] + 2, bx[0] + bx[2] - 2, bx[1] + bx[3] - 2), fill=(25, 25, 25))
        live = S.pil_to_bgr(img)
        screen, rect = S.scene_of(live, 300, 150, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        r = locator.locate_widget_on_screen(screen, rect, t, exists=True)
        self.assertFalse(r["ok"], f"exists 不该靠环带命中：{r.get('method')}")


class NearbyDisambiguationTest(unittest.TestCase):
    """同页多个相同文字 → 用"邻居文字"消歧；离录点太远的文字降级为兜底候选。

    刻意构造：B 离录点更近但没有邻居，A 稍远但有邻居 —— 用来验证"邻居优先于距离"。
    """

    @staticmethod
    def _scene():
        def deco(d, img):
            S.draw_text(img, 110, 108, "订单号", size=18)      # A 的邻居（A 上方）
            S.draw_text(img, 120, 150, "保存", size=22)        # A：带邻居
            S.draw_text(img, 230, 196, "保存", size=22)        # B：同名、离录点更近
        page = S.make_page(header_text="", w=1000, h=640, deco=deco)
        screen, rect = S.scene_of(page, 240, 120, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        return page, screen, rect

    def test_nearby_beats_distance(self):
        page, screen, rect = self._scene()
        spec = S.page_spec_of(page, rect=rect)
        box_a = S.widget_rect_of("保存", 120, 150, size=22)
        box_b = S.widget_rect_of("保存", 230, 196, size=22)
        nb = S.widget_rect_of("订单号", 110, 108, size=18)
        t = S.widget_target(page, spec, box_a, text="保存")
        # 录点故意放在 B 附近（更近），邻居却指向 A
        t["rect_in_page"] = [box_b[0], box_b[1], box_b[2], box_b[3]]
        t["center_in_page"] = [box_b[0] + box_b[2] // 2, box_b[1] + box_b[3] // 2]
        t["nearby"] = [{"text": "订单号", "rect_in_page": list(nb),
                        "offset": [nb[0] + nb[2] // 2 - (box_a[0] + box_a[2] // 2),
                                   nb[1] + nb[3] // 2 - (box_a[1] + box_a[3] // 2)]}]
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r.get("detail"))
        exp = (rect[0] + box_a[0] + box_a[2] // 2, rect[1] + box_a[1] + box_a[3] // 2)
        dev = max(abs(r["center"][0] - exp[0]), abs(r["center"][1] - exp[1]))
        self.assertLessEqual(dev, 12, f"没选邻居对得上的那个：center={r['center']} exp={exp}")
        # 候选留痕（用户审查要求）：默认记下 top3（盒/分数/距离/邻居）
        top3 = r["detail"]["l2_ocr"].get("top3")
        self.assertTrue(top3, f"应留下候选 top3：{r['detail']['l2_ocr']}")
        self.assertEqual(len(top3[0]), 4)
        scores = [c[1] for c in top3]
        self.assertEqual(scores, sorted(scores, reverse=True), f"top3 应按分数降序：{top3}")

    def test_far_text_is_demoted(self):
        """同名文字离录点太远 → 只是兜底候选，不当作主要证据直接采纳。

        录点放在页面中偏下（那里没有该文字），文字在左上 —— 距离超过 far_limit(160)，
        但仍落在放宽后的搜索带内，于是被收集为"远"候选。
        """
        page, screen, rect = self._scene()
        spec = S.page_spec_of(page, rect=rect)
        box_a = S.widget_rect_of("保存", 120, 150, size=22)
        t = S.widget_target(page, spec, box_a, text="保存", include_image=False)
        t["rect_in_page"] = [420, 300, 40, 26]
        t["center_in_page"] = [440, 313]
        r = locator.locate_widget_on_screen(screen, rect, t)
        d = r.get("detail", {}).get("l2_ocr", {})
        self.assertGreaterEqual(d.get("far", 0), 1, f"应识别出'远'候选：{d}")
        self.assertEqual(d.get("near", 0), 0, f"不该当成近候选：{d}")
        # 被明确标成"只有远候选" → 排在整块模板/环带之后，只在没有更强证据时才用
        self.assertIn("l2_ocr_far_only", r.get("detail", {}))


class PageCoordVerifyTest(unittest.TestCase):
    """③ 页内坐标兜底不再"纯几何"：用邻居文字复验，能验证就给出更高置信度（M2 WP5）。"""

    @staticmethod
    def _scene():
        """现场：部件本身换了样子（文字没了、图案也变了），但旁边的标签还在。"""
        page, boxes = S.login_page()
        bx = boxes["pwd_box"]
        nb = S.widget_rect_of("密码", 120, 330, size=22)     # 密码框上方的标签
        live = page.copy()
        live[bx[1] - 6:bx[1] + bx[3] + 6, bx[0] - 6:bx[0] + bx[2] + 6] = (60, 70, 90)
        screen, rect = S.scene_of(live, 300, 150, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        spec = S.page_spec_of(page, rect=rect)
        t = S.widget_target(page, spec, bx, text="绝无此词", include_image=True)
        t["nearby"] = [{"text": "密码", "rect_in_page": list(nb),
                        "offset": [nb[0] + nb[2] // 2 - (bx[0] + bx[2] // 2),
                                   nb[1] + nb[3] // 2 - (bx[1] + bx[3] // 2)]}]
        return screen, rect, t

    def test_coord_fallback_verified_by_nearby(self):
        screen, rect, t = self._scene()
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r.get("detail"))
        self.assertEqual(r["level"], 3, f"应走③兜底：{r.get('detail')}")
        l3 = (r.get("detail") or {}).get("l3") or {}
        self.assertTrue(l3.get("verified"), f"邻居还在，复验应通过：{l3}")
        self.assertEqual(l3.get("verify_method"), "nearby")
        self.assertEqual(r["method"], locator.M_PAGE_COORD)
        self.assertGreater(r["confidence"], 0.5)

    def test_coord_fallback_unverified_still_returns(self):
        """部件和邻居都没了：③ 仍返回（兜底），但如实标 verified=False。"""
        screen, rect, t = self._scene()
        t.pop("nearby", None)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], 3)
        self.assertFalse((r.get("detail", {}).get("l3") or {}).get("verified"),
                         f"不该谎报复验通过：{r.get('detail')}")
        self.assertEqual(r["confidence"], 0.5)


class WidgetLocateNoRectTest(unittest.TestCase):
    """无录点（只有文字）时的兜底搜索：页面下半区的提示文字也要能找到。"""

    @staticmethod
    def _page():
        page, _ = S.login_page()
        # 贴近页面底部的提示行（模拟登录失败的“密码错误，请重新输入”）
        img = S.Image.fromarray(page[:, :, ::-1])
        S.draw_text(img, 360, 596, "密码错误，请重新输入", size=20, fill=(200, 30, 30))
        return S.pil_to_bgr(img)

    def test_bottom_hint_found_without_recorded_rect(self):
        page = self._page()
        screen, rect = S.scene_of(page, 300, 150, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        t = {"text": "密码错误", "match": "text_first"}
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], f"下半区提示未找到 {r}")
        self.assertEqual(r["level"], 2)
        cy = r["center"][1]
        self.assertGreater(cy, rect[1] + rect[3] * 0.7, f"命中的不是底部提示：{r['center']}")

    def test_absent_text_still_fails(self):
        page = self._page()
        screen, rect = S.scene_of(page, 300, 150, 1.0, canvas_w=CANVAS[0], canvas_h=CANVAS[1])
        r = locator.locate_widget_on_screen(screen, rect,
                                            {"text": "工单已提交成功", "match": "text_first"})
        self.assertFalse(r["ok"])


class UiaLevelTest(unittest.TestCase):
    """①级 UI 树定位：页内约束 / 同名消歧 / 全页外时降级 ②。"""

    def setUp(self):
        self.screen, self.rect, self.page, self.boxes = login_scene()
        self.page_spec = S.page_spec_of(self.page, rect=self.rect)
        self.t = S.widget_target(self.page, self.page_spec, self.boxes["login_btn"],
                                 text="登录")

    def _btn_abs(self):
        bx = self.boxes["login_btn"]
        return (self.rect[0] + bx[0], self.rect[1] + bx[1], bx[2], bx[3])

    def test_uia_in_page_hit_with_disambiguation(self):
        """同名双命中：页内（近录点）与页外各一 → 选页内。"""
        far = (900, 900, 60, 40)             # 页外同名字面
        near = self._btn_abs()

        def provider(text):
            return [{"name": "登录", "rect": far,
                     "center": (far[0] + far[2] // 2, far[1] + far[3] // 2)},
                    {"name": "登录", "rect": near,
                     "center": (near[0] + near[2] // 2, near[1] + near[3] // 2)}]

        r = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                            uia_provider=provider)
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], 1)
        self.assertEqual(r["method"], locator.M_UIA)
        self.assertEqual(r["box"], near)
        self.assertEqual(r["detail"]["l1"]["in_page"], 1)

    def test_uia_all_out_of_page_falls_to_level2(self):
        """UI 树命中全部越出页面范围 → 本级弃用（约束铁律），走 ② 相似度。"""
        out = (60, 20, 60, 40)               # 页外

        def provider(text):
            return [{"name": "登录", "rect": out,
                     "center": (out[0] + out[2] // 2, out[1] + out[3] // 2)}]

        r = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                            uia_provider=provider)
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], 2)
        self.assertEqual(r["detail"]["l1"]["reason"], "no_hit_in_page")

    def test_uia_path_disambiguation(self):
        """同名多实例：**树路径对得上**的那个优先，即使它离录点更远（M2 WP4）。

        M1 只看距离（≤60px），遇到 Edge DOM 那类坐标偏差就弃用本级；有了路径，
        即使坐标飘了也能认准是哪一个。
        """
        screen, rect, page, boxes = login_scene()
        spec = S.page_spec_of(page, rect=rect)
        bx = boxes["login_btn"]
        t = S.widget_target(page, spec, bx, text="登录")
        t["uia"] = {"name": "登录", "automation_id": "btnLogin",
                    "path": ["表单区", "登录按钮"]}
        target_center = (rect[0] + bx[0] + bx[2] // 2, rect[1] + bx[1] + bx[3] // 2)

        def provider(_text):
            return [
                {"name": "登录", "rect": (target_center[0] + 130, target_center[1], 80, 40),
                 "center": (target_center[0] + 170, target_center[1] + 20),
                 "type": "ButtonControl", "automation_id": "",
                 "path": ["表单区", "登录按钮"]},          # 路径对得上，但很远（130px）
                {"name": "登录", "rect": (target_center[0] + 10, target_center[1], 80, 40),
                 "center": (target_center[0] + 50, target_center[1] + 20),
                 "type": "ButtonControl", "automation_id": "",
                 "path": ["工具栏", "其它"]},              # 很近，但路径对不上
            ]

        r = locator.locate_widget_on_screen(screen, rect, t, uia_provider=provider)
        self.assertTrue(r["ok"], r.get("detail"))
        self.assertEqual(r["level"], 1, f"应走①级（路径消歧）：{r.get('detail')}")
        self.assertGreaterEqual(r["detail"]["l1"].get("path_match", 0), 2)

    def test_uia_without_path_still_uses_distance(self):
        """老脚本（没记路径）行为不变：仍按距离选，太远仍弃用。"""
        screen, rect, page, boxes = login_scene()
        spec = S.page_spec_of(page, rect=rect)
        bx = boxes["login_btn"]
        t = S.widget_target(page, spec, bx, text="登录")      # 无 uia.path
        c = (rect[0] + bx[0] + bx[2] // 2 + 200, rect[1] + bx[1] + bx[3] // 2)

        def provider(_text):
            return [{"name": "登录", "rect": (c[0], c[1], 80, 40),
                     "center": (c[0] + 40, c[1] + 20), "type": "ButtonControl",
                     "automation_id": "", "path": ["某处"]}]

        r = locator.locate_widget_on_screen(screen, rect, t, uia_provider=provider)
        self.assertNotEqual(r.get("level"), 1, f"没路径又太远 → 不该用①级：{r.get('detail')}")

    def test_no_provider_skip_not_fail(self):
        r = locator.locate_widget_on_screen(self.screen, self.rect, self.t)
        self.assertEqual(r["level"], 2)
        self.assertEqual(r["detail"]["l1"]["reason"], "skip_no_provider_or_text")

    def test_exists_via_uia(self):
        """存在性（如果看到）也可由 ①级 命中判定。"""
        near = self._btn_abs()

        def provider(text):
            return [{"name": "登录", "rect": near,
                     "center": (near[0] + near[2] // 2, near[1] + near[3] // 2)}]

        r = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                            exists=True, uia_provider=provider)
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], locator.M_UIA)

    def test_whole_page_container_rejected(self):
        """整页容器（name=页面标题，盒≈整页，Edge DOM 实测形态）→ ①级弃用不误报。"""
        big = self.rect          # 盒 = 整页
        near = self._btn_abs()

        def provider(text):
            return [{"name": "M0 演示登录 · 示例公司门户", "rect": big,
                     "center": (big[0] + big[2] // 2, big[1] + big[3] // 2)},
                    {"name": "登录", "rect": near,
                     "center": (near[0] + near[2] // 2, near[1] + near[3] // 2)}]

        # exists：整页盒不能算“看到登录按钮”
        re = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                             exists=True, uia_provider=provider)
        self.assertTrue(re["ok"])
        self.assertEqual(re["method"], locator.M_UIA)
        self.assertEqual(re["box"], near)

        def provider_only_big(text):
            return [{"name": "页面标题", "rect": big,
                     "center": (big[0] + big[2] // 2, big[1] + big[3] // 2)}]

        # ①级无可用命中（整页容器被拒）→ exists 由 ②级 OCR 判“看到”，盒必须是按钮而非大盒
        re2 = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                              exists=True,
                                              uia_provider=provider_only_big)
        self.assertTrue(re2["ok"])
        self.assertEqual(re2["method"], locator.M_OCR_TEXT)
        self.assertLess(re2["box"][2], 300, "不得以大盒当作部件命中")
        # 动作定位同防护：①级无命中 → 落 ②
        r2 = locator.locate_widget_on_screen(self.screen, self.rect, self.t,
                                             uia_provider=provider_only_big)
        self.assertEqual(r2["level"], 2)
        self.assertEqual(r2["detail"]["l1"]["reason"], "no_hit_in_page")


if __name__ == "__main__":
    unittest.main()
