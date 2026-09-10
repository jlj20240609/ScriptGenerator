# -*- coding: utf-8 -*-
"""M2-WP8 跨分辨率辅助脚本的离线测试：环境读取、归档（拒绝覆盖/带 meta）、对比报告。"""
import json
import tempfile
import unittest
from pathlib import Path

from engine.scripts import bench as B
from engine.scripts import dpi_report as D


def _row(case, round_no, status="ok", manual=0, assert_ok=True, tpl_ms=100.0):
    return {"case": case, "round": round_no, "status": status, "prompts": manual,
            "prompts_manual": manual, "prompt_kinds": [], "assert_ok": assert_ok,
            "calib": [], "ms": 1000.0, "methods": {}, "tpl_ms_median": tpl_ms,
            "move": None, "error": None, "counters": {}, "shot": ""}


class EnvTest(unittest.TestCase):
    def test_env_shape(self):
        e = D.env_info()
        self.assertGreater(e["dpi"], 0)
        self.assertIn(e["percent"], (100, 125, 150, 175, 200, 225, 250, 300))
        self.assertEqual(len(e["phys"]), 2)
        self.assertEqual(len(e["logical"]), 2)
        self.assertLessEqual(e["logical"][0], e["phys"][0], "逻辑尺寸不该大于物理尺寸")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "raw.jsonl"
        self.src.write_text("\n".join(json.dumps(_row("a", i)) for i in range(1, 4)) + "\n",
                            encoding="utf-8")
        self._old_dir, self._old_raw = D.ARCH_DIR, B.RAW
        D.ARCH_DIR = self.dir / "arch"
        B.RAW = self.src

    def tearDown(self):
        D.ARCH_DIR, B.RAW = self._old_dir, self._old_raw
        self.tmp.cleanup()

    def test_archive_writes_rows_and_meta(self):
        p = D.archive("125dpi")
        self.assertTrue(p.exists())
        rows, meta = D.load_archive("125dpi")
        self.assertEqual(len(rows), 3)
        self.assertEqual(meta["name"], "125dpi")
        self.assertEqual(meta["rows"], 3)
        self.assertIn("percent", meta["env"], "meta 里要有当时的缩放百分比")

    def test_archive_refuses_overwrite(self):
        D.archive("125dpi")
        with self.assertRaises(FileExistsError):
            D.archive("125dpi")
        D.archive("125dpi", force=True)          # --force 才允许

    def test_archive_missing_source(self):
        B.RAW = self.dir / "nope.jsonl"
        with self.assertRaises(FileNotFoundError):
            D.archive("x")


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.a = [(_row("login", i) for i in range(1, 11))]
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old_doc = D.DOC
        D.DOC = self.dir / "report.md"

    def tearDown(self):
        D.DOC = self._old_doc
        self.tmp.cleanup()

    def _arch(self, name, percent, clean_rounds):
        rows = [_row("login", i, manual=0 if i <= clean_rounds else 1) for i in range(1, 11)]
        return (name, rows, {"name": name, "rows": len(rows),
                             "archived_at": "2026-01-01 00:00:00",
                             "env": {"percent": percent, "dpi": int(percent * 0.96),
                                     "phys": [1920, 1200], "logical": [1920, 1200]}})

    def test_table_has_all_envs_and_cases(self):
        table = D.compare_table([self._arch("125dpi", 125, 10), self._arch("150dpi", 150, 8)])
        self.assertIn("125dpi", table)
        self.assertIn("150dpi", table)
        self.assertIn("login", table)

    def test_doc_reports_pass_and_fail(self):
        doc = D.write_doc([self._arch("125dpi", 125, 10), self._arch("150dpi", 150, 8)])
        self.assertIn("是否全部达到 DoD ≥90%", doc)
        self.assertIn("**否**", doc, "150% 那批只有 80% 干净，应判不达标")
        self.assertIn("复现方式", doc)

    def test_doc_all_pass(self):
        doc = D.write_doc([self._arch("125dpi", 125, 10), self._arch("150dpi", 150, 10)])
        self.assertIn("**是**", doc)


if __name__ == "__main__":
    unittest.main()
