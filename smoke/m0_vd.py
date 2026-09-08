# -*- coding: utf-8 -*-
"""虚拟桌面探测/修正：IVirtualDesktopManager 手写 vtable（无 comtypes 依赖）
vtable: 0QueryInterface 1AddRef 2Release
       3 IsWindowOnCurrentVirtualDesktop(hwnd, *BOOL)
       4 GetWindowDesktopId(hwnd, *GUID)
       5 MoveWindowToDesktop(hwnd, *GUID)
"""
import sys, os
import ctypes
from ctypes import wintypes
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import win32gui

def make_guid(s):
    b = bytes.fromhex(s.replace("-", ""))
    import struct
    return struct.pack("<IHH", *struct.unpack(">IHH", b[:8])) + b[8:]

VDM_CLSID = make_guid("AA5090865CA94C258F95589D3C07B48A")
VDM_IID = make_guid("A5CD92FF29BE454C8D04D82879FB3F1B")
GUID_T = wintypes.BYTE * 16
CLSCTX_INPROC_SERVER = 0x1


def get_mgr():
    ctypes.oledll.ole32.CoInitialize(None)
    ppv = ctypes.c_void_p()
    hr = ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(GUID_T(*VDM_CLSID)), None,
                                              CLSCTX_INPROC_SERVER,
                                              ctypes.byref(GUID_T(*VDM_IID)),
                                              ctypes.byref(ppv))
    if hr != 0:
        raise OSError("CoCreateInstance hr=0x%x" % hr)
    return ppv


def vtable_fn(ppv, slot, restype, argtypes):
    """从接口指针读 vtable 第 slot 项并包装成可调用函数（首参 self=接口指针）。"""
    vtable_ptr = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p)).contents.value
    entries = ctypes.cast(vtable_ptr, ctypes.POINTER(ctypes.c_void_p))
    fn_addr = entries[slot]
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_addr)
    return fn


def is_on_current(hwnd):
    mgr = get_mgr()
    out = ctypes.c_int()
    fn = vtable_fn(mgr, 3, ctypes.HRESULT, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)))
    fn(mgr, hwnd, ctypes.byref(out))
    return bool(out.value)


def get_desktop_id(hwnd):
    mgr = get_mgr()
    out = GUID_T()
    fn = vtable_fn(mgr, 4, ctypes.HRESULT, (ctypes.c_void_p, ctypes.POINTER(GUID_T)))
    fn(mgr, hwnd, ctypes.byref(out))
    return bytes(out).hex()


def move_to_desktop(hwnd, guid_bytes):
    mgr = get_mgr()
    g = GUID_T(*guid_bytes)
    fn = vtable_fn(mgr, 5, ctypes.HRESULT, (ctypes.c_void_p, ctypes.POINTER(GUID_T)))
    return fn(mgr, hwnd, ctypes.byref(g))


if __name__ == "__main__":
    import m0_trial
    refs = []
    for sub in ("启动M0", "M0 演示登录", "desktop-note.txt", "icons - ", "计算器"):
        ws = m0_trial.find_window_by_title(sub)
        if ws:
            refs.append((sub, ws[0]))
    for name, hwnd in refs:
        cur = is_on_current(hwnd)
        print("%-16s hwnd=%-9d on_current=%s desktop=%s" %
              (name, hwnd, cur, get_desktop_id(hwnd)[:16]))
    # 用一个确定在当前桌面的窗口（Edge 演示登录页）作参照，把 notepad/explorer 移过去
    base = None
    for name, hwnd in refs:
        if name == "M0 演示登录":
            base = hwnd
    if base and is_on_current(base):
        guid = GUID_T(*bytes.fromhex(get_desktop_id(base)))
        for name, hwnd in refs:
            if not is_on_current(hwnd) and name in ("desktop-note.txt", "icons - "):
                hr = move_to_desktop(hwnd, guid)
                print("moved %s hr=0x%x now_on_current=%s" %
                      (name, hr & 0xFFFFFFFF, is_on_current(hwnd)))
