# -*- coding: utf-8 -*-
"""CLI 冒烟：validate / selftest 子进程级验证（引擎可独立运行）。"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSET = Path(__file__).parent / "assets" / "analysis_example.sgscript.json"


def _py():
    return sys.executable


class CliTest(unittest.TestCase):
    def test_validate_ok(self):
        r = subprocess.run([_py(), "-m", "engine", "validate", str(ASSET)],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("通过", r.stdout)

    def test_validate_rejects_bad(self):
        bad = ROOT / "engine" / "tests" / "assets" / "bad_tmp.sgscript.json"
        try:
            bad.write_text('{"version":"1.0","steps":[{"id":"a","type":"nope"}]}',
                           encoding="utf-8")
            r = subprocess.run([_py(), "-m", "engine", "validate", str(bad)],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            self.assertNotEqual(r.returncode, 0)
        finally:
            bad.unlink(missing_ok=True)

    def test_selftest(self):
        r = subprocess.run([_py(), "-m", "engine", "selftest"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
