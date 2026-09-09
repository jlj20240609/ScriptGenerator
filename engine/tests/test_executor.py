# -*- coding: utf-8 -*-
"""E4 executor：六动作 × 条件 × 循环 + L1/L2 + on_fail + 校验钩子 + 点击安全闸。"""
import threading
import time
import unittest

import numpy as np

from engine import executor as X
from engine.errors import EngineError
from engine.logger import MemoryLogger
from engine.tests import support as S

CANVAS = (1500, 1050)
PX, PY = 300, 150                      # 页面摆放在整屏中的位置（页面 A/B 同点切换）


class Env:
    """登录 → 首页 场景 + 替身驱动。"""

    def __init__(self, human=None, guard=True, calib=None, stop_event=None):
        self.login, self.login_boxes = S.login_page()
        self.home, self.home_boxes = S.home_page()
        self.v2, self.v2_boxes = S.home_page_v2()
        self.scene = S.StatefulScene(
            {"login": (self.login, self.login_boxes), "home": (self.home, self.home_boxes),
             "v2": (self.v2, self.v2_boxes)},
            {"login": (PX, PY, 1.0), "home": (PX, PY, 1.0), "v2": (PX, PY, 1.0)},
            canvas=CANVAS, initial="login")
        bx = self.login_boxes["login_btn"]
        self.scene.add_zone("login_btn", bx, "home")
        self.driver = S.FakeDriver(self.scene.provider, on_click=self.scene.on_click)
        self.human = human or S.FakeHuman()
        self.cfg = X.RunConfig(l1_retries=1, l1_retry_interval_s=0.01,
                               l2_poll_interval_s=0.05, l2_timeout_s=2.0,
                               guard=guard)
        self.calib = calib
        self.stop_event = stop_event
        self.login_spec = S.page_spec_of(self.login, rect=None)
        self.home_spec = S.page_spec_of(self.home, rect=None)

    def run(self, sg):
        self.logger = MemoryLogger()
        return X.run_script(sg, self.driver, cfg=self.cfg, loc_logger=self.logger,
                            human=self.human, calibrator=self.calib,
                            stop_event=self.stop_event)

    def page_rect(self, name):
        x, y, sc = self.scene.placement[name]
        bgr = self.scene.pages[name][0]
        h, w = bgr.shape[:2]
        return (x, y, w, h)

    def t_login(self, key, text=None, **kw):
        bx = self.login_boxes[key]
        return S.widget_target(self.login, self.login_spec, bx,
                               text=self.login_boxes_text(key) if text is None else text, **kw)

    def login_boxes_text(self, key):
        return {"user_label": "用户名", "pwd_label": "密码", "login_btn": "登录"}[key]

    def t_home(self, key):
        bx = self.home_boxes[key]
        text = {"menu": "库存查询", "welcome": "登录成功"}[key]
        return S.widget_target(self.home, self.home_spec, bx, text=text)


class FlowTest(unittest.TestCase):
    """验收条-1/3：六动作 × 条件 × 循环端到端（合成屏），点击全部落在页面内。"""

    def test_six_actions_condition_loop_chain(self):
        env = Env()
        sg = S.script("自动登录·合成", [
            S.action_step("s1", "type", env.t_login("user_label"), {"text": "admin"}),
            S.action_step("s2", "type", env.t_login("pwd_label"), {"text": "pass123"}),
            S.action_step("s3", "click", env.t_login("login_btn"),
                          expected_outcome={
                              "target": S.widget_target(env.home, env.home_spec,
                                                        env.home_boxes["welcome"],
                                                        text="登录成功"),
                              "on_fail": {"strategy": "notify",
                                          "message": "用户名或密码可能不对"}}),
            S.condition_step("s4", S.widget_target(env.home, env.home_spec,
                                                   env.home_boxes["welcome"],
                                                   text="登录成功"),
                             exists=True,
                             then=[S.action_step("s4a", "hotkey", None, {"keys": "ctrl+1"})],
                             else_=[S.action_step("s4b", "notify", None,
                                                  {"message": "还在登录页？"})]),
            S.loop_step("s5", {"mode": "count", "count": 2},
                        body=[S.action_step("s5a", "hotkey", None, {"keys": "ctrl+s"})]),
            S.action_step("s6", "notify", None, {"message": "完成"}),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        c = rep["counters"]
        self.assertEqual(c["clicks"], 1)          # 点击动作 1 次（登录）
        self.assertEqual(len(env.driver.clicks), 3)   # 实际点击 3 次（s1/s2 聚焦 + s3）
        self.assertEqual(env.driver.typed, ["admin", "pass123"])
        self.assertEqual(env.driver.hotkeys, ["ctrl+1", "ctrl+s", "ctrl+s"])
        self.assertEqual(env.human.notified, ["完成"])
        # 每次点击都必须落在当时页面内（越界防御：整屏画布内 = 页面区域外是灰色背景）
        login_rect = env.page_rect("login")
        for x, y, _ in env.driver.clicks:
            self.assertGreaterEqual(x, login_rect[0] - 2)
            self.assertGreaterEqual(y, login_rect[1] - 2)
        # 完成后停在首页
        self.assertEqual(env.scene.state, "home")
        ok_rows = [r for r in rep["steps"] if r.get("status") == "ok"
                   and r.get("type") == "action"]
        labels = " | ".join(r.get("label", "") for r in ok_rows)
        self.assertIn("输入文字", labels)
        self.assertIn("点一下", labels)
        # 定位日志按 step 可调阅（E6 契约：logger.tail(step_id, n)）
        rows = env.logger.tail("s3", 50)
        self.assertTrue(rows)
        self.assertTrue(all(r.get("step_id") == "s3" for r in rows))
        # 动作日志有定位方法/置信度
        s3_rows = [r for r in rep["steps"] if r.get("step_id") == "s3"
                   and r.get("status") == "ok"]
        self.assertTrue(s3_rows and s3_rows[0].get("method"))

    def test_condition_false_branch(self):
        env = Env()
        sg = S.script("条件-否", [
            S.action_step("s1", "click", env.t_login("login_btn"), None),
            S.condition_step("s2", S.widget_target(env.login, env.login_spec,
                                                   env.login_boxes["login_btn"],
                                                   text="登录"),
                             exists=True,
                             then=[S.action_step("s2a", "notify", None,
                                                 {"message": "还在登录页"})],
                             else_=[S.action_step("s2b", "notify", None,
                                                  {"message": "已经离开登录页"})]),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        # s1 点击登录 → 转到首页；旧页面“登录”按钮不可见 → 走 else
        self.assertEqual(env.human.notified, ["已经离开登录页"])
        cond = [r for r in rep["steps"] if r.get("type") == "condition"][0]
        self.assertEqual(cond["branch"], "else")

    def test_click_widget_text(self):
        """点击坐标 = 部件（文字）中心 + 页面偏移。"""
        env = Env()
        sg = S.script("坐标", [
            S.action_step("s1", "click", env.t_login("login_btn"), None),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        bx = env.login_boxes["login_btn"]
        exp = (PX + bx[0] + bx[2] // 2, PY + bx[1] + bx[3] // 2)
        x, y, _ = env.driver.clicks[0]
        dev = max(abs(x - exp[0]), abs(y - exp[1]))
        self.assertLessEqual(dev, 12, f"click=({x},{y}) exp={exp}")


class L1FlowTest(unittest.TestCase):
    def test_page_missing_l1_prompt_continue_after_fix(self):
        """页面缺失 → L1 自动重试 → 白话提示；用户处理后“继续”→ 步骤成功。"""
        env = Env()
        login_canvas = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
        S.paste_scale(env.login, login_canvas, PX, PY, 1.0)
        blank = np.full((CANVAS[1], CANVAS[0], 3), 24, np.uint8)
        holder = {"state": "blank"}
        env.driver = S.FakeDriver(lambda: login_canvas if holder["state"] == "login"
                                  else blank)
        env.human = S.FakeHuman(answers=["continue"])
        env.human.on_not_found = lambda msg: holder.__setitem__("state", "login")
        sg = S.script("L1-继续", [
            S.action_step("s1", "click", env.t_login("login_btn"), None),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertEqual(len(env.human.not_found_calls), 1)
        self.assertEqual(len(env.driver.clicks), 1)

    def test_page_missing_l1_prompt_skip(self):
        holder = {"state": "blank"}
        blank = np.full((CANVAS[1], CANVAS[0], 3), 24, np.uint8)
        env = Env()
        env.driver = S.FakeDriver(lambda: blank)
        env.human = S.FakeHuman(answers=["skip"])
        sg = S.script("L1-跳过", [
            S.action_step("s1", "click", env.t_login("login_btn"), None),
            S.action_step("s2", "notify", None, {"message": "后续步骤"})])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(len(env.driver.clicks), 0)
        s1 = [r for r in rep["steps"] if r.get("step_id") == "s1"][-1]
        self.assertEqual(s1["status"], "skipped")
        self.assertEqual(env.human.notified, ["后续步骤"])

    def test_page_missing_prompt_stop(self):
        blank = np.full((CANVAS[1], CANVAS[0], 3), 24, np.uint8)
        env = Env()
        env.driver = S.FakeDriver(lambda: blank)
        env.human = S.FakeHuman(answers=["stop"])
        sg = S.script("L1-停止", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "stopped")
        self.assertEqual(len(env.driver.clicks), 0)


class CalibFlowTest(unittest.TestCase):
    def test_calibrator_restores_after_page_missing(self):
        """过程性错误 → 校验钩子（返回更新后的页面）→ 自动恢复运行，无人值守。

        注：专注“页面定位失败→校验恢复”语义，关闭点击安全闸——闸的确定性
        行为由 GuardFlowTest 单独覆盖（本机 ORT/cv2 偶发挂死噪声不入本测试路径）。
        """
        holder = {"state": "login"}
        env = Env()
        login_canvas = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
        S.paste_scale(env.login, login_canvas, PX, PY, 1.0)
        home_canvas = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
        S.paste_scale(env.home, home_canvas, PX, PY, 1.0)
        env.driver = S.FakeDriver(lambda: home_canvas if holder["state"] == "home"
                                  else login_canvas)
        env.cfg.guard = False

        def calib(req):
            if req["reason"] == "page_not_found":
                holder["state"] = "home"      # 模拟“自动重采集后页面正确出现”
                return {"ok": True, "updated": True, "page_spec": env.home_spec,
                        "note": "AI 语义确认：页面已改版→重采集"}
            return {"ok": False, "updated": False}

        env.calib = calib
        # 部件页指向“首页”但屏幕一开始是登录页 → 页面定位失败 → 校验 → 恢复
        sg = S.script("校验恢复", [
            S.action_step("s1", "click", env.t_home("menu"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertEqual(len(rep["calib"]), 1)
        self.assertEqual(rep["calib"][0]["reason"], "page_not_found")
        self.assertEqual(len(env.human.not_found_calls), 0)   # 无人值守恢复
        self.assertEqual(len(env.driver.clicks), 1)

    def test_first_run_calibration_invoked_once(self):
        calls = []

        def calib(req):
            calls.append(req["reason"])
            return {"ok": True, "updated": False}

        env = Env(calib=calib)
        env.cfg.calibrate_first_run = True
        sg = S.script("首次校验", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(calls, ["first_run"])
        self.assertEqual(len(rep["calib"]), 1)

    def test_calibrator_fail_then_l1_prompt_stop(self):
        env = Env(calib=lambda req: {"ok": False, "updated": False})
        blank = np.full((CANVAS[1], CANVAS[0], 3), 24, np.uint8)
        env.driver = S.FakeDriver(lambda: blank)
        env.human = S.FakeHuman(answers=["stop"])
        sg = S.script("校验失败停止", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "stopped")
        self.assertEqual(rep["calib"][0]["ok"], False)


class OutcomeFlowTest(unittest.TestCase):
    def _sg_click_login(self, env, on_fail, timeout=2.0):
        env.cfg.l2_timeout_s = timeout
        return S.script("结果校验", [
            S.action_step("s1", "click", env.t_login("login_btn"),
                          expected_outcome={
                              "target": S.widget_target(env.home, env.home_spec,
                                                        env.home_boxes["welcome"],
                                                        text="登录成功"),
                              "on_fail": on_fail})])

    def test_outcome_ok(self):
        env = Env()    # 点击登录 → 转首页 → 登录成功出现
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                 "message": "用户名或密码可能不对"}))
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertEqual(env.human.outcome_calls, [])
        s1 = [r for r in rep["steps"] if r.get("step_id") == "s1" and
              r.get("status") == "ok"][-1]
        self.assertEqual(s1.get("verify", {}).get("outcome"), "seen")

    def _stuck_env(self):
        """点击登录不转场（状态机坏掉/密码错）→ 预期结果永不出现。"""
        env = Env()
        env.scene.zones.clear()
        return env

    def test_outcome_fail_notify(self):
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                 "message": "用户名或密码可能不对"}))
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.outcome_calls, ["用户名或密码可能不对"])
        fail = [r for r in rep["steps"] if r.get("status") == "fail"]
        self.assertTrue(fail and fail[0]["error"] == "verify_failed")
        self.assertEqual(len(env.driver.clicks), 1)   # 结果性失败不重试

    def test_outcome_fail_retry_then_prompt(self):
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        rep = env.run(self._sg_click_login(env, {"strategy": "retry",
                                                 "retry": {"times": 3},
                                                 "message": "登录失败"}))
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(len(env.driver.clicks), 3)   # 1 + 2 次自动重试
        self.assertEqual(len(env.human.outcome_calls), 1)

    def test_outcome_fail_stop(self):
        env = self._stuck_env()
        rep = env.run(self._sg_click_login(env, {"strategy": "stop",
                                                 "message": "停止脚本"}))
        self.assertEqual(rep["status"], "stopped")
        self.assertEqual(len(env.driver.clicks), 1)

    def test_outcome_fail_skip_continues(self):
        env = self._stuck_env()
        sg = self._sg_click_login(env, {"strategy": "skip"})
        sg["steps"].append(S.action_step("s2", "notify", None, {"message": "失败后继续"}))
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["失败后继续"])
        fails = [r for r in rep["steps"] if r.get("status") == "fail"]
        self.assertTrue(fails)


class GuardFlowTest(unittest.TestCase):
    def test_guard_blocks_click_when_patch_changed(self):
        class BadPatchDriver(S.FakeDriver):
            def grab_rect(self, rect):
                return np.full((120, 420, 3), 210, np.uint8)   # 空白 → 校验必失败

        env = Env()
        env.driver = BadPatchDriver(env.scene.provider, on_click=env.scene.on_click)
        env.human = S.FakeHuman(answers=["stop"])
        sg = S.script("安全闸", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "stopped")
        self.assertEqual(len(env.driver.clicks), 0)           # 过不了闸绝不点击
        self.assertEqual(len(env.human.not_found_calls), 1)


class LoopTest(unittest.TestCase):
    def test_count_loop(self):
        env = Env()
        sg = S.script("次数循环", [
            S.loop_step("l1", {"mode": "count", "count": 3},
                        body=[S.action_step("b1", "notify", None, {"message": "圈"})])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["圈", "圈", "圈"])
        loop_rows = [r for r in rep["steps"] if r.get("type") == "loop"
                     and r.get("status") == "ok" and "iterations" in r]
        self.assertEqual(loop_rows[-1]["iterations"], 3)

    def test_until_loop_waits_page_change(self):
        """登录页点登录 → 跳转首页 → “直到看到登录成功”退出（1 次循环体）。"""
        env = Env()
        sg = S.script("直到看到", [
            S.loop_step("l1", {"mode": "until",
                               "target": S.widget_target(env.home, env.home_spec,
                                                         env.home_boxes["welcome"],
                                                         text="登录成功")},
                        body=[S.action_step("b1", "click", env.t_login("login_btn"), None)])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertEqual(env.scene.state, "home")
        loop_rows = [r for r in rep["steps"] if r.get("type") == "loop"
                     and r.get("status") == "ok" and "iterations" in r]
        self.assertEqual(loop_rows[-1]["iterations"], 1)
        self.assertEqual(len(env.driver.clicks), 1)

    def test_until_loop_cap(self):
        env = Env()
        env.cfg.until_max = 5
        sg = S.script("上限", [
            S.loop_step("l1", {"mode": "until",
                               "target": S.widget_target(env.home, env.home_spec,
                                                         env.home_boxes["welcome"],
                                                         text="登录成功")},
                        body=[S.action_step("b1", "notify", None, {"message": "空转"})]),
            S.action_step("s2", "notify", None, {"message": "循环后继续"})])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")   # 循环达上限记为失败但不终止脚本
        loop_rows = [r for r in rep["steps"] if r.get("type") == "loop"]
        self.assertEqual(loop_rows[-1]["error"], "loop_limit")
        self.assertEqual(loop_rows[-1]["iterations"], 5)
        self.assertEqual(env.human.notified[-1], "循环后继续")

    def test_forever_stop_event(self):
        env = Env()
        ev = threading.Event()
        env.stop_event = ev
        sg = S.script("一直重复", [
            S.loop_step("l1", {"mode": "forever"},
                        body=[S.action_step("b1", "notify", None, {"message": "转"})])])
        result = {}

        def worker():
            result["rep"] = env.run(sg)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        deadline = time.time() + 15
        while len(env.human.notified) < 2 and time.time() < deadline:
            time.sleep(0.02)
        ev.set()
        t.join(10)
        self.assertFalse(t.is_alive(), "forever 循环未在停止标志后退出")
        self.assertEqual(result["rep"]["status"], "stopped")


class InterpreterMiscTest(unittest.TestCase):
    def test_wait_and_hotkey_no_target_steps(self):
        env = Env()
        sg = S.script("无目标", [
            S.action_step("w", "wait", None, {"seconds": 0.05}),
            S.action_step("h", "hotkey", None, {"keys": "alt+f4"}),
            S.action_step("n", "notify", None, {"message": "hi"})])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.driver.hotkeys, ["alt+f4"])
        self.assertGreaterEqual(env.driver.slept, 0.05)

    def test_stop_event_between_steps(self):
        env = Env()
        ev = threading.Event()
        env.stop_event = ev
        sg = S.script("步间停止", [
            S.action_step("s1", "click", env.t_login("login_btn"), None),
            S.action_step("s2", "click", env.t_home("menu"), None)])

        class InterruptDriver(S.FakeDriver):
            def click(self, x, y, dbl=False):
                super().click(x, y, dbl)
                ev.set()

        env.driver = InterruptDriver(env.scene.provider, on_click=env.scene.on_click)
        rep = env.run(sg)
        self.assertEqual(rep["status"], "stopped")
        self.assertEqual(len(env.driver.clicks), 1)

    def test_schema_invalid_raises(self):
        env = Env()
        sg = S.script("坏脚本", [{"id": "x", "type": "nope"}])
        with self.assertRaises(EngineError) as cm:
            env.run(sg)
        self.assertEqual(cm.exception.code, "schema_invalid")

    def test_loc_logger_events_written(self):
        env = Env()
        sg = S.script("日志", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        rows = env.logger.tail("s1", 100)
        events = {r["event"] for r in rows}
        self.assertIn("locate_page", events)
        self.assertIn("locate_widget", events)
        self.assertIn("click_guard", events)
        # 方法词汇在允许集内（定位 + 点击闸验证方法）
        methods = {r["method"] for r in rows}
        self.assertTrue(methods <= {"page_tpl", "anchor", "ocr_text", "tpl",
                                    "page_coord", "ai", "none", "ocr_patch",
                                    "tpl_fallback", "no_patch", "guard_off"})


class PerfSmokeTest(unittest.TestCase):
    def test_step_widget_locate_under_500ms(self):
        """验收 §5.7 预算抽查：一次点击步骤的总定位（页面+部件模板路径，无 OCR 命中前）
        主耗时在页面定位；widget 单独模板定位 ≤500ms（locator 已测）。
        这里记录整步耗时供回归观测，不设硬门槛（页面多尺度在预算外另测）。
        """
        env = Env()
        env.cfg.guard = False
        sg = S.script("耗时观测", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        t0 = time.perf_counter()
        rep = env.run(sg)
        total_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(rep["status"], "ok")
        self.assertLess(total_ms, 8000, f"整步含首次页面定位过慢: {total_ms:.0f}ms")


class UiaExecTest(unittest.TestCase):
    """executor ↔ UIA ①级接线：driver.uia_provider 命中时步骤走 uia 路径。"""

    def test_click_via_uia_level1(self):
        env = Env()
        bx = env.login_boxes["login_btn"]
        abs_rect = (PX + bx[0], PY + bx[1], bx[2], bx[3])

        class UiaDriver(S.FakeDriver):
            def uia_provider(self):
                def provider(text):
                    return [{"name": text, "rect": abs_rect,
                             "center": (abs_rect[0] + abs_rect[2] // 2,
                                        abs_rect[1] + abs_rect[3] // 2),
                             "type": "ButtonControl"}]
                return provider

        env.driver = UiaDriver(env.scene.provider, on_click=env.scene.on_click)
        sg = S.script("UIA点击", [
            S.action_step("s1", "click", env.t_login("login_btn"), None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        ok_rows = [r for r in rep["steps"] if r.get("status") == "ok"]
        self.assertEqual(ok_rows[-1]["method"], "uia")
        exp = (abs_rect[0] + abs_rect[2] // 2, abs_rect[1] + abs_rect[3] // 2)
        x, y, _ = env.driver.clicks[0]
        dev = max(abs(x - exp[0]), abs(y - exp[1]))
        self.assertLessEqual(dev, 4, f"click=({x},{y}) exp={exp}")


if __name__ == "__main__":
    unittest.main()
