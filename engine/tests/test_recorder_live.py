# -*- coding: utf-8 -*-
"""M3-WP1 真机接线的离线测试。

真机层里最容易出错、也最该被钉住的不是"钩子能否挂上"（那是环境问题），
而是几条格式约定：
  * 松开事件绝不能上报，否则「输入 demo」变成「输入 demodemo」
  * Ctrl+S 的控制字符必须还原成 's'，否则快捷键记成怪字符
  * 全屏帧是整虚拟屏，裁剪窗口前要减掉虚拟屏原点（多显示器会直接错位）

钩子本身用假按键对象直接驱动回调，不真挂监听、不碰屏幕。
"""
import unittest
from unittest import mock

import numpy as np

from engine import recorder as R
from engine import recorder_live as L


class KeyNameTest(unittest.TestCase):
    def test_special_keys(self):
        from pynput import keyboard
        self.assertEqual(L.key_name(keyboard.Key.enter), "enter")
        self.assertEqual(L.key_name(keyboard.Key.ctrl_l), "ctrl_l")
        self.assertEqual(L.key_name(keyboard.Key.space), "space")
        self.assertEqual(L.key_name(keyboard.Key.f5), "f5")
        self.assertEqual(R.classify_key(L.key_name(keyboard.Key.enter)), "func")
        self.assertEqual(R.classify_key(L.key_name(keyboard.Key.ctrl_l)), "modifier")

    def test_char_keys(self):
        from pynput import keyboard
        self.assertEqual(L.key_name(keyboard.KeyCode.from_char("a")), "a")
        self.assertEqual(L.key_name(keyboard.KeyCode.from_char("中")), "中")

    def test_unctrl_restores_letter(self):
        self.assertEqual(L.unctrl("\x13", ["ctrl"]), "s", "Ctrl+S 的控制字符要还原")
        self.assertEqual(L.unctrl("\x01", ["ctrl"]), "a")
        self.assertEqual(L.unctrl("s", []), "s", "没有 Ctrl 就原样")
        self.assertEqual(L.unctrl("中", ["ctrl"]), "中")


class HookerEventShapeTest(unittest.TestCase):
    """直接驱动钩子回调——不挂全局监听，也就不会真去监听用户的键盘。"""

    def setUp(self):
        self.hook = L.PynputHooker()
        self.events = []
        self.hook._cb = self.events.append

    def test_click_only_on_press(self):
        from pynput import mouse
        self.hook._on_click(100, 200, mouse.Button.left, True)
        self.hook._on_click(100, 200, mouse.Button.left, False)
        self.assertEqual(len(self.events), 1, "松开不能上报，否则点击会翻倍")
        self.assertEqual(self.events[0]["kind"], R.EV_CLICK)
        self.assertEqual((self.events[0]["x"], self.events[0]["y"]), (100, 200))
        self.assertEqual(self.events[0]["button"], "left")

    def test_scroll_shape(self):
        self.hook._on_scroll(5, 6, 0, -120)
        self.assertEqual(self.events[0]["kind"], R.EV_SCROLL)
        self.assertEqual(self.events[0]["dy"], -120)

    def test_char_release_emits_nothing(self):
        from pynput import keyboard
        self.hook._on_press(keyboard.KeyCode.from_char("a"))
        self.hook._on_release(keyboard.KeyCode.from_char("a"))
        self.assertEqual(len(self.events), 1, "松开必须静默，否则输入内容会翻倍")

    def test_modifier_state_tracks_release(self):
        from pynput import keyboard
        self.hook._on_press(keyboard.Key.ctrl_l)
        self.hook._on_press(keyboard.KeyCode.from_char("s"))
        self.assertEqual(self.events[-1]["mods"], ["ctrl"])
        self.assertEqual(self.events[-1]["key"], "s", "控制字符要还原")
        self.hook._on_release(keyboard.Key.ctrl_l)
        self.hook._on_press(keyboard.KeyCode.from_char("a"))
        self.assertEqual(self.events[-1]["mods"], [], "松开 Ctrl 后不该还带着修饰键")

    def test_hooker_output_feeds_pure_layer(self):
        """钩子产出的事件直接喂给纯逻辑层，必须得到「按快捷键 ctrl+s」这一块。"""
        from pynput import keyboard

        class _Left:
            name = "left"

        self.hook._on_click(10, 10, _Left(), True)
        self.hook._on_press(keyboard.Key.ctrl_l)
        self.hook._on_press(keyboard.KeyCode.from_char("\x13"))   # Ctrl+S
        self.hook._on_release(keyboard.KeyCode.from_char("\x13"))
        self.hook._on_release(keyboard.Key.ctrl_l)
        for i, e in enumerate(self.events):
            e["t"] = i * 0.1
        blocks = R.blocks_from_events(self.events)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_HOTKEY], blocks)
        self.assertEqual(blocks[-1]["params"]["keys"], "ctrl+s")

    def test_callback_errors_do_not_escape(self):
        def boom(_ev):
            raise RuntimeError("录制器炸了")
        self.hook._cb = boom
        from pynput import keyboard
        self.hook._on_press(keyboard.KeyCode.from_char("a"))   # 不应抛出


class StopHotkeyTest(unittest.TestCase):
    """停止热键：置位 stopped，且这个组合本身绝不进录制内容。"""

    def setUp(self):
        self.hook = L.PynputHooker(stop_combo="ctrl+alt+q")
        self.events = []
        self.hook._cb = self.events.append

    def test_default_combo_parsed(self):
        self.assertEqual(self.hook._stop_key, "q")
        self.assertEqual(self.hook._stop_mods, frozenset({"ctrl", "alt"}))

    def test_no_combo_means_never_stops(self):
        h = L.PynputHooker()
        self.assertEqual(h._stop_key, "")
        h._cb = self.events.append
        from pynput import keyboard
        h._on_press(keyboard.KeyCode.from_char("q"))
        self.assertEqual(len(self.events), 1, "没配停止热键时 q 应正常上报")

    def test_combo_sets_event_and_is_not_recorded(self):
        from pynput import keyboard
        self.hook._on_press(keyboard.Key.ctrl_l)
        self.hook._on_press(keyboard.Key.alt_l)
        before = len(self.events)
        self.hook._on_press(keyboard.KeyCode.from_char("q"))
        self.assertTrue(self.hook.stopped.is_set(), "按下停止热键应置位停止事件")
        self.assertEqual(len(self.events), before,
                         "停止热键本身不能上报，否则录制末尾会多一个垃圾积木")

    def test_q_alone_is_recorded(self):
        from pynput import keyboard
        self.hook._on_press(keyboard.KeyCode.from_char("q"))
        self.assertFalse(self.hook.stopped.is_set())
        self.assertEqual(self.events[-1]["key"], "q")

    def test_partial_mods_do_not_stop(self):
        from pynput import keyboard
        self.hook._on_press(keyboard.Key.ctrl_l)
        self.hook._on_press(keyboard.KeyCode.from_char("q"))   # 少了 alt
        self.assertFalse(self.hook.stopped.is_set())
        self.assertEqual(self.events[-1]["key"], "q")


class FrameCropTest(unittest.TestCase):
    """全屏帧是整虚拟屏；多显示器时原点是负的，不减就会整体错位。"""

    def setUp(self):
        self.pages = L.LivePages(ocr=False)

    def test_offset_applied(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        frame[30:40, 50:60] = 7
        with mock.patch.object(L.capture, "virtual_screen_rect",
                               return_value=(-10, -20, 200, 100)):
            got = self.pages._crop_from_frame(frame, (-10 + 50, -20 + 30, 10, 10))
        self.assertIsNotNone(got, "在虚拟屏内的窗口应能裁出来")
        self.assertEqual(got.shape[:2], (10, 10))
        self.assertTrue((got == 7).all(), "裁的位置必须对得上（减掉虚拟屏原点）")

    def test_shape_mismatch_falls_back(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        with mock.patch.object(L.capture, "virtual_screen_rect",
                               return_value=(0, 0, 999, 999)):
            self.assertIsNone(self.pages._crop_from_frame(frame, (0, 0, 10, 10)),
                              "帧的实际尺寸和虚拟屏对不上时不能硬裁")

    def test_none_frame(self):
        self.assertIsNone(self.pages._crop_from_frame(None, (0, 0, 10, 10)))


class WidgetPickTest(unittest.TestCase):
    """OCR 反查：点到文字上就取那块文字，认不出就退到以点击点为中心的框。"""

    def setUp(self):
        self.pages = L.LivePages()
        self.shape = (600, 800, 3)
        self.toks = [("登录", (300, 200, 60, 24), 0.99),
                     ("用户名", (100, 200, 80, 24), 0.98),
                     ("密码", (100, 240, 60, 24), 0.97),
                     ("确定", (300, 400, 60, 30), 0.96)]

    def test_pick_token_containing_point(self):
        box, text = self.pages._pick(self.toks, 310, 210, self.shape)
        self.assertEqual(text, "登录")
        x, y, w, h = box
        self.assertTrue(x <= 300 and y <= 200 and x + w >= 360 and y + h >= 224,
                        f"应包住整块文字并留边：{box}")

    def test_pick_smallest_containing_token(self):
        toks = [("外层容器", (100, 100, 300, 100), 0.9),
                ("确定", (150, 130, 40, 20), 0.99)]
        box, text = self.pages._pick(toks, 160, 140, self.shape)
        self.assertEqual(text, "确定", "点在嵌套文字上时应取最小的那块")

    def test_pick_nearest_within_radius(self):
        box, text = self.pages._pick(self.toks, 297, 212, self.shape)   # 紧贴「登录」左侧
        self.assertEqual(text, "登录")

    def test_pick_far_point_falls_back(self):
        box, text = self.pages._pick(self.toks, 700, 580, self.shape)
        self.assertIsNone(box, "离所有文字都很远时不该硬认一个")

    def test_fallback_box_is_click_centered(self):
        box = self.pages._fallback_box(400, 300, self.shape)
        x, y, w, h = box
        self.assertEqual((x + w // 2, y + h // 2), (400, 300))
        self.assertEqual(box, self.pages._fallback_box(400, 300, self.shape))

    def test_fallback_box_clamps_at_edges(self):
        box = self.pages._fallback_box(2, 2, self.shape)
        x, y, w, h = box
        self.assertEqual((x, y), (0, 0))
        self.assertTrue(w > 0 and h > 0)

    def test_nearby_only_same_row(self):
        n = self.pages._nearby(self.toks, (300, 200, 60, 24), self.shape)
        texts = [x["text"] for x in n]
        self.assertIn("用户名", texts, "同一行的邻居要收进来")
        self.assertNotIn("密码", texts, "换行的不算同行")
        self.assertNotIn("登录", texts, "目标自己不算邻居")
        me = [x for x in n if x["text"] == "用户名"][0]
        self.assertLess(me["offset"][0], 0, "左侧邻居的偏移应为负")

    def test_nearby_empty(self):
        self.assertIsNone(self.pages._nearby([], (0, 0, 10, 10), self.shape))

    def test_page_embedded_only_on_first_and_page_change(self):
        p = L.LivePages()
        p._cur_page_key = ("hwnd", (0, 0, 10, 10))
        self.assertTrue(p._should_embed_page({"a": 1}), "第一块必须带页面")
        self.assertFalse(p._should_embed_page({"a": 1}), "同页后续动作靠执行器继承")
        p._cur_page_key = ("hwnd", (0, 0, 20, 20))
        self.assertTrue(p._should_embed_page({"a": 1}), "换页了必须重新内嵌")


if __name__ == "__main__":
    unittest.main()
