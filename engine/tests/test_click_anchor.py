# -*- coding: utf-8 -*-
"""点击点落在"用户框的那块"上，而不是"文字上"（2026-09-13）。

背景（用户反馈 + 实录）：OCR 文字识别在定位里占大头，脚本运行时就点文字的中心；
而文字常常不是有效点击部位 —— 实录 `app/blibli测试脚本.json`：
  · 输入框：录制的部件框 429x79，文字框只有 154x22，**点击点偏 125px**；
  · 评论正文区：425x39 vs 303x21，偏 48px。
离线 A/B（`smoke/diag/_pref_ab.py`，录制页面）：有部件图的 7 步，模板**全部命中、分 1.0**，
命中框 = 录制部件框（偏差 0px）；而文字优先时 s7 偏 125px、s6 偏 48px。

所以默认偏好（`match: auto`）改为：**有部件图 → 图像优先**（文字只当"目标还在这一带"
的线索，②b 会额外在文字候选附近找一次外形），没有图（一句话生成那类）→ 文字优先。

这组用例钉住四件事：
  1. 默认下有图目标点的是**部件框中心**，不是文字框中心；
  2. 页面整体位移后，仍靠"文字指路 + 局部外形"点到新位置（`l2_tpl_anchor == "text"`）；
  3. 外形认到别处（离文字很远）时**否掉模板**，不让它把点击点带跑；
  4. 无图目标与显式 `text_first` 的老行为不变。
"""
import unittest

from engine import locator
from engine.tests import support as S

W, H = 1000, 640
BOX_X, BOX_W, BOX_H = 100, 600, 60
BOX_Y = 300                      # 录制时输入框的纵向位置
TEXT_DX, TEXT_DY = 12, 18        # 框内文字相对框左上角的偏移
TEXT = "评论内容一段"
TEXT_SIZE = 22


def _page(box_y=BOX_Y, text=TEXT, decoy=False):
    """一页：一个输入框（可整体位移）+ 框内左侧一行文字；decoy=True 时原位再放一个空壳块。"""
    boxes = {}

    def deco(d, img):
        S.draw_text(img, 40, 40, "工单台 · 巡检页", size=20, fill=(120, 120, 120))
        if decoy:                                  # 原位留一个"长得像但不含目标文字"的块
            d.rectangle((BOX_X, BOX_Y, BOX_X + BOX_W, BOX_Y + BOX_H),
                        fill=(252, 252, 254), outline=(150, 155, 165))
            S.draw_text(img, BOX_X + TEXT_DX, BOX_Y + TEXT_DY, "另一行别的内容", size=TEXT_SIZE)
        d.rectangle((BOX_X, box_y, BOX_X + BOX_W, box_y + BOX_H),
                    fill=(255, 255, 255), outline=(150, 155, 165))
        if text:
            boxes["text"] = S.draw_text(img, BOX_X + TEXT_DX, box_y + TEXT_DY, text,
                                        size=TEXT_SIZE, fill=(20, 20, 20))
        boxes["box"] = (BOX_X, box_y, BOX_W, BOX_H)

    return S.make_page(w=W, h=H, deco=deco), boxes


def _scene(page):
    return S.scene_of(page, 100, 80, 1.0, canvas_w=1400, canvas_h=1000)


class ClickAnchorTest(unittest.TestCase):
    """默认（auto + 有部件图）= 图像优先：点击点落在部件框中心。"""

    @staticmethod
    def _target(rec_page, box, text=TEXT, match=None, with_image=True):
        spec = S.page_spec_of(rec_page, rect=(100, 80, W, H))
        t = S.widget_target(rec_page, spec, box, text=text, include_image=with_image)
        if match:
            t["match"] = match
        return t

    def test_default_clicks_widget_center_not_text_center(self):
        rec_page, boxes = _page()
        t = self._target(rec_page, boxes["box"])
        screen, rect = _scene(rec_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_TPL, f"默认应走图像（部件模板）：{r}")
        exp = (rect[0] + BOX_X + BOX_W // 2, rect[1] + BOX_Y + BOX_H // 2)
        self.assertLessEqual(max(abs(r["center"][0] - exp[0]), abs(r["center"][1] - exp[1])), 8,
                             f"点击点应在部件框中心 {exp}，实际 {r['center']}")
        # 文字框中心离得很远 —— 正是用户抱怨的"点在文字上"的那个位置
        tx, ty, tw, th = boxes["text"]
        text_c = (rect[0] + tx + tw // 2, rect[1] + ty + th // 2)
        self.assertGreater(max(abs(r["center"][0] - text_c[0]), abs(r["center"][1] - text_c[1])),
                           100, f"这次不该落在文字中心 {text_c}")

    def test_explicit_text_first_keeps_old_behavior(self):
        """显式 text_first 的老脚本：仍然点文字框（行为不变，便于回退比对）。"""
        rec_page, boxes = _page()
        t = self._target(rec_page, boxes["box"], match="text_first")
        screen, rect = _scene(rec_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_OCR_TEXT, f"{r}")
        tx, ty, tw, th = boxes["text"]
        text_c = (rect[0] + tx + tw // 2, rect[1] + ty + th // 2)
        self.assertLessEqual(max(abs(r["center"][0] - text_c[0]), abs(r["center"][1] - text_c[1])),
                             12, f"text_first 应落在文字框中心 {text_c}，实际 {r['center']}")

    def test_no_image_target_stays_text_first(self):
        """没有部件图的目标（一句话生成那类）→ 文字优先，与 M1 行为一致。"""
        rec_page, boxes = _page()
        t = self._target(rec_page, boxes["box"], with_image=False)
        screen, rect = _scene(rec_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_OCR_TEXT, f"{r}")

    def test_image_only_target_unchanged(self):
        """纯图片目标（无文字）：一直就是模板路径，行为不变。"""
        rec_page, boxes = _page()
        t = self._target(rec_page, boxes["box"], text="", with_image=True)
        screen, rect = _scene(rec_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_TPL, f"{r}")

    def test_page_shifted_template_found_near_text(self):
        """页面整体下移：录点附近已经没有目标 → 靠"文字指路"在那附近找外形并点准。"""
        rec_page, boxes = _page(box_y=BOX_Y)
        run_page, run_boxes = _page(box_y=BOX_Y + 200)
        t = self._target(rec_page, boxes["box"])
        screen, rect = _scene(run_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["method"], locator.M_TPL, f"应拿到部件框而不是文字框：{r}")
        self.assertEqual((r.get("detail") or {}).get("l2_tpl_anchor"), "text",
                         f"这次的外形应当是在「文字候选附近」找到的：{r.get('detail')}")
        by = BOX_Y + 200
        exp = (rect[0] + BOX_X + BOX_W // 2, rect[1] + by + BOX_H // 2)
        self.assertLessEqual(max(abs(r["center"][0] - exp[0]), abs(r["center"][1] - exp[1])), 8,
                             f"点击点应在移动后的部件框中心 {exp}，实际 {r['center']}")

    def test_decoy_near_record_does_not_win(self):
        """录点附近留了个"长得像"的块、目标跑到别处 → 点击点必须跟着真实目标走。

        这条同时钉住两个机制：① 模板除了"录点附近"还会在**文字候选附近**找一次；
        ② 两个窗口都命中时，"罩得住目标文字"的那个优先（光比分数会被诱饵骗到）。
        """
        rec_page, boxes = _page(box_y=BOX_Y)
        run_page, run_boxes = _page(box_y=BOX_Y + 200, decoy=True)
        t = self._target(rec_page, boxes["box"])
        screen, rect = _scene(run_page)
        r = locator.locate_widget_on_screen(screen, rect, t)
        d = r.get("detail") or {}
        self.assertTrue(r["ok"], r)
        # 诱饵在录点处（y≈BOX_Y），真实目标在下方 200px —— 落点必须靠近后者
        self.assertGreater(r["center"][1] - rect[1], BOX_Y + 120,
                           f"不能点到录点处的诱饵：{r['center']}，detail={d}")
        # 若（万一）选中了录点窗口的框，那条"文字不在这块里"的否掉记录必须留痕
        if d.get("l2_tpl_anchor") == "record":
            self.assertIn("l2_tpl_text_outside", d, f"诱饵应当被判为认错地方：{d}")


if __name__ == "__main__":
    unittest.main()
