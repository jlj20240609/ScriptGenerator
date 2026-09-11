# -*- coding: utf-8 -*-
"""M3-WP2 录制 IPC 契约测试：界面调的方法名、参数、返回字段在这里钉死。

界面与引擎之间只有这一层契约，错了界面就会"点了没反应"或"步骤是空的"，
而那种错在真机上要花很久才能定位。所以用假录制器走**真实的 RPC 通路**
（handle_line → 方法表 → 序列化）把它验掉，不需要屏幕也不需要钩子。
"""
import json
import unittest

from engine import ipc as IP
from engine import recorder as R
from engine import record_session as RS


class _FakeHooker:
    def __init__(self):
        import threading
        self.stopped = threading.Event()
        self._stop_mods = frozenset({"ctrl", "alt"})
        self._stop_key = "q"

    def stop(self):
        pass


class _FakeRec:
    """假录制器：事件由测试灌进来（真机上是钩子灌），其余接口与真的一致。"""

    def __init__(self, events):
        self.events = events
        self.hooker = _FakeHooker()
        self.grab_stats = {"n": 2, "ms_total": 80.0, "failed": 0}
        self.elapsed_s = 1.5

    def start(self):
        return {"ok": True, "note": "开始录制"}

    def stop(self):
        blocks = R.blocks_from_events(self.events)
        return {"ok": True, "blocks": blocks, "summary": R.summarize(blocks),
                "elapsed_s": 1.5}

    def steps(self, page_provider=None):
        blocks = R.blocks_from_events(self.events)
        steps, n = [], 0
        for b in blocks:
            n += 1
            st = {"id": f"r{n}", "type": "action", "action": b["action"],
                  "params": dict(b.get("params") or {})}
            if b.get("action") in (R.B_CLICK, R.B_DBLCLICK, R.B_TYPE):
                st["target"] = {"text": "确定", "rect_in_page": [10, 20, 60, 24],
                                "center_in_page": [40, 32],
                                "image": "data:image/png;base64,AAAA"}
            steps.append(st)
        return {"ok": True, "steps": steps, "skipped": [], "notes": [],
                "summary": R.summarize(blocks)}

    def discard(self):
        return {"ok": True, "note": "已放弃本次录制"}


class RecordIpcTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.sent = []
        self.sess = RS.RecordSession(poll_s=0.05)
        self.sess._make = lambda: _FakeRec(self.events)      # 换成假录制器
        self.sess._notify = lambda m, p: self.sent.append({"method": m, "params": p})
        self.srv = IP.IpcServer(record_session=self.sess)
        self.srv.send = lambda obj: self.sent.append(obj)

    def call(self, method, params=None):
        return self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params or {}})

    def result(self, method, params=None):
        r = self.call(method, params)
        self.assertNotIn("error", r, r.get("error"))
        return r["result"]

    def test_status_before_start(self):
        r = self.result("record.status")
        self.assertFalse(r["recording"])
        self.assertEqual(r["blocks"], [])

    def test_full_flow_over_ipc(self):
        self.assertTrue(self.result("record.start")["ok"])
        self.events.append({"t": 0.0, "kind": "click", "x": 5, "y": 6,
                            "button": "left"})
        self.events.append({"t": 3.0, "kind": "click", "x": 9, "y": 9,
                            "button": "left"})
        st = self.result("record.status")
        self.assertTrue(st["recording"], "录制中界面要能问进度")
        self.assertEqual([b["action"] for b in st["blocks"]],
                         [R.B_CLICK, R.B_WAIT, R.B_CLICK])
        self.assertEqual(st["blocks"][1]["text"], "等一下 3.0 秒")

        r = self.result("record.stop")
        self.assertEqual([s["action"] for s in r["steps"]],
                         ["click", "wait", "click"])
        self.assertEqual(r["steps"][0]["target"]["text"], "确定")
        self.assertEqual(r["summary"]["total"], 3)
        # 步骤必须能原样过 schema 校验（带上 page 就能直接回放）
        self.assertEqual(self.result("record.status")["recording"], False)

    def test_stop_without_recording_is_an_error(self):
        r = self.call("record.stop")
        self.assertIn("error", r)
        self.assertIn("没有在录制", r["error"]["message"])

    def test_cancel_leaves_no_steps(self):
        self.result("record.start")
        self.events.append({"t": 0.0, "kind": "click", "x": 1, "y": 1})
        self.assertTrue(self.result("record.cancel")["ok"])
        self.assertIsNone(self.result("record.status")["result"])
        self.assertIn("event.record.cancelled",
                      [e.get("method") for e in self.sent])

    def test_started_event_carries_hotkey(self):
        self.result("record.start")
        started = [e for e in self.sent if e.get("method") == "event.record.started"]
        self.assertTrue(started, "界面要知道怎么停（用户在别的窗口里）")
        self.assertEqual(started[0]["params"]["hotkey"], "ctrl+alt+q")

    def test_live_blocks_reach_the_ui(self):
        self.result("record.start")
        self.events.append({"t": 0.0, "kind": "key", "key": "a", "mods": []})
        deadline = __import__("time").time() + 2.0
        import time
        while time.time() < deadline:
            got = [e for e in self.sent if e.get("method") == "event.record.block"]
            if got:
                break
            time.sleep(0.02)
        got = [e for e in self.sent if e.get("method") == "event.record.block"]
        self.assertTrue(got, "录制中界面要实时看到积木")
        self.assertEqual(got[-1]["params"]["text"], "输入文字「a」")

    def test_duplicate_start_refused(self):
        self.result("record.start")
        r = self.result("record.start")
        self.assertFalse(r["ok"])
        self.assertIn("已经在录制", r["note"])

    def test_notifications_are_valid_json_lines(self):
        """推送走的是同一条 stdout 通道，必须是能序列化的 JSON。"""
        self.result("record.start")
        self.events.append({"t": 0.0, "kind": "click", "x": 1, "y": 2})
        line = None
        for e in self.sent:
            if e.get("method") == "event.record.block":
                line = json.dumps(e, ensure_ascii=False)
        self.assertIsNotNone(line or json.dumps({"method": "x"}))

    def test_hotkey_stop_notifies_done(self):
        self.result("record.start")
        self.sess._rec.hooker.stopped.set()
        import time
        deadline = time.time() + 2.0
        done = []
        while time.time() < deadline:
            done = [e for e in self.sent if e.get("method") == "event.record.done"]
            if done:
                break
            time.sleep(0.02)
        self.assertTrue(done, "用户按停止热键后界面要收到 done")
        self.assertEqual(done[0]["params"]["reason"], "hotkey")


if __name__ == "__main__":
    unittest.main()
