# -*- coding: utf-8 -*-
"""诊断：erp 案例的整窗模板为什么命中不了（跑批里 30 次 page_tpl 全灭）。

跑法：python smoke/diag/probe_erp_asset.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, locator, matcher, schema  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

capture.init_dpi_aware()
case = B.CASES["erp"]

hwnd = B.ensure_case_window(case)
print(f"hwnd={hwnd} iconic={capture.is_iconic(hwnd)}")
B.reset_page(hwnd, case)
time.sleep(2.0)
l, t, r, b = capture.window_rect(hwnd)
win = [l, t, r - l, b - t]
print(f"窗口屏幕矩形={win}（{win[2]}x{win[3]}）")

sg = schema.load(B.ASSETS / f"{case['asset']}.sgscript.json")
step = sg["steps"][0]
ps = (step.get("target") or {}).get("page")
print(f"资产：{case['asset']}.sgscript.json 步骤数={len(sg['steps'])} "
      f"目标文字={((step.get('target') or {}).get('text'))!r}")
if not ps:
    print("资产里没有页面模板（target.page 缺失）→ 这就是根因")
    sys.exit(1)
print(f"资产页面模板 size={ps.get('size')} rect_in_screen={ps.get('rect_in_screen')}")
tpl = matcher.dataurl_to_bgr(ps["image"])
print(f"模板图 {tpl.shape[1]}x{tpl.shape[0]}")

screen = capture.grab_screen()
print(f"整屏 {screen.shape[1]}x{screen.shape[0]}")
res = locator.locate_page(screen, ps)
print(f"\nlocate_page ok={res['ok']} method={res['method']} rect={res.get('rect')} "
      f"conf={res.get('confidence')}")
print("detail:")
print(json.dumps(res.get("detail"), ensure_ascii=False, indent=2)[:1500])

# 直接比较：把模板和当前窗口区域做一次同源比对（看是不是"内容变了"还是"位置/尺寸变了"）
crop = capture.grab_screen(win)
if crop is not None and tpl is not None:
    fixed = matcher.bgr_to_dataurl if hasattr(matcher, "bgr_to_dataurl") else None
    import cv2
    a = cv2.resize(tpl, (crop.shape[1], crop.shape[0]))
    sim = matcher.pixel_sim(crop, a, size=(240, 150), thr=16.0)
    print(f"\n同尺寸对齐后的同源度={sim:.4f}（低说明页面内容/样式变了，而不是位置问题）")
    r2 = matcher.find_template(screen, tpl, score_thr=0.5)
    print(f"整屏模板匹配 best={r2['best_score']:.4f} scale={r2.get('scale')} "
          f"rect={r2['rect']}")
