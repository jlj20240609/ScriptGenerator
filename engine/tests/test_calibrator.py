# -*- coding: utf-8 -*-
"""E5 calibrator：校验模式子流程（合成屏离线；AI=语义桩模拟云端改名语义）。"""
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from engine import ai as A
from engine import calibrator as C
from engine import locator, matcher
from engine.tests import support as S

PX, PY = 300, 150
CANVAS = (1500, 1050)


def _canvas_of(page):
    scr = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
    rect = S.paste_scale(page, scr, PX, PY, 1.0)
    return scr, rect


class WidgetLostCalibTest(unittest.TestCase):
    """小改版（ERP v1→v2_small）：页面模板仍命中，部件文字+模板双失配 → 部件级恢复。"""

    def setUp(self):
        self.v1, b1 = S.erp_v1_page()
        self.v2, b2 = S.erp_v2_small_page()
        self.v2_canvas, self.v2_rect = _canvas_of(self.v2)
        self.spec1 = S.page_spec_of(self.v1)
        # 旧目标（v1 采集：库存查询 菜单）
        self.target = S.widget_target(self.v1, self.spec1, b1["menu"], text="库存查询")
        # 预检：v2 屏上页面模板应仍命中；旧部件“可见性”（exists）应失败
        pg = locator.locate_page(self.v2_canvas, self.spec1)
        assert pg["ok"], "前置：v2_small 屏上 v1 模板应命中"
        w = locator.locate_widget_on_screen(self.v2_canvas, pg["rect"], self.target,
                                            exists=True)
        assert not w["ok"], "前置：旧部件在 v2_small 上应不可见（改名+样式变）"
        self.cal = C.Calibrator(S.FakeDriver(lambda: self.v2_canvas),
                                ai=A.SemanticStub(aliases=["库存中心"]))

    def test_recapture_and_rewrite(self):
        """真实主路径：动作定位退到旧坐标（L3）→ 点击闸拦下（click_guard_failed）
        → 校准器圈区重采集改名部件。"""
        req = {"reason": "click_guard_failed", "target": self.target,
               "page_spec": self.spec1, "page_rect": self.v2_rect,
               "loc_rows": [], "ctx": {}}
        res = self.cal(req)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["updated"], res)
        self.assertEqual(self.target["text"], "库存中心")
        # 新模板可解码且为新菜单样式区域
        new_img = matcher.dataurl_to_bgr(self.target["image"])
        self.assertGreater(new_img.shape[1], 60)
        b2_menu = S.erp_v2_small_page()[1]["menu"]
        exp = (b2_menu[0] - C.PAD, b2_menu[1] - C.PAD)
        dev = max(abs(self.target["rect_in_page"][0] - exp[0]),
                  abs(self.target["rect_in_page"][1] - exp[1]))
        self.assertLessEqual(dev, 14, f"rect={self.target['rect_in_page']} exp={exp}")
        # 写回后自检已通过（页面+部件都可定位）
        self.assertIn("自检", res["note"])

    def test_absent_keeps_old_values(self):
        """彻底改名（无共同前缀）且无别名 → 校准失败，旧值完整保留（人工兜底）。"""
        renamed, _ = S.erp_v2_renamed_page()
        canvas, rect = _canvas_of(renamed)
        cal = C.Calibrator(S.FakeDriver(lambda: canvas),
                           ai=A.SemanticStub(aliases=[]))
        old_img = self.target["image"]
        old_text = self.target["text"]
        res = cal({"reason": "click_guard_failed", "target": self.target,
                   "page_spec": self.spec1, "page_rect": rect,
                   "loc_rows": [], "ctx": {}})
        self.assertFalse(res["ok"])
        self.assertFalse(res["updated"])
        self.assertEqual(self.target["image"], old_img)
        self.assertEqual(self.target["text"], old_text)

    def test_widget_not_found_reason_same_path(self):
        """widget_not_found 入口（罕见：页内越界/几何异常）与 guard 同一条恢复路径。"""
        req = {"reason": "widget_not_found", "target": self.target,
               "page_spec": self.spec1, "page_rect": self.v2_rect,
               "loc_rows": [], "ctx": {}}
        res = self.cal(req)
        self.assertTrue(res["ok"] and res["updated"], res)


class PageLostCalibTest(unittest.TestCase):
    """大改版（erp_v1 → erp_v2 暖色系）：整窗模板失配 → 语义粗圈 + 整窗重采集恢复。"""

    def setUp(self):
        self.v1, b1 = S.erp_v1_page()
        self.v2, b2 = S.erp_v2_page()
        self.v2_canvas, self.v2_rect = _canvas_of(self.v2)
        self.spec1 = S.page_spec_of(self.v1)
        self.target = S.widget_target(self.v1, self.spec1, b1["menu"], text="库存查询")
        pg = locator.locate_page(self.v2_canvas, self.spec1)
        assert not pg["ok"], "前置：v2 大改版屏上 v1 整窗模板应失配"
        self.cal = C.Calibrator(S.FakeDriver(lambda: self.v2_canvas),
                                ai=A.SemanticStub(aliases=["库存中心"]),
                                window_rect=lambda: self.v2_rect)

    def test_page_recapture_restores(self):
        req = {"reason": "page_not_found", "target": self.target,
               "page_spec": self.spec1, "page_rect": None,
               "loc_rows": [], "ctx": {}}
        res = self.cal(req)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["updated"], res)
        self.assertEqual(self.target["text"], "库存中心")
        # 页面整窗重采集：新模板 = v2 现场页（静态页行带稳定 → 会顺带写回冗余锚）
        new_page = matcher.dataurl_to_bgr(self.spec1["image"])
        self.assertEqual(new_page.shape[:2], (640, 1000))
        self.assertIn("page_recapture",
                      self.spec1["capture_meta"].get("calib", ""))
        # 自检：新模板+新部件在 v2 屏定位
        pg = locator.locate_page(self.v2_canvas, self.spec1)
        self.assertTrue(pg["ok"], "重采集后页面应可定位")
        w = locator.locate_widget_on_screen(self.v2_canvas, pg["rect"], self.target)
        self.assertTrue(w["ok"], f"重采集后部件应可定位 {w}")

    def test_no_window_rect_reports_manual(self):
        cal = C.Calibrator(S.FakeDriver(lambda: self.v2_canvas),
                           ai=A.SemanticStub(aliases=["库存中心"]),
                           window_rect=None)
        res = cal({"reason": "page_not_found", "target": self.target,
                   "page_spec": self.spec1, "page_rect": None,
                   "loc_rows": [], "ctx": {}})
        self.assertFalse(res["ok"])
        self.assertIn("窗口矩形", res["note"])


class FeedAnchorCalibTest(unittest.TestCase):
    """强动态页（bili feed 型合成）：整窗每帧失配 → 语义定位顶栏词 →
    行带锚跨帧复验写回 → 后续帧仅靠锚定位页面与部件（dev=0px 无人工）。"""

    W, H = 1100, 760

    def setUp(self):
        self.scene = S.FeedScene(w=self.W, h=self.H, top_h=46)
        # 录制帧（帧 0）：页面模板 + 顶栏“热门”部件
        self.spec = S.page_spec_of(self.scene.screen(), rect=(0, 0, self.W, self.H))
        self.target = self.scene.widget_target("热门", page_spec=self.spec)
        # 主区变化 ×2（整窗模板应失配）
        self.scene.next_frame()
        self.scene.next_frame()
        pg = locator.locate_page(self.scene.screen(), self.spec)
        assert not pg["ok"], "前置：动态帧上旧整窗模板应失配"
        self.cal = C.Calibrator(S.FakeDriver(self.scene.provider),
                                ai=A.SemanticStub(),
                                window_rect=lambda: (0, 0, self.W, self.H),
                                anchor_dt_s=0.0)

    def test_feed_anchor_recapture_restores(self):
        req = {"reason": "page_not_found", "target": self.target,
               "page_spec": self.spec, "page_rect": None,
               "loc_rows": [], "ctx": {}}
        res = self.cal(req)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["updated"], res)
        anchors = self.spec.get("anchors") or []
        self.assertEqual(len(anchors), 1, "应写回静态锚")
        self.assertIn("stable_at", anchors[0])
        self.assertIn("_stable", str(res.get("detail", {})))
        # 后续帧（内容又变）：整窗失配 → 锚定位还原页面原点
        self.scene.next_frame()
        screen = self.scene.screen()
        r = locator.locate_page(screen, self.spec)
        self.assertTrue(r["ok"], f"锚应恢复页面定位 {r}")
        self.assertEqual(r["method"], locator.M_ANCHOR)
        dev = max(abs(r["rect"][0]), abs(r["rect"][1]))
        self.assertLessEqual(dev, 2, f"页面原点还原 dev={dev}")
        # 部件在锚还原页内定位（顶栏词未变）
        w = locator.locate_widget_on_screen(screen, r["rect"], self.target)
        self.assertTrue(w["ok"], f"部件应可定位 {w}")
        self.assertEqual(self.target["text"], "热门")

    def test_feed_widget_moved_name_changed(self):
        """动态页 + 顶栏词改名（直播→新词）→ 别名语义恢复。"""
        scene2 = S.FeedScene(w=self.W, h=self.H, top_h=46)
        spec2 = S.page_spec_of(scene2.screen())
        # 顶栏词位置同但词不同：热门→精选
        boxes = scene2.boxes
        bx = boxes["热门"]
        # 自定义改名帧提供器：把顶栏“热门”涂掉换成“精选”（其余顶栏同）
        class _Renamed:
            def __init__(self, base):
                self.base = base
                self.renamed = False

            def screen(self):
                img = self.base.screen().copy()
                if self.renamed:
                    x, y = bx[0], bx[1]
                    img[y - 4:y + bx[3] + 4, x - 4:x + bx[2] + 4] = (226, 232, 240)
                    from PIL import Image, ImageDraw
                    pil = Image.fromarray(img[:, :, ::-1])
                    d = ImageDraw.Draw(pil)
                    f = ImageFont.truetype(S.FONT_PATH, 20)
                    d.text((x, y), "精选", font=f, fill=(25, 40, 70))
                    img = __import__("numpy").asarray(pil)[:, :, ::-1].copy()
                return img

            def provider(self):
                return self.screen()

        base = S.FeedScene(w=self.W, h=self.H, top_h=46)
        ren = _Renamed(base)
        ren.renamed = True
        tgt = base.widget_target("热门", page_spec=spec2)
        cal = C.Calibrator(S.FakeDriver(ren.provider),
                           ai=A.SemanticStub(aliases=["精选"]),
                           window_rect=lambda: (0, 0, self.W, self.H),
                           anchor_dt_s=0.0)
        res = cal({"reason": "page_not_found", "target": tgt,
                   "page_spec": spec2, "page_rect": None,
                   "loc_rows": [], "ctx": {}})
        self.assertTrue(res["ok"] and res["updated"], res)
        self.assertEqual(tgt["text"], "精选")
        self.assertEqual(len(spec2.get("anchors") or []), 1)


class FirstRunTest(unittest.TestCase):
    def test_first_run_valid_target(self):
        v1, b1 = S.erp_v1_page()
        canvas, rect = _canvas_of(v1)
        spec = S.page_spec_of(v1)
        t = S.widget_target(v1, spec, b1["menu"], text="库存查询")
        cal = C.Calibrator(S.FakeDriver(lambda: canvas),
                           ai=A.SemanticStub(aliases=[]))
        res = cal({"reason": "first_run", "target": t, "page_spec": spec,
                   "page_rect": None, "loc_rows": [], "ctx": {}})
        self.assertTrue(res["ok"])
        self.assertFalse(res["updated"])

    def test_first_run_missing_target_noop(self):
        cal = C.Calibrator(S.FakeDriver(lambda: _canvas_of(S.login_page()[0])[0]),
                           ai=A.SemanticStub())
        res = cal({"reason": "first_run", "target": None, "page_spec": None,
                   "page_rect": None, "loc_rows": [], "ctx": {}})
        self.assertTrue(res["ok"])
        self.assertFalse(res["updated"])


if __name__ == "__main__":
    unittest.main()
