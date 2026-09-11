# -*- coding: utf-8 -*-
"""M3-WP2 录制会话的测试：界面要的行为，用假录制器离线钉住。

界面侧的契约就四件事：能开始、能实时看到积木在长、停止能拿到步骤、用户按了
停止热键时界面能自己收到通知（否则用户录完走了，界面还在转圈）。
"""
import threading
import time
import unittest

from engine import record_session as RS
from engine import recorder as R


class _FakeHooker:
    def __init__(self):
        self.stopped = threading.Event()
        self._stop_mods = frozenset({"ctrl", "alt"})
        self._stop_key = "q"

    def stop(self):
        pass


class _FakeRec:
    """假录制器：测试自己往里灌事件，不必碰屏幕也不必有钩子。"""

    def __init__(self, events=None):
        self.events = events if events is not None else []   # 共用同一个列表
        self.hooker = _FakeHooker()
        self.grab_stats = {"n": 0, "ms_total": 0.0, "failed": 0}
        self.elapsed_s = 0.0
        self.started = False
        self.stopped = False
        self.discarded = False
        self.steps_calls = 0

    def start(self):
        self.started = True
        return {"ok": True, "note": "开始录制"}

    def stop(self):
        self.stopped = True
        blocks = R.blocks_from_events(self.events)
        return {"ok": True, "blocks": blocks, "summary": R.summarize(blocks),
                "elapsed_s": 3.0}

    def steps(self, page_provider=None):
        self.steps_calls += 1
        blocks = R.blocks_from_events(self.events)
        return {"ok": True, "steps": [{"id": f"r{i}", "type": "action",
                                       "action": b["action"], "params": {}}
                                      for i, b in enumerate(blocks, 1)],
                "skipped": [], "notes": [], "summary": R.summarize(blocks)}

    def discard(self):
        self.discarded = True
        return {"ok": True, "note": "已放弃本次录制"}


def _click(t, x=10, y=20):
    return {"t": t, "kind": "click", "x": x, "y": y, "button": "left"}


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.pushed = []
        self.rec = _FakeRec(self.events)
        self.sess = RS.RecordSession(notify=lambda m, p: self.pushed.append((m, dict(p))),
                                     make_recorder=lambda: self.rec, poll_s=0.05)

    def _names(self):
        return [m for m, _ in self.pushed]

    def test_start_pushes_started_with_hotkey(self):
        res = self.sess.start()
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.rec.started)
        self.assertIn("event.record.started", self._names())
        args = [p for m, p in self.pushed if m == "event.record.started"][0]
        self.assertEqual(args["hotkey"], "ctrl+alt+q",
                         "界面要能告诉用户怎么停（他在别的窗口里）")

    def test_second_start_refused(self):
        self.sess.start()
        res = self.sess.start()
        self.assertFalse(res["ok"])
        self.assertIn("已经在录制", res["note"])

    def test_live_blocks_are_pushed(self):
        self.sess.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and not self._names():
            time.sleep(0.02)                    # 等 started
        self.events.append(_click(0.0))
        blocks = self._wait_for("event.record.block")
        self.assertIsNotNone(blocks, "录制中应把新积木推给界面")
        self.assertEqual(blocks["action"], R.B_CLICK)
        self.assertEqual(blocks["text"], "点一下")
        self.assertFalse(blocks["update"])

    def test_tail_block_update_is_pushed(self):
        """打字是一块在长的积木：界面要能收到同一块的更新，而不是一直等。"""
        self.events.append(_click(0.0))
        self.sess.start()
        self._wait_for("event.record.block")
        self.events.append({"t": 0.2, "kind": "key", "key": "d", "mods": []})
        self._wait_for("event.record.block", text="输入文字「d」")
        got = [p for m, p in self.pushed
               if m == "event.record.block" and p.get("update")]
        self.assertTrue(got, "同一块内容变了要推更新")
        self.assertEqual(got[-1]["text"], "输入文字「d」")
        self.assertEqual(got[-1]["index"], 1, "更新的是第 2 块（打字块）")

    def test_stop_returns_steps_once(self):
        self.events.extend([_click(0.0), _click(3.0)])
        self.sess.start()
        first = self.sess.stop()
        self.assertEqual([s["action"] for s in first["steps"]], ["click", "wait", "click"])
        self.assertEqual(self.rec.steps_calls, 1)
        again = self.sess.stop()
        self.assertEqual(again["steps"], first["steps"], "重复停止返回同一份结果")
        self.assertEqual(self.rec.steps_calls, 1, "不该重复做一遍 OCR")

    def test_hotkey_auto_stops_and_notifies(self):
        """用户按停止热键时他人在别的窗口，界面必须自己收到 done。"""
        self.events.append(_click(0.0))
        self.sess.start()
        self.rec.hooker.stopped.set()
        done = self._wait_for("event.record.done")
        self.assertIsNotNone(done, "按下停止热键后界面应收到 done")
        self.assertEqual(done["reason"], "hotkey")
        self.assertFalse(self.sess.recording)
        self.assertTrue(self.rec.stopped)

    def test_cancel_discards(self):
        self.events.append(_click(0.0))
        self.sess.start()
        res = self.sess.cancel()
        self.assertTrue(res["ok"])
        self.assertTrue(self.rec.discarded)
        self.assertFalse(self.sess.recording)
        self.assertIn("event.record.cancelled", self._names())
        self.assertIsNone(self.sess.result, "放弃的录制不该留下结果")

    def test_status_reports_growth(self):
        self.sess.start()
        st = self.sess.status()
        self.assertTrue(st["recording"])
        self.assertEqual(st["blocks"], [])
        self.events.extend([_click(0.0), _click(3.0)])
        st = self.sess.status()
        self.assertEqual([b["action"] for b in st["blocks"]],
                         [R.B_CLICK, R.B_WAIT, R.B_CLICK])
        self.assertEqual(st["summary"]["total"], 3)
        self.sess.stop()
        st = self.sess.status()
        self.assertFalse(st["recording"])
        self.assertIsNotNone(st["result"])

    def test_stop_without_recording(self):
        self.assertFalse(self.sess.stop()["ok"])
        self.assertFalse(self.sess.cancel()["ok"])

    def test_make_recorder_failure_is_reported(self):
        def boom():
            raise RuntimeError("pynput 起不来")

        sess = RS.RecordSession(notify=lambda m, p: None, make_recorder=boom)
        res = sess.start()
        self.assertFalse(res["ok"])
        self.assertIn("录制器起不来", res["note"])
        self.assertFalse(sess.recording)

    # ---- 工具

    def _wait_for(self, method, text=None, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m, p in self.pushed:
                if m == method and (text is None or p.get("text") == text):
                    return p
            time.sleep(0.02)
        return None


if __name__ == "__main__":
    unittest.main()
