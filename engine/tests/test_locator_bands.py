# -*- coding: utf-8 -*-
"""整页兜底带的行为：只跑一次、且覆盖整页（2026-09-13）。

为什么钉住：以前整页兜底带按搜索点居中，于是每个搜索点各扫一次半页，
而两次窗口合起来只覆盖页面 y∈[156,754]（998 高的页面里约 60%）——
目标落在窗口之外时这一级**永远扫不到**（不是慢，是漏）：表现不是"报失败"，
而是"退到 ③ 盲点坐标、点错地方"。

`smoke/diag/_edge_bands_probe.py` 实测（页高 988、录点 y=400）：
目标在 y=60 / y=900 时旧条带一次都碰不到；整页兜底上线后两处都命中，
落点偏差 1~2px。这条测试保证：整页扫描至多真正付一次时间，而且那一次覆盖整页。
"""
import unittest

from engine import locator, matcher
from engine.tests import support as S

PAGE_W, PAGE_H = 1000, 988
REC_Y = 400                       # 录制时目标在页面中部
TEXT_SIZE = 24


def _spy_ocr(testcase):
    """记录**真正付了时间**的 OCR 调用（内容缓存命中约 0ms，不算）。

    为什么不按毫秒阈值判：OCR 快慢随机器的负载在 30ms~4s 之间浮动（同一条 1000x988
    实测 0.46s ~ 4.1s），用"耗时 > 200ms 才算真跑"会在快机器上把两次真跑都判成缓存命中，
    测试就变成永远通过的摆设。数调用次数不受机器快慢影响。
    """
    calls = []
    orig = matcher.ocr_run

    def spy(bgr, *a, **kw):
        r = orig(bgr, *a, **kw)
        if not r.get("cached"):
            calls.append((int(bgr.shape[1]), int(bgr.shape[0])))
        return r

    matcher.ocr_run = spy
    testcase.addCleanup(lambda: setattr(matcher, "ocr_run", orig))
    return calls


def _page_with_button_at(y):
    """一页（988 高）：目标文字「立即刷新」摆在指定纵向位置，其余内容不动。"""
    boxes = {}

    def deco(d, img):
        S.draw_text(img, 60, 30, "工单台 · 巡检页", size=20, fill=(120, 120, 120))
        S.draw_text(img, 60, 300, "工单编号", size=22)
        boxes["btn"] = S.draw_text(img, 120, y, "立即刷新", size=TEXT_SIZE,
                                   fill=(30, 60, 130))

    return S.make_page(w=PAGE_W, h=PAGE_H, deco=deco), boxes


class FullPageFallbackTest(unittest.TestCase):
    def setUp(self):
        matcher.ocr_cache_clear()          # 别的用例留下的缓存会让"真跑次数"失真

    def test_page_wide_band_is_paid_once_and_covers_page(self):
        page, boxes = S.login_page()
        h, w = page.shape[:2]
        spec = S.page_spec_of(page, rect=(0, 0, w, h))
        # 用页面上绝对不存在的词 → 强制逐级升级，直到整页兜底
        tgt = S.widget_target(page, spec, boxes["login_btn"], text="绝无此物XYZ")
        calls = _spy_ocr(self)
        res = locator.locate_widget_on_screen(page, (0, 0, w, h), tgt, exists=False)
        bands = (res.get("detail") or {}).get("l2_ocr_bands") or []
        self.assertTrue(bands, "应当留下逐带明细")
        wide = [b for b in bands if (b["w"], b["h"]) == (w, h)]
        self.assertTrue(wide, f"整页兜底带必须覆盖整页，实际逐带："
                              f"{[(b['w'], b['h']) for b in bands]}")
        paid = [c for c in calls if c == (w, h)]
        self.assertEqual(len(paid), 1,
                         f"整页兜底带最多只能真正跑一次，实际 {len(paid)} 次：{calls}")


class PageEdgeTargetTest(unittest.TestCase):
    """目标跑到页面上下缘时也必须找得到（旧条带覆盖不到的那一段）。

    旧条带在这页上的纵向并集是 [156,754]（见模块说明与 `_edge_bands_probe.py`）：
    目标在 y=60 / y=900 时 ②a 一个条带都碰不到它 → 只能靠 ③ 盲点坐标，点错地方。
    """

    @staticmethod
    def _locate(run_y):
        rec_page, boxes = _page_with_button_at(REC_Y)
        run_page, _ = _page_with_button_at(run_y)
        screen, rect = S.scene_of(run_page, 200, 60, 1.0,
                                  canvas_w=1400, canvas_h=1180)
        spec = S.page_spec_of(rec_page, rect=rect)
        tgt = S.widget_target(rec_page, spec, list(boxes["btn"]), text="立即刷新")
        return (locator.locate_widget_on_screen(screen, rect, tgt, exists=False),
                rect, run_y)

    def _assert_found_at(self, run_y):
        res, rect, y = self._locate(run_y)
        self.assertTrue(res["ok"], f"没定位出来：{res}")
        self.assertEqual(res["level"], 2, f"退到盲点坐标层了：{res}")
        self.assertEqual(res["method"], locator.M_OCR_TEXT, f"{res}")
        exp_cy = rect[1] + y + int(TEXT_SIZE * 1.35) // 2
        self.assertLessEqual(abs(res["center"][1] - exp_cy), 15,
                             f"落点 {res['center']} 期望 y={exp_cy}")
        rec_cy = rect[1] + REC_Y + int(TEXT_SIZE * 1.35) // 2
        self.assertGreater(abs(res["center"][1] - rec_cy), 260,
                           "用例前提：目标离录点足够远，旧条带够不着它")

    def test_target_at_page_top(self):
        self._assert_found_at(60)

    def test_target_at_page_bottom(self):
        self._assert_found_at(PAGE_H - 88)


if __name__ == "__main__":
    unittest.main()
