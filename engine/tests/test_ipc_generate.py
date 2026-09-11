# -*- coding: utf-8 -*-
"""M4-WP5 一句话生成的 IPC 契约测试。

界面到引擎之间就这一层。这里最该钉住的是**边界**：生成出来的步骤不许带坐标
（云端一旦给了坐标，整个"坐标一律本地求"的架构就破了），以及"没授权时不硬试"。
"""
import unittest

from engine import ipc as IP
from engine.tests.test_ai_generate import GOOD_JSON, NOT_JSON, _FakeVLM


class _FakeVlmServer(IP.IpcServer):
    """把假模型塞进服务端（真机上是授权过的 ZhipuVLM）。"""

    def __init__(self, vlm):
        super().__init__()
        self._vlm = vlm


class GenerateIpcTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.srv = None

    def make(self, vlm):
        srv = _FakeVlmServer(vlm)
        srv.send = lambda obj: self.sent.append(obj)
        return srv

    def call(self, srv, method, params=None):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}})

    def result(self, srv, method, params=None):
        r = self.call(srv, method, params)
        self.assertNotIn("error", r, r.get("error"))
        return r["result"]

    def test_generate_returns_skeleton(self):
        srv = self.make(_FakeVLM(reply=GOOD_JSON))
        r = self.result(srv, "generate.script", {"sentence": "帮我做一个自动登录"})
        self.assertTrue(r["ok"], r.get("notes"))
        self.assertEqual([s["action"] for s in r["script"]["steps"]],
                         ["type", "type", "click", "wait", "notify"])
        self.assertIn("登录", r["pending"])
        logs = [e["params"]["text"] for e in self.sent if e.get("method") == "event.log"]
        self.assertTrue(any("还差框一次操作页面" in t for t in logs), logs)

    def test_generated_skeleton_has_no_coordinates(self):
        import json
        srv = self.make(_FakeVLM(reply=GOOD_JSON))
        r = self.result(srv, "generate.script", {"sentence": "自动登录"})
        blob = json.dumps(r["script"], ensure_ascii=False)
        for bad in ("rect_in_page", "center_in_page", "coord", "image"):
            self.assertNotIn(bad, blob)

    def test_empty_sentence_refused(self):
        srv = self.make(_FakeVLM())
        r = self.call(srv, "generate.script", {"sentence": "  "})
        self.assertIn("error", r)

    def test_not_authorized_degrades_without_calling(self):
        vlm = _FakeVLM(enabled=False)
        srv = self.make(vlm)
        r = self.result(srv, "generate.script", {"sentence": "自动登录"})
        self.assertFalse(r["ok"])
        self.assertEqual(vlm.calls, [], "没授权一次都不许调云端")
        self.assertTrue(any("截图目标" in n for n in r["notes"]), r["notes"])

    def test_chatty_reply_degrades(self):
        srv = self.make(_FakeVLM(reply=NOT_JSON))
        r = self.result(srv, "generate.script", {"sentence": "自动登录"})
        self.assertFalse(r["ok"])
        self.assertTrue(r["notes"])

    def test_pending_lists_unfilled_widgets(self):
        srv = self.make(_FakeVLM(reply=GOOD_JSON))
        gen = self.result(srv, "generate.script", {"sentence": "自动登录"})
        pend = self.result(srv, "generate.pending", {"script": gen["script"]})
        self.assertEqual(pend["pending"], ["用户名", "密码", "登录", "登录成功"])

    def test_autofill_without_page_gives_clear_note(self):
        srv = self.make(_FakeVLM())
        r = self.result(srv, "generate.autofill",
                        {"script": {"version": "1.0", "name": "x", "steps": [
                            {"id": "a", "type": "action", "action": "click",
                             "target": {"text": "登录"}, "params": {}}]}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("截图目标" in n for n in r["notes"]), r["notes"])

    def test_autofill_needs_script(self):
        srv = self.make(_FakeVLM())
        r = self.call(srv, "generate.autofill", {})
        self.assertIn("error", r)

    def test_events_serializable(self):
        import json
        srv = self.make(_FakeVLM(reply=GOOD_JSON))
        self.result(srv, "generate.script", {"sentence": "自动登录"})
        for e in self.sent:
            json.dumps(e, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
