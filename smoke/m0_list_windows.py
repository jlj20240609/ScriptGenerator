# -*- coding: utf-8 -*-
"""列出可见窗口（含进程名），供补测目标挑选"""
import subprocess
import sys
import win32gui
import win32process

import m0lib

m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

ws = []


def cb(h, _):
    t = win32gui.GetWindowText(h).strip()
    if win32gui.IsWindowVisible(h) and t and len(t) < 70:
        ws.append((h, t, win32gui.GetClassName(h)))


win32gui.EnumWindows(cb, None)
for i, (h, t, c) in enumerate(ws):
    _, pid = win32process.GetWindowThreadProcessId(h)
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=15).stdout
        exe = (r.split('","')[0] or "?").strip('"') if r else "?"
    except Exception:
        exe = "?"
    print("%2d | %-20s | %-36s | rect=%s | cls=%s" % (i, exe[:20], t[:36],
                                                      win32gui.GetWindowRect(h), c[:24]))
