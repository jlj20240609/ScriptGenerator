# -*- coding: utf-8 -*-
"""run 同路径探测：calc/tk 移动+置顶后是否真的可见"""
import sys, os, time, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui, win32con
import m0_trial

for name, needle in (("calc", "8"), ("tk", "M0桌面验证")):
    t = m0lib.Target.load("smoke/targets/%s.json" % name)
    hwnd = int(t.page["hwnd"])
    # 1) 与 do_run 相同的移动
    rng = random.Random(7)
    mm = m0_trial.monitor_rect(1)
    outer = win32gui.GetWindowRect(hwnd)
    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
    nx = rng.randint(20, max(20, mm[2] - ow - 20))
    ny = rng.randint(20, max(20, mm[3] - oh - 20))
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, nx, ny, 0, 0, win32con.SWP_NOSIZE)
    time.sleep(0.45)
    # 2) 与 do_run 相同的 raise
    raised = m0lib.bring_to_foreground(hwnd)
    time.sleep(0.45)
    rect = win32gui.GetWindowRect(hwnd)
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    print("%s: raised=%s topmost=%s fg=%s rect=%s" % (
        name, raised, bool(ex & win32con.WS_EX_TOPMOST),
        win32gui.GetForegroundWindow() == hwnd, rect))
    r = m0lib.ocr_run(m0lib.grab_screen(rect))
    hit = any(m0lib.text_similar(x, needle) >= 0.75 for x in r["txts"])
    print("   区域OCR(%d): %s ... contains=%s" % (len(r["txts"]), r["txts"][:6], hit))
