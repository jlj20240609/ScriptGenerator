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
         calib=None, tpl_ms=120.0, ms=5000.0, error=None, shot=""):
    return {"case": case, "round": round_no, "ts": 0.0, "perturb": "", "status": status,
            "prompts": prompts, "prompt_kinds": kinds or [], "assert_ok": assert_ok,
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


if __name__ == "__main__":
    unittest.main()
