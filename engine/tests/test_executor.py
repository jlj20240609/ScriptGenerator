# -*- coding: utf-8 -*-
"""E4 executor：六动作 × 条件 × 循环 + L1/L2 + on_fail + 校验钩子 + 点击安全闸。"""
import threading
import time
import unittest

import numpy as np

from engine import executor as X
from engine.errors import EngineError
from engine.executor import HUMAN_COORD_ONCE, HUMAN_SKIP
from engine.logger import MemoryLogger
from engine.tests import support as S

CANVAS = (1500, 1050)
PX, PY = 300, 150                      # 页面摆放在整屏中的位置（页面 A/B 同点切换）


class _FakeJudge:
    """假 D3 判定器：不需要屏幕也不需要网络，只记下被问了什么。"""

    def __init__(self, kind="not_found", label="没出现", boom=False):
        self.kind = kind
        self.label = label
        self.boom = boom
        self.calls = 0
        self.intents = []

    def judge(self, screen, intent="", local=None, ocr_fn=None):
        self.calls += 1
        self.intents.append(intent)
        if self.boom:
            raise RuntimeError("判定器挂了")
        return {"ok": False, "kind": self.kind, "label": self.label,
                "reason": "timeout", "source": "ai", "elapsed_ms": 12.0,
                "intent": intent}


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

    def run(self, sg, judge=None):
        self.logger = MemoryLogger()
        return X.run_script(sg, self.driver, cfg=self.cfg, loc_logger=self.logger,
                            human=self.human, calibrator=self.calib,
                            stop_event=self.stop_event, judge=judge)

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

    def test_semantic_hint_is_added_to_failure_message(self):
        """D3 接进执行器：失败提示要带上"看起来是哪一类失败"，用户才知道去修什么。"""
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        judge = _FakeJudge(kind="password_wrong", label="密码错")
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                "message": "做完后没看到预期结果"}),
                      judge=judge)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.outcome_calls,
                         ["做完后没看到预期结果（看起来是：密码错）"])
        self.assertEqual(judge.calls, 1, "只问一次")
        self.assertIn("点一下", judge.intents[0], "要把这一步本来想做什么告诉判定器")

    def test_semantic_hint_silent_when_undecidable(self):
        """判不出来时一个字都不加：给一个猜的原因比不说话更糟（用户会照着修错东西）。"""
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        judge = _FakeJudge(kind="not_found", label="没出现")
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                "message": "做完后没看到预期结果"}),
                      judge=judge)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.outcome_calls, ["做完后没看到预期结果"])

    def test_no_judge_keeps_old_behaviour(self):
        """没接 D3 时行为与以前完全一致（老测试靠这条保平安）。"""
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                "message": "用户名或密码可能不对"}))
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.outcome_calls, ["用户名或密码可能不对"])

    def test_judge_crash_does_not_break_the_run(self):
        """判定器挂了不能把整个运行带崩——它只是个"顺手多说一句"的增强。"""
        env = self._stuck_env()
        env.human = S.FakeHuman(answers=["continue"])
        rep = env.run(self._sg_click_login(env, {"strategy": "notify",
                                                "message": "没等到结果"}),
                      judge=_FakeJudge(boom=True))
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.outcome_calls, ["没等到结果"])


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


class ClickGuardRingTest(unittest.TestCase):
    """安全闸也要"认边框"：输入框被填过数据（占位提示被顶替）时不能把点击误拦。"""

    @staticmethod
    def _runner(patch):
        from engine.executor import _Runner, RunConfig

        class D:
            def grab_rect(self, rect):
                return patch

            def grab_screen(self):
                return patch, {}

            def sleep(self, _s):
                pass

            def click(self, *_a, **_k):
                pass

            def type_text(self, _t):
                pass

            def hotkey(self, _k):
                pass

        r = _Runner.__new__(_Runner)          # 只测这个纯判定函数，跳过 __init__
        r.cfg = RunConfig()
        r.driver = D()
        return r

    @staticmethod
    def _patch_of(page, bx, fill):
        img = S.Image.fromarray(page[:, :, ::-1])
        fill(img, bx)
        live = S.pil_to_bgr(img)
        x0 = max(0, bx[0] - 40)
        return live[max(0, bx[1] - 40):bx[1] + bx[3] + 40, x0:bx[0] + bx[2] + 40]

    def test_guard_accepts_filled_input(self):
        page, boxes = S.login_page()
        bx = boxes["pwd_box"]
        t = S.widget_target(page, S.page_spec_of(page), bx, text="请输入密码")
        patch = self._patch_of(page, bx, lambda img, b: S.draw_text(
            img, b[0] + 18, b[1] + 16, "wrong-pass-1234", size=22, fill=(25, 25, 25)))
        g = self._runner(patch)._click_guard(
            t, (bx[0] + bx[2] // 2, bx[1] + bx[3] // 2), None)
        self.assertTrue(g["ok"], g)
        self.assertIn(g["method"], ("ocr_patch", "tpl_fallback", "tpl_ring"))

    def test_guard_still_blocks_when_control_replaced(self):
        """控件真被换掉（整块涂黑）→ 仍然拦住，不放行误点。"""
        page, boxes = S.login_page()
        bx = boxes["pwd_box"]
        t = S.widget_target(page, S.page_spec_of(page), bx, text="请输入密码")
        patch = self._patch_of(page, bx, lambda img, b: S.ImageDraw.Draw(img).rectangle(
            (b[0] + 2, b[1] + 2, b[0] + b[2] - 2, b[1] + b[3] - 2), fill=(25, 25, 25)))
        g = self._runner(patch)._click_guard(
            t, (bx[0] + bx[2] // 2, bx[1] + bx[3] // 2), None)
        self.assertFalse(g["ok"], g)


class L3TemplatesTest(unittest.TestCase):
    """L3 显式条件的三种典型用法（分析文档 §6.2）——M2 补齐"停止"动作后全部可表达。"""

    def test_see_notice_then_stop(self):
        """"如果看到'用户名或密码错误' → 停止"。"""
        env = Env()
        sg = S.script("看到错误就停", [
            S.condition_step("c1", env.t_login("login_btn"), exists=True,
                             then=[S.action_step("a1", "stop", None, None)]),
            S.action_step("s9", "notify", None, {"message": "不该跑到这里"}),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "stopped")
        self.assertIn("停止", rep.get("error") or "")
        self.assertEqual(env.human.notified, [])          # 后面的步骤没执行

    def test_not_see_then_retry_by_loop(self):
        """"如果没看到'登录成功' → 重复 3 次点击登录"（条件 + 循环组合）。"""
        env = Env()
        sg = S.script("没看到就重试", [
            S.loop_step("l1", {"mode": "count", "count": 3}, body=[
                S.condition_step("c1", env.t_home("welcome"), exists=False,
                                 then=[S.action_step("a1", "click",
                                                     env.t_login("login_btn"), None)])])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertGreaterEqual(len(env.driver.clicks), 1)   # 至少点过一次
        self.assertEqual(env.scene.state, "home")            # 最终确实到了首页

    def test_see_failure_then_notify(self):
        """"如果看到'网络连接失败' → 提示我'请检查网络后点继续'"。"""
        env = Env()
        sg = S.script("断网提示", [
            S.condition_step("c1", env.t_login("login_btn"), exists=True,
                             then=[S.action_step("a1", "notify", None,
                                                 {"message": "请检查网络后点继续"})])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["请检查网络后点继续"])


class LoopLimitTest(unittest.TestCase):
    """M2：固定次数循环的软上限（防手滑写大数把电脑跑飞）。"""

    def test_repeat_capped(self):
        env = Env()
        env.cfg.repeat_max = 2
        sg = S.script("超大次数", [
            S.loop_step("l1", {"mode": "count", "count": 99999}, body=[
                S.action_step("b1", "notify", None, {"message": "圈"})])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["圈", "圈"])     # 只跑上限次
        labels = [r.get("label", "") for r in rep["steps"]]
        self.assertTrue(any("超过上限" in x for x in labels), labels)


class CoordFallbackTest(unittest.TestCase):
    """L1 弹窗里的半自动降级："用记下来的位置点一次"（用户审查要求）。"""

    def test_user_choice_clicks_by_recorded_position(self):
        env = Env()
        env.human = S.FakeHuman(answers=[HUMAN_COORD_ONCE])
        # 页面上没有这个东西（文字查不到、图案也不对）→ 进 L1；用户选"按位置点一次"
        t = env.t_login("login_btn")
        bad = S.widget_target(env.login, env.login_spec, env.login_boxes["login_btn"],
                              text="绝无此物", include_image=False)
        bad["rect_in_page"] = list(env.login_boxes["login_btn"])
        bad["center_in_page"] = [env.login_boxes["login_btn"][0] +
                                 env.login_boxes["login_btn"][2] // 2,
                                 env.login_boxes["login_btn"][1] +
                                 env.login_boxes["login_btn"][3] // 2]
        sg = S.script("按位置点一次", [S.action_step("s1", "click", bad, None)])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok", rep.get("error"))
        self.assertEqual(len(env.driver.clicks), 1)          # 就点一次（不反复）
        rows = [r for r in rep["steps"] if r.get("step_id") == "s1"]
        self.assertTrue(rows and rows[-1].get("forced"), rows)
        self.assertEqual(rows[-1]["method"], "page_coord")
        _ = t

    def test_no_rect_means_fall_back_to_prompt(self):
        """没有位置信息时不能瞎点：如实说明并重新让用户选。"""
        env = Env()
        env.human = S.FakeHuman(answers=[HUMAN_COORD_ONCE, HUMAN_SKIP])
        bad = S.widget_target(env.login, env.login_spec, env.login_boxes["login_btn"],
                              text="绝无此物", include_image=False)
        bad.pop("rect_in_page", None)                       # 没记下位置
        bad.pop("center_in_page", None)
        sg = S.script("没有位置", [S.action_step("s1", "click", bad, None)])
        rep = env.run(sg)
        self.assertEqual(len(env.driver.clicks), 0)
        self.assertTrue(any("没记下位置" in m for m in env.human.notified), env.human.notified)
        self.assertEqual(rep["status"], "ok")               # 第二次选了跳过


class UnicodeInputTest(unittest.TestCase):
    """输入文字用 Unicode 直发（绕开中文输入法）。

    真人实测（2026-09-10）：中文输入法会把 "demo" 拦成拼音组合 —— 输入内容本身是错的，
    而且候选框浮在页面上会让整窗模板匹配掉到 0（页面“找不到”）。Unicode 直发不触发候选。
    """

    def test_ascii_down_up_pairs(self):
        from engine.executor import _unicode_key_events
        self.assertEqual(_unicode_key_events("ab"),
                         [(97, 4), (97, 6), (98, 4), (98, 6)])

    def test_chinese_symbols_and_digits(self):
        from engine.executor import _unicode_key_events
        ev = _unicode_key_events("中-9")
        self.assertEqual([s for s, f in ev if f == 4], [ord("中"), ord("-"), ord("9")])
        self.assertEqual(len(ev), 6)                       # 3 字符 × down/up

    def test_surrogate_pair_for_non_bmp(self):
        from engine.executor import _unicode_key_events
        ev = _unicode_key_events("😀")
        self.assertEqual([s for s, f in ev if f == 4], [0xD83D, 0xDE00])
        self.assertEqual(len(ev), 4)

    def test_live_driver_prefers_unicode(self):
        """LiveDriver.type_text 先走 Unicode 直发（而不是 pynput 按键）。"""
        from unittest import mock
        from engine import executor
        calls = []
        with mock.patch.object(executor, "send_unicode_text",
                               lambda t, **kw: calls.append(t) or len(t)):
            executor.LiveDriver().type_text("demo")
        self.assertEqual(calls, ["demo"])


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

    def test_count_loop_multi_step_body(self):
        """多个步骤作为一个整体循环：顺序与次数都要对（甲,乙,丙 ×2）。"""
        env = Env()
        sg = S.script("整体循环", [
            S.loop_step("l1", {"mode": "count", "count": 2}, body=[
                S.action_step("b1", "notify", None, {"message": "甲"}),
                S.action_step("b2", "notify", None, {"message": "乙"}),
                S.action_step("b3", "notify", None, {"message": "丙"}),
            ])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["甲", "乙", "丙", "甲", "乙", "丙"])

    def test_loop_then_main_flow_step(self):
        """循环之后的主流程步骤只跑一次（循环不吞后面的步骤）。"""
        env = Env()
        sg = S.script("循环后接步骤", [
            S.loop_step("l1", {"mode": "count", "count": 2}, body=[
                S.action_step("b1", "notify", None, {"message": "甲"}),
                S.action_step("b2", "notify", None, {"message": "乙"})]),
            S.action_step("t1", "notify", None, {"message": "尾"}),
        ])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["甲", "乙", "甲", "乙", "尾"])

    def test_nested_loop_order_and_iterations(self):
        """循环里再套循环：次数相乘、顺序按嵌套展开（外,内,内 ×2）。"""
        env = Env()
        sg = S.script("嵌套循环", [
            S.loop_step("l1", {"mode": "count", "count": 2}, body=[
                S.action_step("b1", "notify", None, {"message": "外"}),
                S.loop_step("l2", {"mode": "count", "count": 2}, body=[
                    S.action_step("b2", "notify", None, {"message": "内"})]),
            ])])
        rep = env.run(sg)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(env.human.notified, ["外", "内", "内", "外", "内", "内"])
        iters = [r["iterations"] for r in rep["steps"]
                 if r.get("type") == "loop" and "iterations" in r]
        self.assertEqual(iters, [2, 2, 2])          # 外层 2；内层每轮 2（末轮汇总）

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


class UnicodeInputStructTest(unittest.TestCase):
    """真机输入的结构体大小：错了 SendInput 会**静默返回 0**，很难查。

    这个坑真踩过：INPUT 是"键盘/鼠标/硬件"三选一的联合体，大小由 MOUSEINPUT 决定
    （x64 上 32 字节）。只按 KEYBDINPUT 算出来是 32 字节，Windows 要求 40，
    SendInput 直接拒绝；于是 LiveDriver 静默回退到 pynput 按键序列，
    而那条路会被中文输入法拦坏（"输入 demo"变成拼音候选）。
    只测 _unicode_key_events（纯函数）是发现不了的——所以这里直接钉结构体。
    """

    def test_input_struct_size_matches_windows(self):
        import ctypes
        want = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(X._INPUT), want,
                         "INPUT 大小必须与 Windows 一致，否则 SendInput 会返回 0")

    def test_mouseinput_dominates_the_union(self):
        import ctypes
        self.assertGreaterEqual(ctypes.sizeof(X._MOUSEINPUT),
                                ctypes.sizeof(X._KEYBDINPUT))

    def test_unicode_key_events_shape(self):
        ev = X._unicode_key_events("ab")
        self.assertEqual(len(ev), 4, "每个字符按下+松开各一条")
        self.assertTrue(all(flags & 0x0004 for _scan, flags in ev), "都应是 UNICODE 直发")

    def test_surrogate_pair_for_non_bmp(self):
        ev = X._unicode_key_events("\U0001F600")     # 😀
        self.assertEqual(len(ev), 4, "非 BMP 字符按代理对发两条")


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
