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


class _SilentAI(A.SemanticStub):
    """会说"找不到"的语义桩：给粗圈，但问它"现在叫什么"时明确拒绝。

    用来模拟"AI 也判不出来"的真实情况——这是低置信人工兜底路径的触发条件。
    """

    def ask_rename(self, old_widget, screen_bgr, semantic, options=None):
        return {"ok": False, "text": "", "reason": "absent",
                "note": "语义桩：说找不到", "elapsed_ms": 0.0}


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
        """彻底改名（无共同前缀）**且 AI 也说找不到** → 校准失败，旧值完整保留（人工兜底）。

        注意"AI 也说找不到"这个前提：语义桩现在能从**候选词**里挑出改名后的字
        （这正是真实云端的行为，见 engine/ai.py 的 RENAME_PROMPT_OPTIONS），
        所以只用 `aliases=[]` 已经不足以模拟"AI 判不出来"了——要显式让它拒绝回答。
        """
        renamed, _ = S.erp_v2_renamed_page()
        canvas, rect = _canvas_of(renamed)
        cal = C.Calibrator(S.FakeDriver(lambda: canvas), ai=_SilentAI(aliases=[]))
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
    """彻底改版（erp_v1 → 左右分栏新版）：整窗模板与特征都失配 → 语义粗圈 + 整窗重采集恢复。

    注意这里用的是 `erp_redesigned_page` 而不是 `erp_v2_page`：后者（暖色改版）在
    M2 加了 ORB 特征兜底之后已经能被正确定位（左上偏差 3px，是真命中），拿它当
    "页面找不到"的前置已经不成立，会让这两条恢复测试悄悄测不到该测的路径。
    """

    def setUp(self):
        self.v1, b1 = S.erp_v1_page()
        self.v2, b2 = S.erp_redesigned_page()
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
        self.assertGreaterEqual(len(anchors), 2, "M2-WP2：应写回多个分散锚（共识需要互相印证）")
        rects = [a["rect_in_page"] for a in anchors]
        self.assertEqual(len({tuple(r) for r in rects}), len(rects), f"锚不该重复: {rects}")
        self.assertIn("stable_at", anchors[0])
        self.assertNotIn("_stable", anchors[0], "临时字段不应落盘")
        self.assertIn("anchors", str(res.get("detail", {})))
        # 后续帧（内容又变）：整窗失配 → 锚定位还原页面原点
        self.scene.next_frame()
        screen = self.scene.screen()
        r = locator.locate_page(screen, self.spec)
        self.assertTrue(r["ok"], f"锚应恢复页面定位 {r}")
        self.assertEqual(r["method"], locator.M_ANCHOR)
        self.assertGreaterEqual(r.get("consensus", 0), 1, f"应走上多锚共识路径 {r}")
        dev = max(abs(r["rect"][0]), abs(r["rect"][1]))
        self.assertLessEqual(dev, 2, f"页面原点还原 dev={dev}")
        # 部件在锚还原页内定位（顶栏词未变）
        w = locator.locate_widget_on_screen(screen, r["rect"], self.target)
        self.assertTrue(w["ok"], f"部件应可定位 {w}")
        self.assertEqual(self.target["text"], "热门")

    def test_dynamic_page_keeps_recorded_template(self):
        """动态页校准后**不该**把当前帧写成整窗模板。

        否则下一轮又是新画面 → 又失配 → 又校准，每轮都要打扰用户，锚永远用不上
        （实测：feed 案例每轮 2 次定位、锚 0 次）。
        这里用"每次抓屏都前进一帧"的帧提供器——真实动态页是**自己在变**的，
        而普通的合成 provider 只有被推一下才变，那样会被判成"页面稳定"。
        """
        scene = S.FeedScene(w=self.W, h=self.H, top_h=46)
        spec = S.page_spec_of(scene.screen(), rect=(0, 0, self.W, self.H))
        target = scene.widget_target("热门", page_spec=spec)
        scene.next_frame()
        scene.next_frame()
        cal = C.Calibrator(S.FakeDriver(scene.next_frame), ai=A.SemanticStub(),
                           window_rect=lambda: (0, 0, self.W, self.H), anchor_dt_s=0.0)
        recorded_image = spec["image"]
        res = cal({"reason": "page_not_found", "target": target, "page_spec": spec,
                   "page_rect": None, "loc_rows": [], "ctx": {}})
        self.assertTrue(res["ok"], res)
        self.assertEqual(spec["image"], recorded_image,
                         "动态页的整窗模板应保持录制帧不变")
        self.assertIn("dynamic", str((spec.get("capture_meta") or {}).get("calib", "")),
                      "重采集结果要标注清楚这次是动态页（没换整窗模板）")
        self.assertGreaterEqual(len(spec.get("anchors") or []), 1, "动态页要留下锚")
        # 后续帧：整窗仍失配 → 必须由锚接管
        r = locator.locate_page(scene.next_frame(), spec)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_ANCHOR, f"应交由锚定位 {r}")

    def test_static_page_still_recaptures(self):
        """静态改版页：整窗稳定，照旧换新模板（别把 M1 的改版恢复路径改坏了）。"""
        page, boxes = S.erp_v1_page()
        spec = S.page_spec_of(page)
        tgt = S.widget_target(page, spec, boxes["menu"], text="库存查询")
        canvas, rect = _canvas_of(page)
        cal = C.Calibrator(S.FakeDriver(lambda: canvas), ai=A.SemanticStub(),
                           window_rect=lambda: rect)
        res = cal({"reason": "page_not_found", "target": tgt, "page_spec": spec,
                   "page_rect": None, "loc_rows": [], "ctx": {}})
        if res.get("updated"):
            self.assertNotIn("dynamic", str((spec.get("capture_meta") or {}).get("calib", "")),
                             "静态页不该走「保留旧模板」的分支")

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
        # 本用例只关心"语义改名能恢复"；锚的**数量/分散性**由 AnchorBandPickTest 覆盖
        self.assertGreaterEqual(len(spec2.get("anchors") or []), 1)


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


class AnchorBandPickTest(unittest.TestCase):
    """M2-WP2 多锚采集：候选条带的挑选（纯几何/纹理）+ 多锚只抓一帧。"""

    W, H = 1100, 760

    @staticmethod
    def _banded_page(w=1100, h=760):
        """合成"顶栏 + 中部工具条 + 底栏三块静态横带、其余留白"的页面。

        真实场景里稳定的正是这类横带（导航/工具条/状态栏）；横带之间的内容区才是动态的。
        """
        img = Image.new("RGB", (w, h), (250, 250, 252))
        d = ImageDraw.Draw(img)
        for y in (0, h // 2 - 20, h - 44):
            d.rectangle((0, y, w, y + 44), fill=(226, 232, 240))
            for i in range(6):                       # 一排按钮，制造纹理
                x = 20 + i * 170
                d.rectangle((x, y + 8, x + 120, y + 36), fill=(60, 90, 140))
        return S.pil_to_bgr(img)

    def _cal(self, provider):
        return C.Calibrator(S.FakeDriver(provider), ai=A.SemanticStub(),
                            window_rect=lambda: (0, 0, self.W, self.H), anchor_dt_s=0.0)

    def test_bands_dispersed_and_inside_page(self):
        page = self._banded_page(self.W, self.H)
        bands = self._cal(lambda: page)._anchor_bands(page, box=[20, 8, 120, 36])
        self.assertGreaterEqual(len(bands), 2, f"应挑出多个分散锚 {bands}")
        ys = sorted(b[1] for b in bands)
        self.assertGreater(ys[-1] - ys[0], self.H * 0.4, f"锚应垂直分散 {ys}")
        for b in bands:
            self.assertGreaterEqual(b[2], C.ANCHOR_BAND_MIN[0])
            self.assertTrue(0 <= b[0] and 0 <= b[1], b)
            self.assertLessEqual(b[0] + b[2], self.W, b)
            self.assertLessEqual(b[1] + b[3], self.H, b)

    def test_flat_page_no_anchor(self):
        """纯色页面：低纹理块在整屏搜索时会到处匹配 → 一个锚都不要。"""
        flat = np.full((self.H, self.W, 3), 200, np.uint8)
        self.assertEqual(self._cal(lambda: flat)._anchor_bands(flat, box=[20, 8, 120, 36]), [])

    def test_multi_anchor_grabs_single_frame(self):
        """多锚只抓一次第二帧（M1 每个锚各抓一次，锚越多越慢）。"""
        page = self._banded_page(self.W, self.H)
        calls = []

        def _provider():
            calls.append(1)
            return page

        cal = self._cal(_provider)
        got = cal._collect_anchors(page, [20, 8, 120, 36], (0, 0, self.W, self.H), {})
        self.assertGreaterEqual(len(got), 2, got)
        self.assertEqual(len(calls), 1, f"多锚应复用同一帧，实际抓了 {len(calls)} 次")
        self.assertGreaterEqual(got[0]["_stable"], C.ANCHOR_STABLE_MIN)

    def test_overlapping_candidates_dropped(self):
        """与主锚重叠过多的候选要丢掉（重叠的锚提供不了独立证据）。"""
        page = self._banded_page(self.W, self.H)
        cal = self._cal(lambda: page)
        wide_box = [0, 0, self.W, 44]            # 部件框占满整条顶栏 → 主锚已覆盖顶行
        bands = cal._anchor_bands(page, box=wide_box)
        main = bands[0]
        for b in bands[1:]:
            ov = C._overlap_ratio(b, main)
            self.assertLessEqual(ov, C.ANCHOR_OVERLAP_MAX + 1e-6, f"{b} 与主锚重叠 {ov:.2f}")


if __name__ == "__main__":
    unittest.main()
