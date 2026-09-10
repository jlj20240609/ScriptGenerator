# -*- coding: utf-8 -*-
"""诊断：feed 案例（强动态页）预热后整窗模板到底命中成什么样。

跑法：python smoke/diag/probe_feed_dynamic.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, locator, schema  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

capture.init_dpi_aware()
case = B.CASES["feed"]
hwnd = B.ensure_case_window(case)
print(f"hwnd={hwnd}")
B.reset_page(hwnd, case)
B.ensure_foreground(hwnd)
sg = schema.load(B.ASSETS / "feed.sgscript.json")
ps = sg["steps"][0]["target"]["page"]
tpl = None
import numpy as np  # noqa: E402

prev = None
for wait in (0.0, 2.0, 2.0, 2.0):
    time.sleep(wait)
    screen = capture.grab_screen()
    r = locator.locate_page(screen, ps)
    det = (r.get("detail") or {}).get("page_tpl") or {}
    print(f"累计等待 {wait:>4}s: ok={r['ok']} method={r['method']:<9} "
          f"score={det.get('score')} sim={det.get('sim')} soft={r.get('soft')} "
          f"verdict={det.get('verdict')} rect={r.get('rect') if r['ok'] else None}")
    if prev is not None and prev.shape == screen.shape:
        d = int((__import__('cv2').absdiff(prev, screen).max(axis=2) > 24).sum())
        print(f"            与上一帧差异像素={d}")
    prev = screen
