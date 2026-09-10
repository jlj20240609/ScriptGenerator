# -*- coding: utf-8 -*-
"""IPC 服务端（engine/ipc.py）测试：协议、双截图采集、运行事件、人工确认、停止。

离线：屏幕抓取 / UIA hit-test / 窗口上下文 / driver 均注入合成屏。
另含一个真子进程冒烟（stdio 协议连通，不抓屏）。
"""
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from engine import ipc as IP
from engine.tests import support as S

PX, PY = 300, 150
CANVAS = (1500, 1050)


class _Env:
    """合成屏 + 注入件（page.capture/widget.capture/script.run 全离线）。"""

    def __init__(self):
        self.login, self.boxes = S.login_page()
        self.canvas = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
        self.page_rect = S.paste_scale(self.login, self.canvas, PX, PY, 1.0)
        self.scene = S.StatefulScene(
            {"login": (self.login, self.boxes)},
            {"login": (PX, PY, 1.0)}, canvas=CANVAS, initial="login")
        bx = self.boxes["login_btn"]
        self.scene.add_zone("login_btn", bx, "login")
        self.driver = S.FakeDriver(self.scene.provider, on_click=self.scene.on_click)
        self.events = []

    def server(self, **kw):
        srv = IP.IpcServer(
            driver_factory=lambda win_ctx: self.driver,
            grab=lambda rect=None: self._grab(rect),
            hit_test=lambda x, y: {"name": "登录", "automation_id": "btnLogin",
                                   "type": "ButtonControl"},
            context_from_point=lambda x, y: {"hwnd": 424242, "process": "msedge.exe",
                                             "title": "M0 演示登录 · 示例公司门户",
                                             "class": "Chrome_WidgetWin_1"},
            **kw)
        srv.send = lambda obj: self.events.append(obj)      # 收集事件（不写 stdout）
        return srv

    def _grab(self, rect=None):
        if rect is None:
            return self.canvas
        x, y, w, h = [int(v) for v in rect]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.canvas.shape[1], x + w), min(self.canvas.shape[0], y + h)
        return self.canvas[y0:y1, x0:x1]

    def _call(self, srv, method, params=None, rid=1):
        return srv.handle({"jsonrpc": "2.0", "id": rid, "method": method,
                           "params": params or {}})

    def _result(self, srv, method, params=None):
        resp = self._call(srv, method, params)
        if "error" in resp:
            return None, resp["error"]
        return resp["result"], None

    def events_of(self, method):
        return [e["params"] for e in self.events if e.get("method") == method]


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.srv = self.env.server()

    def test_ping(self):
        r, err = self.env._result(self.srv, "ping")
        self.assertIsNone(err)
        self.assertTrue(r["ok"])
        self.assertIn("engine_version", r)

    def test_new_script_valid(self):
        r, _ = self.env._result(self.srv, "script.new", {"name": "自动登录"})
        self.assertEqual(r["script"]["version"], "1.0")
        self.assertEqual(r["script"]["steps"], [])

    def test_unknown_method(self):
        resp = self.env._call(self.srv, "nope.method")
        self.assertEqual(resp["error"]["code"], IP.RPC_METHOD_NOT_FOUND)

    def test_bad_params(self):
        resp = self.env._call(self.srv, "page.capture", {"rect": [1, 2]})
        self.assertEqual(resp["error"]["code"], IP.RPC_INVALID_PARAMS)

    def test_parse_error(self):
        resp = self.srv.handle_line("{not json")
        self.assertEqual(resp["error"]["code"], IP.RPC_PARSE_ERROR)

    def test_shutdown(self):
        r, _ = self.env._result(self.srv, "engine.shutdown")
        self.assertTrue(r["ok"])
        self.assertTrue(self.srv._closing)


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.srv = self.env.server()

    def test_page_capture_then_widget_capture(self):
        pr, err = self.env._result(self.srv, "page.capture",
                                   {"rect": list(self.env.page_rect)})
        self.assertIsNone(err, err)
        page = pr["page"]
        self.assertEqual(page["size"], [1000, 640])
        self.assertEqual(pr["hwnd"], 424242)
        self.assertEqual(page["context"]["process"], "msedge.exe")
        self.assertTrue(page["image"].startswith("data:image/png;base64,"))
        # 部件：页面内框住“登录”按钮（两次框选换算成页内坐标）
        bx = self.env.boxes["login_btn"]
        wr, err = self.env._result(self.srv, "widget.capture",
                                   {"rect_in_page": list(bx)})
        self.assertIsNone(err, err)
        tgt = wr["target"]
        self.assertIn("登录", tgt["text"])
        self.assertEqual(tgt["rect_in_page"], list(bx))
        self.assertEqual(tgt["uia"]["automation_id"], "btnLogin")
        # 返回的 target+page 可直接组装并校验
        tgt["page"] = wr["page"]
        sg = {"version": "1.0", "name": "x", "steps": [
            {"id": "s1", "type": "action", "action": "click", "target": tgt,
             "params": {}}]}
        from engine import schema
        self.assertEqual(schema.validate(sg), [])

    def test_widget_before_page(self):
        srv = self.env.server()
        resp = self.env._call(srv, "widget.capture", {"rect_in_page": [1, 1, 20, 20]})
        self.assertEqual(resp["error"]["code"], IP.RPC_ENGINE_ERROR)

    def test_widget_out_of_page(self):
        self.env._result(self.srv, "page.capture", {"rect": list(self.env.page_rect)})
        resp = self.env._call(self.srv, "widget.capture",
                              {"rect_in_page": [900, 600, 200, 200]})
        self.assertEqual(resp["error"]["code"], IP.RPC_INVALID_PARAMS)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.srv = self.env.server()
        self.srv._confirm_auto = lambda kind, msg, opts: "继续"
        self.env._result(self.srv, "page.capture", {"rect": list(self.env.page_rect)})
        wr, _ = self.env._result(self.srv, "widget.capture",
                                 {"rect_in_page": list(self.env.boxes["login_btn"])})
        self.target = wr["target"]
        self.target["page"] = wr["page"]

    def _wait_done(self, timeout=60.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            done = self.env.events_of("event.run_done")
            if done:
                return done[-1]
            time.sleep(0.1)
        self.fail("run_done 未到达")

    def test_run_ok_with_events(self):
        sg = {"version": "1.0", "name": "登录演示", "steps": [
            {"id": "s1", "type": "action", "action": "click",
             "target": self.target, "params": {}},
        ]}
        r, err = self.env._result(self.srv, "script.run",
                                  {"script": sg, "options": {"calibrate": False}})
        self.assertIsNone(err, err)
        self.assertTrue(r["run_id"].startswith("r"))
        done = self._wait_done()
        self.assertEqual(done["status"], "ok", self.env.events)
        self.assertGreaterEqual(done["clicks"], 1)
        steps = self.env.events_of("event.step")
        self.assertTrue(any(s.get("status") == "ok" for s in steps))
        states = [e["state"] for e in self.env.events_of("event.run_state")]
        self.assertEqual(states[0], "running")
        self.assertEqual(states[-1], "done")

    def test_notify_step_triggers_confirm(self):
        calls = []
        self.srv._confirm_auto = lambda kind, msg, opts: (calls.append((kind, msg, opts))
                                                          or "继续")
        sg = {"version": "1.0", "name": "提示", "steps": [
            {"id": "s1", "type": "action", "action": "notify",
             "params": {"message": "请确认一下网络"}}]}
        self.env._result(self.srv, "script.run", {"script": sg})
        done = self._wait_done()
        self.assertEqual(done["status"], "ok")
        self.assertEqual(calls[0][0], "notify")
        self.assertIn("请确认一下网络", calls[0][1])
        reqs = self.env.events_of("event.confirm_request")
        self.assertTrue(reqs and reqs[0]["options"] == ["继续"])

    def test_confirm_reply_resolves(self):
        """真实等待链：引擎发 confirm_request，UI 侧回 confirm.reply。"""
        self.srv._confirm_auto = None
        sg = {"version": "1.0", "name": "提示", "steps": [
            {"id": "s1", "type": "action", "action": "notify",
             "params": {"message": "处理完请继续"}}]}
        self.env._result(self.srv, "script.run", {"script": sg})

        def reply():
            t0 = time.time()
            while time.time() - t0 < 20:
                reqs = self.env.events_of("event.confirm_request")
                if reqs:
                    self.env._result(self.srv, "confirm.reply",
                                     {"request_id": reqs[-1]["request_id"],
                                      "choice": "继续"})
                    return
                time.sleep(0.05)

        th = threading.Thread(target=reply, daemon=True)
        th.start()
        done = self._wait_done()
        th.join(5)
        self.assertEqual(done["status"], "ok")

    def test_busy_second_run(self):
        sg = {"version": "1.0", "name": "长循环", "steps": [
            {"id": "l1", "type": "loop", "loop": {"mode": "forever"},
             "body": [{"id": "b1", "type": "action", "action": "wait",
                       "params": {"seconds": 0.05}}]}]}
        self.env._result(self.srv, "script.run", {"script": sg})
        resp = self.env._call(self.srv, "script.run", {"script": sg})
        self.assertEqual(resp["error"]["code"], IP.RPC_BUSY)
        self.env._result(self.srv, "script.stop")
        self._wait_done()

    def test_stop_forever_loop(self):
        sg = {"version": "1.0", "name": "一直重复", "steps": [
            {"id": "l1", "type": "loop", "loop": {"mode": "forever"},
             "body": [{"id": "b1", "type": "action", "action": "wait",
                       "params": {"seconds": 0.05}}]}]}
        self.env._result(self.srv, "script.run", {"script": sg})
        t0 = time.time()
        while not self.env.events_of("event.run_state") and time.time() - t0 < 10:
            time.sleep(0.05)
        time.sleep(0.4)
        self.env._result(self.srv, "script.stop")
        done = self._wait_done()
        self.assertEqual(done["status"], "stopped")

    def test_save_load_roundtrip(self):
        import tempfile
        sg = {"version": "1.0", "name": "存取", "steps": [
            {"id": "s1", "type": "action", "action": "click",
             "target": self.target, "params": {}}]}
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "a.sgscript.json")
            r, err = self.env._result(self.srv, "script.save",
                                      {"path": path, "script": sg})
            self.assertIsNone(err, err)
            self.assertTrue(r["ok"])
            r2, err2 = self.env._result(self.srv, "script.load", {"path": path})
            self.assertIsNone(err2, err2)
            self.assertEqual(r2["script"]["name"], "存取")
            self.assertEqual(r2["script"]["steps"][0]["target"]["text"],
                             self.target["text"])

    def test_engine_error_code_passthrough(self):
        """页面无法定位（target 无 page）→ L1 提示 → 用户选停止 → run_done stopped。"""
        self.srv._confirm_auto = lambda kind, msg, opts: (
            "停止" if "停止" in opts else "继续")
        sg = {"version": "1.0", "name": "坏脚本", "steps": [
            {"id": "s1", "type": "action", "action": "click",
             "target": {"text": "不存在的东西"}}]}
        resp = self.env._call(self.srv, "script.run", {"script": sg})
        self.assertNotIn("error", resp)
        done = self._wait_done()
        self.assertEqual(done["status"], "stopped")
        reqs = self.env.events_of("event.confirm_request")
        self.assertTrue(reqs and reqs[0]["kind"] == "not_found", reqs)


class SubprocessSmokeTest(unittest.TestCase):
    """真子进程冒烟：stdio 协议连通（不抓屏、不开窗）。"""

    def test_ping_and_shutdown(self):
        root = Path(__file__).resolve().parents[2]
        p = subprocess.Popen([sys.executable, "-u", "-m", "engine", "serve"],
                             cwd=str(root), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")

        def request(rid, method, params=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                      "params": params or {}}) + "\n")
            p.stdin.flush()
            for _ in range(50):           # 跳过中间的通知（event.*）
                msg = json.loads(p.stdout.readline())
                if msg.get("id") == rid:
                    return msg
            self.fail(f"未收到 {method} 的响应")

        try:
            resp = request(1, "ping")
            self.assertTrue(resp["result"]["ok"])
            resp2 = request(2, "engine.shutdown")
            self.assertTrue(resp2["result"]["ok"])
            p.stdin.close()
            self.assertEqual(p.wait(timeout=30), 0)
        finally:
            if p.poll() is None:
                p.kill()
            for stream in (p.stdin, p.stdout, p.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
