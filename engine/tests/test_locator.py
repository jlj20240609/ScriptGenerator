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
