# -*- coding: utf-8 -*-
"""屏幕抓取 vs PrintWindow 像素级对比 + 与登录页窗口交叉验证（找显示错位）"""
import sys, os, ctypes
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui
import numpy as np

user32 = ctypes.windll.user32


def pw(hwnd):
    import win32ui
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    hdc = user32.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    sd = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sd.SelectObject(bmp)
    rc = user32.PrintWindow(hwnd, sd.GetSafeHdc(), 2)
    if not rc:
        rc = user32.PrintWindow(hwnd, sd.GetSafeHdc(), 0)
    bits = bmp.GetBitmapBits(True)
    info = bmp.GetInfo()
    arr = np.ascontiguousarray(np.frombuffer(bits, np.uint8).reshape(
        info["bmHeight"], info["bmWidth"], 4)[:, :, 2::-1])
    win32gui.DeleteObject(bmp.GetHandle())
    sd.DeleteDC(); mfc.DeleteDC()
    user32.ReleaseDC(hwnd, hdc)
    return rect, arr


def sim(a, b):
    a = cv2.resize(a, (320, 240)); b = cv2.resize(b, (320, 240))
    a = a.astype(np.float32); b = b.astype(np.float32)
    mu = (a.mean() + b.mean()) / 2
    num = ((a - a.mean()) * (b - b.mean())).mean()
    den = a.std() * b.std() + 1e-6
    return float(num / den)


import cv2
import m0_trial
for name, sub in [("note", "desktop-note.txt"), ("login", "M0 演示登录"), ("icons", "icons - ")]:
    ws = m0_trial.find_window_by_title(sub)
    if not ws:
        print(name, "not found"); continue
    hwnd = ws[0]
    rect, pimg = pw(hwnd)
    simg = m0lib.grab_screen(rect)
    s1 = sim(pimg, simg)
    print("%-6s hwnd=%d rect=%s sim(grab,pw)=%.3f  grab_std=%.1f pw_std=%.1f" %
          (name, hwnd, rect, s1, float(simg.std()), float(pimg.std())))
