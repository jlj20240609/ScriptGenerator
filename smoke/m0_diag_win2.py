# -*- coding: utf-8 -*-
"""PrintWindow 内容校验 + 串台窗口清理 + 重开 icons 并校验内容"""
import sys, os
import ctypes
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui, win32con

user32 = ctypes.windll.user32


def pw(hwnd, flags=2):
    import win32ui
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    hwnd_dc = user32.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    rc = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
    if not rc:
        rc = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
    bits = bmp.GetBitmapBits(True)
    info = bmp.GetInfo()
    import numpy as np
    arr = np.ascontiguousarray(np.frombuffer(bits, np.uint8).reshape(
        info["bmHeight"], info["bmWidth"], 4)[:, :, 2::-1])
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC(); mfc_dc.DeleteDC()
    user32.ReleaseDC(hwnd, hwnd_dc)
    return arr


import m0_trial

# 1) 关闭串台 explorer（非桌面 Process Manager；桌面窗口 hwnd 是 root 类型）
def close_title(sub):
    for h in m0_trial.find_window_by_title(sub):
        cls = win32gui.GetClassName(h)
        if cls == "Progman":
            continue
        win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
        print("close", sub, h)

for sub in ("icons - ", "desktop-note.txt"):
    close_title(sub)
time.sleep(1.5)

# 2) 重开 notepad 与 icons
import subprocess as sp
sp.Popen(["notepad.exe", os.path.join(os.path.dirname(__file__), "fixtures", "desktop-note.txt")])
sp.Popen(["explorer.exe", os.path.join(os.path.dirname(__file__), "fixtures", "icons")])
time.sleep(6)

for name, sub, expect in [("note", "desktop-note.txt", "关键行"),
                          ("icons", "icons - ", "项目资料")]:
    wins = m0_trial.find_window_by_title(sub)
    if not wins:
        print(name, "not found"); continue
    hwnd = wins[0]
    rect = win32gui.GetWindowRect(hwnd)
    img = pw(hwnd)
    r = m0lib.ocr_run(img)
    ok = any(expect in t for t in r["txts"]) or any(m0lib.text_similar(t, expect) > 0.75 for t in r["txts"])
    print("%s hwnd=%d rect=%s PW_std=%.1f contains(%s)=%s" %
          (name, hwnd, rect, float(img.std()), expect, ok))
    print("   texts:", r["txts"][:14])
