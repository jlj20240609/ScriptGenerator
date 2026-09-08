# -*- coding: utf-8 -*-
"""诊断：目标窗口客户区截图 + OCR 结果 + 当时前台窗口是谁"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
import m0_trial
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

names = {"login": "M0 演示登录", "erp": "M0 ERP 查询", "dyn": "M0 动态监控台",
         "note": "desktop-note.txt", "icons": "icons - "}
outdir = os.path.join(os.path.dirname(__file__), "data", "diag")
os.makedirs(outdir, exist_ok=True)
for name, sub in names.items():
    wins = m0_trial.find_window_by_title(sub)
    if not wins:
        print(name, "窗口未找到"); continue
    hwnd = wins[0]
    import win32gui
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print(name, "SetForeground失败", e)
    m0lib.time.sleep(0.8)
    fg = win32gui.GetForegroundWindow()
    crect = m0_trial.client_rect_of(hwnd)
    bgr = m0lib.grab_screen(crect)
    p = os.path.join(outdir, name + ".png")
    m0lib.save_png(p, bgr)
    r = m0lib.ocr_run(bgr)
    print("%-6s rect=%s fg_is_target=%s boxes=%d texts=%s" %
          (name, crect, fg == hwnd, len(r["txts"]), r["txts"][:8]))
