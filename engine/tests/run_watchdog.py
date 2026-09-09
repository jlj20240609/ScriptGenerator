# -*- coding: utf-8 -*-
"""引擎测试串行运行器（实时进度）：python -u engine/tests/run_watchdog.py

逐测试串行执行并实时打印（规避 unittest 输出缓冲与并发线程对 cv2 全局状态的干扰）。
测试内自陷 KeyboardInterrupt 无意义——进度文件即卡点定位依据。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(str(Path(__file__).resolve().parent), pattern="test_*.py",
                            top_level_dir=str(Path(__file__).resolve().parent))
    tests = list(flatten(suite))
    print("TOTAL %d tests" % len(tests), flush=True)
    ok_n = fail_n = 0
    fails = []
    for idx, tc in enumerate(tests, 1):
        name = tc.id().split(".")[-1]
        print("[%02d/%02d] RUN %s" % (idx, len(tests), name), flush=True)
        from engine import matcher
        matcher.ocr_reset()          # 防测试间熔断状态污染
        matcher.cv_reset()
        res = unittest.TestResult()
        try:
            tc.run(res)
        except Exception as e:      # noqa: BLE001 —— 单测试失败不中断整个套件
            res.addError(tc, sys.exc_info())
        if res.wasSuccessful():
            ok_n += 1
            print("[%02d/%02d] OK   %s" % (idx, len(tests), name), flush=True)
        else:
            fail_n += 1
            fails.append(name)
            detail = "; ".join(str(e[1])[:220] for e in (res.errors + res.failures))
            print("[%02d/%02d] FAIL %s :: %s" % (idx, len(tests), name, detail),
                  flush=True)
    print("\nSUMMARY ok=%d fail=%d" % (ok_n, fail_n), flush=True)
    if fails:
        print("PROBLEMS: %s" % ", ".join(fails), flush=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
