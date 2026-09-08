# -*- coding: utf-8 -*-
"""诊断 run 阶段 Edge 窗口 raise 失败：模拟 run 的完整步骤并逐步打印"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui, win32con, win32api
import m0_trial

target = m0lib.Target.load("smoke/targets/dyn.json")
hwnd = int(target.page["hwnd"])
print("hwnd", hwnd, "title:", win32gui.GetWindowText(hwnd),
      "iconic:", win32gui.IsIconic(hwnd), "visible:", win32gui.IsWindowVisible(hwnd))

# 模仿 run：先移动
import random
rng = random.Random(7)
outer = win32gui.GetWindowRect(hwnd)
print("outer", outer)
nx, ny = 300, 200
win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, nx, ny, 0, 0, win32con.SWP_NOSIZE)
print("after move rect", win32gui.GetWindowRect(hwnd))
m0lib.bring_to_foreground(hwnd)
time.sleep(0.8)
ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
print("topmost?", bool(ex & win32con.WS_EX_TOPMOST), "fg?", win32gui.GetForegroundWindow() == hwnd)
rect = win32gui.GetWindowRect(hwnd)
ref = m0lib.grab_window_content(hwnd)
print("ref std:", None if ref is None else round(float(ref.std()), 1))
if ref is not None:
    sim = m0lib.pixel_sim(m0lib.grab_screen(rect), ref)
    print("sim:", round(sim, 3))
    r = m0lib.ocr_run(ref)
    print("PW texts:", r["txts"][:6])
grab = m0lib.grab_screen(rect)
r2 = m0lib.ocr_run(grab)
print("screen texts:", r2["txts"][:10])
