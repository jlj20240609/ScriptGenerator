# -*- coding: utf-8 -*-
"""M3-WP1 录制引擎的纯逻辑测试：事件流 → 积木。

不碰屏幕、不碰钩子（这正是把"事件 → 积木"做成纯函数的原因）——录制的可用性
几乎全在这一步：打字必须聚合、修饰键不能凭空生块、停顿要变成"等一下"。
"""
import unittest

from engine import recorder as R
from engine import schema


def _click(t, x, y, double=False):
    return {"t": t, "kind": "click", "x": x, "y": y, "button": "left", "double": double}


def _key(t, key, mods=(), at=None):
    return {"t": t, "kind": "key", "key": key, "mods": list(mods), "at": at}


class TypeAggregationTest(unittest.TestCase):
    """连续字符必须聚合成**一块**「输入文字」——一个字一个积木的脚本没法用。"""

    def test_consecutive_chars_one_block(self):
        ev = [_click(0.0, 300, 200)] + [
            _key(0.2 + i * 0.08, c) for i, c in enumerate("demo")]
        blocks = R.blocks_from_events(ev)
        types = [b for b in blocks if b["action"] == R.B_TYPE]
        self.assertEqual(len(types), 1, blocks)
        self.assertEqual(types[0]["params"]["text"], "demo")
        self.assertEqual(types[0]["at"], (300, 200),
                         "打字块的位置应沿用最近一次点击（输入框）")

    def test_pause_splits_type_blocks(self):
        ev = [_click(0.0, 10, 10),
              _key(0.2, "a"), _key(0.3, "b"),
              _key(2.0, "c")]                       # 停顿 1.7s > 0.9s → 切块
        blocks = R.blocks_from_events(ev)
        types = [b["params"]["text"] for b in blocks if b["action"] == R.B_TYPE]
        self.assertEqual(types, ["ab", "c"], blocks)

    def test_type_not_content_of_shortcut(self):
        """带修饰键的字母不算输入内容（Ctrl+S 是快捷键，不是"输入 s"）。"""
        ev = [_click(0.0, 10, 10), _key(0.2, "x"),
              _key(0.4, "ctrl"), _key(0.45, "s", mods=["ctrl"])]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks if b["action"] != R.B_WAIT],
                         [R.B_CLICK, R.B_TYPE, R.B_HOTKEY], blocks)
        self.assertEqual(blocks[-1]["params"]["keys"], "ctrl+s")


class ModifierTest(unittest.TestCase):
    """光按下 Ctrl/Shift 本身不能生出积木（否则每次组合键都多两块垃圾）。"""

    def test_bare_modifier_produces_nothing(self):
        ev = [_click(0.0, 10, 10), _key(0.2, "ctrl"), _key(0.25, "shift"),
              _key(0.3, "a")]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_TYPE], blocks)

    def test_classify(self):
        self.assertEqual(R.classify_key("a"), "char")
        self.assertEqual(R.classify_key("中"), "char")
        self.assertEqual(R.classify_key("ctrl_l"), "modifier")
        self.assertEqual(R.classify_key("enter"), "func")
        self.assertEqual(R.classify_key("F5".lower()), "func")
        self.assertEqual(R.classify_key(""), "unknown")


class ClickTest(unittest.TestCase):
    def test_double_click_merges(self):
        ev = [_click(0.0, 500, 400), _click(0.15, 501, 400)]   # 同点、间隔 0.15s
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_DBLCLICK], blocks)
        self.assertEqual(blocks[0]["at"], (501, 400))

    def test_two_slow_clicks_stay_separate(self):
        ev = [_click(0.0, 500, 400), _click(1.0, 500, 400)]    # 间隔 1.0s > 0.35s
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_CLICK], blocks)

    def test_clicks_at_different_places_stay_separate(self):
        ev = [_click(0.0, 100, 100), _click(0.1, 900, 700)]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_CLICK], blocks)


class WaitInsertionTest(unittest.TestCase):
    """操作之间的无感停顿要自动变成「等一下」——文档明确要求。"""

    def test_gap_becomes_wait(self):
        ev = [_click(0.0, 10, 10), _click(3.0, 20, 20)]        # 停 3 秒
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks],
                         [R.B_CLICK, R.B_WAIT, R.B_CLICK], blocks)
        self.assertAlmostEqual(blocks[1]["params"]["seconds"], 3.0, places=1)

    def test_no_wait_for_fast_sequence(self):
        ev = [_click(0.0, 10, 10), _click(0.5, 20, 20)]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_CLICK], blocks)


class UnsupportedAndSummaryTest(unittest.TestCase):
    def test_scroll_is_recorded_but_marked_unsupported(self):
        """滚轮暂时没有对应动作（schema.ACTIONS 里没有）→ 如实留痕，不硬塞。"""
        ev = [_click(0.0, 10, 10),
              {"t": 0.5, "kind": "scroll", "x": 100, "y": 200, "dy": -120}]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks], [R.B_CLICK, R.B_UNSUPPORTED], blocks)
        self.assertIn("滚轮", R.describe(blocks[-1]))

    def test_summarize_and_describe(self):
        ev = [_click(0.0, 10, 10), _key(0.2, "a"), _key(0.3, "b"),
              {"t": 0.6, "kind": "scroll", "x": 1, "y": 2, "dy": -1}]
        blocks = R.blocks_from_events(ev)
        s = R.summarize(blocks)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["usable"], 2, "滚轮那块不算可用")
        self.assertEqual(s["unsupported"], 1)
        self.assertEqual(R.describe(blocks[0]), "点一下")
        self.assertEqual(R.describe([b for b in blocks if b["action"] == R.B_TYPE][0]),
                         "输入文字「ab」")


class RealisticSequenceTest(unittest.TestCase):
    """一段接近真实操作的序列：点输入框 → 打字 → 点按钮 → 停顿 → 快捷键。"""

    def test_end_to_end_shape(self):
        ev = [_click(0.0, 400, 300),
              _key(0.3, "d"), _key(0.38, "e"), _key(0.46, "m"), _key(0.54, "o"),
              _click(1.0, 520, 460),
              _key(4.0, "ctrl"), _key(4.05, "s", mods=["ctrl"])]
        blocks = R.blocks_from_events(ev)
        self.assertEqual([b["action"] for b in blocks],
                         [R.B_CLICK, R.B_TYPE, R.B_CLICK, R.B_WAIT, R.B_HOTKEY], blocks)
        self.assertEqual(blocks[1]["params"]["text"], "demo")
        self.assertEqual(blocks[1]["at"], (400, 300), "打字块沿用输入框位置")
        self.assertEqual(blocks[4]["params"]["keys"], "ctrl+s")
        self.assertEqual(R.summarize(blocks)["usable"], 5)


class _FakeHooker:
    """假钩子：把回调留住，测试自己往里灌事件。"""

    def __init__(self):
        self.cb = None
        self.started = 0
        self.stopped = 0

    def start(self, cb):
        self.cb = cb
        self.started += 1

    def stop(self):
        self.stopped += 1


class _FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class RecorderSkeletonTest(unittest.TestCase):
    """录制器的骨架：钩子/抓屏/取部件全部注入，所以不必真录屏也能钉住行为。"""

    def _rec(self, **kw):
        hook, clock = _FakeHooker(), _FakeClock(100.0)
        r = R.Recorder(hooker=hook, clock=clock, **kw)
        self.assertTrue(r.start()["ok"], "注入钩子后应能开始录制")
        return r, hook, clock

    def test_no_hooker_refuses(self):
        r = R.Recorder()
        res = r.start()
        self.assertFalse(res["ok"])
        self.assertIn("监听", res["note"])
        self.assertFalse(r.running)

    def test_events_get_relative_time(self):
        r, hook, clock = self._rec()
        clock.t = 100.5
        hook.cb({"kind": "click", "x": 10, "y": 20, "button": "left"})
        clock.t = 101.25
        hook.cb({"kind": "key", "key": "a", "mods": []})
        clock.t = 105.0
        res = r.stop()
        self.assertTrue(res["ok"])
        self.assertEqual([b["action"] for b in res["blocks"]],
                         [R.B_CLICK, R.B_TYPE],
                         "收尾时的停顿不该生成「等一下」——后面已经没有动作了")
        self.assertEqual(res["summary"]["waits"], 0)
        self.assertAlmostEqual(r.events[0]["t"], 0.5, places=3,
                               msg="事件时间应是相对录制开始的秒数")
        self.assertEqual(res["elapsed_s"], 5.0)
        self.assertEqual(hook.stopped, 1)

    def test_frame_grabbed_once_per_point(self):
        """同一位置连点两次只抓一帧——录制时抓屏必须便宜。"""
        grabbed = []

        def grabr():
            grabbed.append(1)
            return "bgr"

        r, hook, clock = self._rec(grabr=grabr)
        hook.cb({"kind": "click", "x": 5, "y": 6})
        clock.t = 100.2
        hook.cb({"kind": "click", "x": 5, "y": 6})
        clock.t = 100.4
        hook.cb({"kind": "click", "x": 7, "y": 8})
        r.stop()
        self.assertEqual(len(grabbed), 2, "两个不同点位各抓一次")

    def test_stop_twice_is_safe(self):
        r, hook, clock = self._rec()
        r.stop()
        self.assertFalse(r.stop()["ok"])

    def test_steps_need_widget_for_clicks(self):
        """没有取部件能力时，点击块不许生成一个假装能用的步骤。"""
        r, hook, clock = self._rec()
        hook.cb({"kind": "click", "x": 10, "y": 20})
        clock.t = 103.0
        hook.cb({"kind": "key", "key": "ctrl"})
        clock.t = 103.05
        hook.cb({"kind": "key", "key": "s", "mods": ["ctrl"]})
        res = r.steps()
        self.assertEqual([s["action"] for s in res["steps"]], ["wait", "hotkey"],
                         res)
        self.assertEqual(res["steps"][0]["id"], "r1")
        self.assertEqual(res["steps"][1]["params"]["keys"], "ctrl+s")
        self.assertEqual(len(res["skipped"]), 1, "点击块应被跳过而不是硬造")
        self.assertIn("部件", "".join(res["notes"]))

    def test_steps_build_target_from_widget(self):
        r, hook, clock = self._rec(
            page_of=lambda x, y: ((0, 0, 100, 100), "page", {"title": "t"}),
            widget_of=lambda x, y, rect, bgr, spec: {"locator": {"text": "确定"},
                                                     "rect_in_page": [1, 2, 3, 4]})
        hook.cb({"kind": "click", "x": 30, "y": 40})
        clock.t = 100.3
        hook.cb({"kind": "key", "key": "o", "mods": []})
        clock.t = 100.4
        hook.cb({"kind": "key", "key": "k", "mods": []})
        res = r.steps()
        self.assertEqual([s["action"] for s in res["steps"]], ["click", "type"])
        self.assertEqual(res["steps"][0]["target"]["locator"]["text"], "确定")
        self.assertEqual(res["steps"][1]["params"]["text"], "ok")
        self.assertEqual(res["steps"][1]["target"]["locator"]["text"], "确定",
                         "打字块用最近点击的输入框当目标")
        self.assertFalse(res["skipped"])

    def test_unsupported_never_becomes_step(self):
        r, hook, clock = self._rec()
        hook.cb({"kind": "click", "x": 1, "y": 2})
        clock.t = 100.5
        hook.cb({"kind": "scroll", "x": 1, "y": 2, "dy": -120})
        res = r.steps()
        self.assertEqual([s["action"] for s in res["steps"]], [])
        self.assertEqual(len(res["skipped"]), 2)
        self.assertIn("滚轮", "".join(res["notes"]))

    def test_page_provider_failure_is_noted(self):
        def boom(x, y):
            raise RuntimeError("没有窗口")

        r, hook, clock = self._rec(page_of=boom,
                                   widget_of=lambda *a: {"locator": {}})
        hook.cb({"kind": "click", "x": 1, "y": 2})
        res = r.steps()
        self.assertFalse(res["steps"])
        self.assertIn("没认出来", "".join(res["notes"]))


class RecordedScriptIsSchemaLegalTest(unittest.TestCase):
    """录制产出的步骤必须直接过 schema 校验——否则「录完能回放」就是空话。

    这条测试是本 WP 的验收线：造一个模拟真实录制的场景（点输入框 → 打字 →
    点按钮 → 等 → 快捷键），让录制器出步骤，再原样塞进脚本跑 schema。
    """

    _PNG = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
            "q842iQAAAABJRU5ErkJggg==")

    def _widget_of(self, x, y, rect, bgr, spec):
        # 真机实现会用 OCR 反查点到的文字；这里固定一个，验证的是形状而非识别率
        return schema.widget_target(
            page=schema.page_spec(image_dataurl=self._PNG, size=(800, 600),
                                  context={"title": "记事本"},
                                  rect_in_screen=list(rect)),
            image_dataurl=self._PNG, text="保存", rect_in_page=[10, 10, 60, 24],
            center_in_page=[40, 22],
            nearby=[{"text": "文件", "rect_in_page": [0, 0, 40, 20],
                     "offset": [-20, 22]}])

    def test_steps_pass_schema_validation(self):
        hook, clock = _FakeHooker(), _FakeClock(0.0)
        r = R.Recorder(hooker=hook, clock=clock,
                       page_of=lambda x, y: ((100, 50, 800, 600), "bgr", {}),
                       widget_of=self._widget_of)
        self.assertTrue(r.start()["ok"])
        hook.cb({"kind": "click", "x": 400, "y": 300})
        for i, ch in enumerate("demo"):
            clock.t = 0.3 + i * 0.08
            hook.cb({"kind": "key", "key": ch, "mods": []})
        clock.t = 1.0
        hook.cb({"kind": "click", "x": 520, "y": 460})
        clock.t = 4.0
        hook.cb({"kind": "key", "key": "ctrl"})
        clock.t = 4.05
        hook.cb({"kind": "key", "key": "s", "mods": ["ctrl"]})

        res = r.steps()
        self.assertEqual([s["action"] for s in res["steps"]],
                         ["click", "type", "click", "wait", "hotkey"], res["notes"])
        self.assertEqual(res["steps"][1]["params"]["text"], "demo")
        self.assertFalse(res["skipped"], res["notes"])

        sg = schema.new_script("录制测试")
        sg["steps"] = res["steps"]
        problems = schema.validate(sg)
        self.assertEqual(problems, [], "录制的步骤必须直接过 schema 校验")
        self.assertIn("保存", schema.dump(sg), "应能原样写盘")

    def test_dump_is_json_roundtrippable(self):
        import json
        hook, clock = _FakeHooker(), _FakeClock(0.0)
        r = R.Recorder(hooker=hook, clock=clock,
                       page_of=lambda x, y: ((0, 0, 10, 10), "bgr", {}),
                       widget_of=self._widget_of)
        r.start()
        hook.cb({"kind": "click", "x": 1, "y": 1})
        sg = schema.new_script("t")
        sg["steps"] = r.steps()["steps"]
        again = json.loads(schema.dump(sg))
        self.assertEqual(again["steps"][0]["action"], "click")
        self.assertEqual(again["steps"][0]["target"]["text"], "保存")


if __name__ == "__main__":
    unittest.main()
