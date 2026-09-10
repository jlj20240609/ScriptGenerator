# -*- coding: utf-8 -*-
"""跑批器（engine/scripts/bench.py）离线部分：原始数据落盘/续跑、汇总口径、单轮异常隔离。

这些是 M2-1 的地基：跑批是几十分钟级的任务，数据必须**每轮立刻落盘**（能续跑），
单轮出问题**不能**中断整批，汇总口径要与 DoD 一致（无人工介入 / 误报）。
不触屏：只用内存构造的轮次结果与临时文件。
"""
import random
import tempfile
import unittest
from pathlib import Path

from engine.executor import RunConfig
from engine.scripts import bench


def _row(case="a", round_no=1, status="ok", prompts=0, kinds=None, assert_ok=True,
         calib=None, tpl_ms=120.0, ms=5000.0, error=None, shot="", prompts_manual=None):
    return {"case": case, "round": round_no, "ts": 0.0, "perturb": "", "status": status,
            "prompts": prompts, "prompt_kinds": kinds or [], "assert_ok": assert_ok,
            "prompts_manual": prompts if prompts_manual is None else prompts_manual,
            "assert_detail": [], "calib": calib or [], "ms": ms, "methods": {},
            "tpl_ms_median": tpl_ms, "move": None, "error": error, "counters": {},
            "shot": shot}


class RawStoreTest(unittest.TestCase):
    """原始结果落盘：容忍坏行、能续跑（跑批被打断不该丢机时）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "raw.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(bench.load_raw(self.path), [])

    def test_bad_lines_tolerated(self):
        """半行/坏行/无 case 的行都要跳过——一行坏了不能连累整批数据。"""
        self.path.write_text('{"case":"a","round":1}\n\n{坏行\n{"case":"b","round":2}\n'
                             '{"nope":1}\n', encoding="utf-8")
        got = bench.load_raw(self.path)
        self.assertEqual([bench.row_key(r) for r in got], [("a", 1), ("b", 2)])

    def test_append_then_resume_skips_done(self):
        bench.append_raw(_row("a", 1), self.path)
        bench.append_raw(_row("a", 2), self.path)
        done = {bench.row_key(r) for r in bench.load_raw(self.path)}
        self.assertIn(("a", 1), done)
        self.assertNotIn(("a", 3), done)
        self.assertEqual(len(bench.load_raw(self.path)), 2)


class SummarizeTest(unittest.TestCase):
    """汇总口径与 M2 DoD 一致：成功率 / **无人工介入率** / 误报率。"""

    def setUp(self):
        self.rows = [
            _row("a", 1, ms=5000.0),                                   # 干净通过
            _row("a", 2, prompts=1, kinds=["not_found"], tpl_ms=140.0,
                 calib=["widget_not_found"], ms=9000.0),               # 成功但用了人工
            _row("a", 3, status="failed", prompts=1, kinds=["not_found"],
                 assert_ok=False, tpl_ms=None, ms=8000.0),             # 失败
            _row("b", 1, assert_ok=False, tpl_ms=100.0, ms=4000.0),    # 误报（报 ok 但断言不成立）
        ]
        self.st = bench.summarize(self.rows)

    def test_totals(self):
        self.assertEqual(self.st["total"], 4)
        self.assertEqual(self.st["ok"], 3, "失败那轮不算成功")
        # 口径按 DoD：无人工介入 = ok 且零人工交互（b#1 是"干净但结果错"，仍然打扰不到人，
        # 所以算无人工介入，由误报率这条独立指标去抓它）
        self.assertEqual(self.st["clean"], 2)
        self.assertEqual(self.st["misreport"], 1, "b#1 是误报")
        self.assertAlmostEqual(self.st["clean_rate"], 50.0, places=4)

    def test_misreport_round_is_listed(self):
        """误报轮次（干净但断言不成立）也必须进未达标明细——否则等于没记。"""
        problems = [p for s in self.st["by_case"].values() for p in s["problems"]]
        self.assertTrue(any("[b #1]" in p and "断言=False" in p for p in problems), problems)

    def test_by_case(self):
        s = self.st["by_case"]["a"]
        self.assertEqual((s["n"], s["ok"], s["clean"]), (3, 2, 1))
        self.assertEqual(s["bad_rounds"], [2, 3], "有人工的轮号要列出来")
        self.assertEqual(s["calib"], 1)
        self.assertEqual(s["med_ms"], 140.0, "中位取上中位（[120,140] → 140）")

    def test_problems_locate_round_and_reason(self):
        problems = [p for s in self.st["by_case"].values() for p in s["problems"]]
        self.assertEqual(len(problems), 3, "a#2、a#3、b#1 都要能定位")
        joined = "\n".join(problems)
        self.assertIn("[a #2]", joined)
        self.assertIn("widget_not_found", joined)

    def test_slowest(self):
        self.assertEqual(self.st["slowest"][0]["round"], 2, "最慢的是 a#2 那条 9000ms")

    def test_empty(self):
        st = bench.summarize([])
        self.assertEqual(st["total"], 0)
        self.assertEqual(st["clean_rate"], 0.0)


class RoundIsolationTest(unittest.TestCase):
    """单轮出问题不能中断整批（机时太贵，前面的结果必须留在盘上）。"""

    def test_missing_asset_records_error_not_raise(self):
        case = {"kind": "fixture", "asset": "__no_such_asset__", "title": "x",
                "expect_all": [], "reset": "none"}
        row = bench.run_round("ghost", case, hwnd=0, round_no=1, cfg=RunConfig(),
                              perturb="", rng=random.Random(0))
        self.assertIsNone(row["status"])
        self.assertIn("脚本资产缺失", row["error"])
        self.assertEqual(bench.row_key(row), ("ghost", 1))
        self.assertEqual(row["shot"], "", "资产缺失时没有截图，字段也要在")


def _log_loc(log, step_id="s1", event="locate_widget", method="ocr_text", top3=None, **kw):
    """按官方接口往 logger 里塞一条定位记录（executor 的 row 结构）。"""
    row = {"step_id": step_id, "event": event, "method": method, "confidence": 0.9,
           "rect": None}
    if top3 is not None:
        row["top3"] = top3
    row.update(kw)
    log.log_loc(row)


class ManualPromptScopeTest(unittest.TestCase):
    """口径：脚本自己设计的「提示我」不算人工介入，异常求助才算。

    这条真踩过：案例 ① 的脚本设计就是"故意输错密码 → 提示我"，若把 notify 也算作人工介入，
    它每轮都会被判成"需要人处理"，无人工介入率永远为 0。
    """

    def test_notify_is_not_manual(self):
        h = bench.BenchHuman()
        h.notify("密码输错了，请重新输入")
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.manual_count(), 0, "「提示我」是脚本设计的一部分")

    def test_not_found_and_outcome_fail_are_manual(self):
        h = bench.BenchHuman()
        h.notify("提示")
        h.prompt_not_found("没找到 X", "X")
        h.prompt_outcome_fail("做完没看到 Y")
        self.assertEqual(h.manual_count(), 2)

    def test_two_manual_scopes(self):
        """两个口径都算、都报（不擅自替用户选）。

        严格 = 文档里拍板的验收定义（ok 且全程没有任何 confirm_request，含脚本自带的
        「提示我」）；宽松 = 只把异常求助算人工介入。差值就是"脚本自带的提示我"造成的。
        """
        rows = [_row("a", 1, prompts=1, kinds=["notify"], prompts_manual=0),      # 只有提示
                _row("a", 2, prompts=1, kinds=["not_found"], prompts_manual=1)]
        st = bench.summarize(rows)
        s = st["by_case"]["a"]
        self.assertEqual(s["clean"], 0, "严格口径：弹过「提示我」就不算无人工介入")
        self.assertEqual(s["clean_manual"], 1, "宽松口径：只有异常求助才算")
        self.assertEqual(s["bad_rounds"], [1, 2], "严格口径下两轮都要列进明细")

    def test_legacy_row_without_field_falls_back(self):
        old = _row("a", 1, prompts=1, kinds=["not_found"])
        old.pop("prompts_manual")
        st = bench.summarize([old])
        self.assertEqual(st["by_case"]["a"]["clean"], 0, "老数据缺字段时按 prompts 兜底")


class EvidenceCollectTest(unittest.TestCase):
    """定位证据采集（M2-WP3 调参的输入）：从定位日志提取②a候选，真值按录制框判定。"""

    def test_iter_steps_recurses_branches(self):
        sg = {"steps": [
            {"id": "1", "action": "click", "target": {}},
            {"id": "2", "type": "condition", "condition": {},
             "then": [{"id": "2a", "action": "click", "target": {}}], "else": []},
            {"id": "3", "type": "loop", "loop": {},
             "body": [{"id": "3a", "action": "click", "target": {}}]}]}
        self.assertEqual([s for s, _ in bench.iter_steps(sg)], ["1", "2", "2a", "3", "3a"])

    def test_collect_evidence_marks_truth_and_far_limit(self):
        sg = {"steps": [{"id": "s1", "action": "click",
                         "target": {"rect_in_page": [100, 200, 80, 30]}}]}
        log = bench.CountLogger()
        _log_loc(log, rect=[290, 350, 80, 30],
                 top3=[[[100, 200, 80, 30], 0.95, 5, True],
                       [[600, 500, 80, 30], 0.88, 320, None]])
        ev = bench.collect_evidence("demo", 3, sg, log)
        self.assertEqual(len(ev), 1)
        s = ev[0]
        self.assertEqual(s["id"], "demo_3_s1")
        self.assertEqual(s["group"], "demo")
        self.assertEqual(s["truth_index"], 0, "落在录制框中心的候选就是真值")
        self.assertEqual(s["cands"][1]["dist"], 320)
        self.assertAlmostEqual(s["far_limit"], 160.0, places=4, msg="max(2*80, 2*30, 160)=160")
        self.assertTrue(s["has_target"])

    def test_no_candidates_no_evidence(self):
        sg = {"steps": [{"id": "s1", "action": "click",
                         "target": {"rect_in_page": [0, 0, 10, 10]}}]}
        log = bench.CountLogger()
        _log_loc(log, method="none", top3=[])
        self.assertEqual(bench.collect_evidence("demo", 1, sg, log), [])

    def test_evidence_is_tunable(self):
        """采出来的证据能直接喂给调参器（格式闭环，不能只是"看起来像"）。"""
        from engine import tuning as T
        sg = {"steps": [{"id": "s1", "action": "click",
                         "target": {"rect_in_page": [100, 200, 80, 30]}}]}
        log = bench.CountLogger()
        _log_loc(log, top3=[[[100, 200, 80, 30], 0.95, 5, True]])
        ev = bench.collect_evidence("demo", 1, sg, log)
        r = T.evaluate(ev, T.DEFAULT_PARAMS)
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["hits"], 1, r)


class CountLoggerInterfaceTest(unittest.TestCase):
    """跑批用的 logger 必须是官方接口（executor 调 log_loc）。

    这条曾经真的炸过：CountLogger 写成了 log()，与 executor 对不上，每次定位都抛
    AttributeError → 跑批 100% failed。合成单测用的是官方 MemoryLogger，绕过了这个替身，
    所以只有真机跑批才暴露。这里直接盯接口。
    """

    def test_log_loc_shape_and_downstream(self):
        log = bench.CountLogger()
        log.log_loc({"step_id": "s1", "event": "locate_widget", "method": "ocr_text",
                     "confidence": 0.9, "rect": [1, 2, 3, 4], "elapsed_ms": 12.0,
                     "top3": [[[1, 2, 3, 4], 0.9, 5, None]]})
        self.assertEqual(len(log.rows), 1)
        self.assertIn("ts", log.rows[0])
        self.assertEqual(log.methods(), {"locate_widget:ocr_text": 1})
        self.assertEqual(log.tail("s1")[0]["step_id"], "s1")
        self.assertEqual(log.tail(None)[0]["event"], "locate_widget")
        sg = {"steps": [{"id": "s1", "action": "click",
                         "target": {"rect_in_page": [1, 2, 3, 4]}}]}
        ev = bench.collect_evidence("demo", 1, sg, log)
        self.assertEqual(len(ev), 1, "证据采集要能从官方结构的 row 里读到顶层的 top3")
        self.assertEqual(ev[0]["truth_index"], 0)

    def test_tpl_ms_reads_top_level_elapsed(self):
        log = bench.CountLogger()
        log.log_loc({"step_id": "s1", "event": "locate_widget", "method": "tpl",
                     "elapsed_ms": 33.0})
        self.assertEqual(log.tpl_ms(), [33.0])


class DeadlinedDriverTest(unittest.TestCase):
    """单轮墙钟上限：屏幕不可用/卡住的一轮不能磨掉几十分钟。

    实测踩到：锁屏后抓屏抛 `BitBlt: 拒绝访问`，执行器照常走"重试→提示→再试"的长链路，
    一轮花掉 25 分钟；没人管时整批就死在那儿（卡了 8 小时才发现）。
    """

    class _Inner:
        hwnd = 7

        def __init__(self):
            self.calls = 0

        def grab_screen(self):
            self.calls += 1
            return "shot"

    def test_passes_through_before_deadline(self):
        inner = self._Inner()
        d = bench.DeadlinedDriver(inner, 60)
        self.assertEqual(d.grab_screen(), "shot")
        self.assertEqual(inner.calls, 1)

    def test_raises_after_deadline(self):
        import time
        d = bench.DeadlinedDriver(self._Inner(), 0.05)
        time.sleep(0.09)                                      # 让它过期
        with self.assertRaises(Exception) as cm:
            d.grab_screen()
        self.assertTrue("round_timeout" in str(cm.exception)
                        or getattr(cm.exception, "code", "") == "round_timeout", cm.exception)

    def test_zero_means_no_limit(self):
        d = bench.DeadlinedDriver(self._Inner(), 0)
        self.assertEqual(d.grab_screen(), "shot")

    def test_forwards_other_attributes(self):
        d = bench.DeadlinedDriver(self._Inner(), 0)
        self.assertEqual(d.hwnd, 7, "其余属性要转发给真实 driver")


class ScreenErrorRowTest(unittest.TestCase):
    """屏幕不可用的轮次要被标出来，跑批器才能及时停批而不是空转。"""

    def test_screen_error_flagging(self):
        case = {"kind": "fixture", "asset": "__no_such_asset__", "title": "x",
                "expect_all": [], "reset": "none"}
        row = bench.run_round("ghost", case, hwnd=0, round_no=1, cfg=RunConfig(),
                              perturb="", rng=random.Random(0))
        self.assertFalse(row["screen_error"], "资产缺失不算屏幕不可用")


class SharedFixtureWindowTest(unittest.TestCase):
    """关别的案例窗口时，不能把"自己那一份"也关掉。

    实测踩到：`login` 与 `login_full` **共用同一个 fixture 与窗口标题**（web-login.html /
    "M0 演示登录"）。按案例名去重就会把刚拿到的那个窗口关掉 —— login_full 第 21 轮因此
    窗口句柄失效（GetWindowRect 1400）、35 次定位全灭、11 次求助。
    """

    def test_shared_fixture_is_excluded(self):
        self.assertEqual(bench.CASES["login"]["fixture"],
                         bench.CASES["login_full"]["fixture"],
                         "前提：这两个案例确实共用同一个 fixture（否则这个测试就没意义了）")
        others = bench.other_case_names("login_full")
        self.assertNotIn("login", others, "共用窗口的案例不能被列入待关闭名单")
        self.assertIn("erp", others, "不共用 fixture 的案例应该照常关闭")

    def test_never_includes_self(self):
        for name in bench.CASES:
            self.assertNotIn(name, bench.other_case_names(name))

    def test_real_case_has_no_fixture(self):
        """真实客户端案例没有 fixture，不该出现在待关闭名单里。"""
        self.assertNotIn("real", bench.other_case_names("erp"))


if __name__ == "__main__":
    unittest.main()
