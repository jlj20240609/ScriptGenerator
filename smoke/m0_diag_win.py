# -*- coding: utf-8 -*-
"""诊断 notepad/icons 窗口：PrintWindow 内容、虚拟桌面归属"""
import sys, os
import ctypes
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui, win32con

import comtypes
from comtypes import GUID

VDM_CLSID = GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
VDM_IID = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")


class IVirtualDesktopManager(comtypes.IUnknown):
    _iid_ = VDM_IID
    _methods_ = [
        comtypes.COMMETHOD([], ctypes.HRESULT, "IsWindowOnCurrentVirtualDesktop",
                           (["in"], ctypes.c_void_p, "topLevelWindow"),
                           (["out"], ctypes.POINTER(ctypes.c_int), "onCurrentDesktop")),
        comtypes.COMMETHOD([], ctypes.HRESULT, "GetWindowDesktopId",
                           (["in"], ctypes.c_void_p, "topLevelWindow"),
                           (["out"], ctypes.POINTER(GUID), "desktopId")),
        comtypes.COMMETHOD([], ctypes.HRESULT, "MoveWindowToDesktop",
                           (["in"], ctypes.c_void_p, "topLevelWindow"),
                           (["in"], ctypes.POINTER(GUID), "desktopId")),
    ]


def desktop_id(hwnd):
    try:
        mgr = comtypes.CoCreateInstance(VDM_CLSID, interface=IVirtualDesktopManager)
        g = GUID()
        mgr.GetWindowDesktopId(hwnd, ctypes.byref(g))
        return str(g)
    except Exception as e:
        return "ERR %r" % e


def printwindow_raw(hwnd):
    import win32ui
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        rc = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not rc:
            rc = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        bits = bmp.GetBitmapBits(True)
        info = bmp.GetInfo()
        import numpy as np
        arr = np.frombuffer(bits, np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        arr = np.ascontiguousarray(arr[:, :, 2::-1])
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return rc, arr
    except Exception:
        import traceback
        traceback.print_exc()
        return None, None


cur = win32gui.GetForegroundWindow()
print("fg window:", hex(cur), repr(win32gui.GetWindowText(cur)),
      "desktop:", desktop_id(cur))
for name, sub in [("note", "desktop-note.txt"), ("icons", "icons - ")]:
    import m0_trial
    wins = m0_trial.find_window_by_title(sub)
    for hwnd in wins:
        rc, img = printwindow_raw(hwnd)
        if img is None:
            print(name, hex(hwnd), "PW failed", rc)
            continue
        txt = m0lib.ocr_run(img)
        print("%s hwnd=%d PW_rc=%s std=%.1f shape=%s desktop=%s" %
              (name, hwnd, rc, float(img.std()), img.shape, desktop_id(hwnd)))
        print("   PW OCR:", txt["txts"][:10])
