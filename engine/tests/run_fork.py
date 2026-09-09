# -*- coding: utf-8 -*-
"""引擎测试 fork 运行器：每个测试独立子进程（彻底隔离 cv2/ORT 全局状态）。

用法：python -u engine/tests/run_fork.py [module.Class[.method] ...]
  不带参数 = 全量发现。超时默认 240s/测试。
"""
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


def main(argv):
    loader = unittest.TestLoader()
    if argv:
        tests = []
        for name in argv:
            tests.extend(flatten(loader.loadTestsFromName(name)))
    else:
        suite = loader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
        tests = list(flatten(suite))
    print("TOTAL %d tests (forked)" % len(tests), flush=True)
    ok_n = fail_n = 0
    fails = []
    for idx, tc in enumerate(tests, 1):
        name = tc.id()
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, "-u", "-m", "unittest", name],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=240)
        except subprocess.TimeoutExpired:
            fail_n += 1
            fails.append(name)
            print("[%02d/%02d] TIMEOUT %s" % (idx, len(tests), name), flush=True)
            continue
        if r.returncode == 0:
            ok_n += 1
            print("[%02d/%02d] OK   %s (%.0fs)" % (idx, len(tests), name,
                                                   time.time() - t0), flush=True)
        else:
            fail_n += 1
            fails.append(name)
            tail = (r.stderr or r.stdout or "")[-300:].replace("\n", " | ")
            print("[%02d/%02d] FAIL %s :: %s" % (idx, len(tests), name, tail),
                  flush=True)
    print("\nSUMMARY ok=%d fail=%d" % (ok_n, fail_n), flush=True)
    if fails:
        print("PROBLEMS: %s" % ", ".join(fails), flush=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
