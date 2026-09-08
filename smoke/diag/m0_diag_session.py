# -*- coding: utf-8 -*-
"""会话诊断：本进程/explorer/前台窗口 各在哪个 Windows Session"""
import ctypes
from ctypes import wintypes

import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

def session_of_pid(pid):
    TOKEN_QUERY = 0x0008
    TokenSessionId = 12
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
    if not h:
        return None
    tok = wintypes.HANDLE()
    ctypes.windll.advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok))
    buf = (ctypes.c_ubyte * 8)()
    n = ctypes.c_ulong()
    ctypes.windll.advapi32.GetTokenInformation(tok, TokenSessionId, buf, 8, ctypes.byref(n))
    ctypes.windll.kernel32.CloseHandle(h)
    ctypes.windll.kernel32.CloseHandle(tok)
    return int(buf[0])

me = ctypes.windll.kernel32.GetCurrentProcessId()
print("本进程 session:", session_of_pid(me))
import win32gui, win32process, subprocess
import m0_trial
for name, sub in [("login", "M0 演示登录"), ("note", "desktop-note.txt")]:
    ws = m0_trial.find_window_by_title(sub)
    if ws:
        _, pid = win32process.GetWindowThreadProcessId(ws[0])
        print("%s 窗口进程 session: %s (pid %d)" % (name, session_of_pid(pid), pid))
out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq explorer.exe"],
                     capture_output=True, text=True).stdout.strip()
for line in out.splitlines():
    parts = line.split('","')
    if len(parts) > 3:
        print("explorer:", parts[0].strip('"'), "session:", parts[-2].strip('"'))
out2 = subprocess.run(["qwinsta"], capture_output=True, text=True).stdout
print(out2)
