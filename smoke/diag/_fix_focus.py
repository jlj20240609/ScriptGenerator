# -*- coding: utf-8 -*-
"""修 generate_e2e.py 的窗口句柄比较（Tk 有两层窗口，直接用 == 会误判）。

Tk 的顶层窗口外面还套一层 wrapper，WindowFromPoint 拿到的与 GetForegroundWindow
拿到的可能不是同一个句柄；引擎里本来就有 capture.root_window() 做归一化，用它比就好。
不改的话会出现"把自己的窗口当成别人的窗口让开"这种荒唐事（实测遇到了）。
"""
from pathlib import Path

p = Path("smoke/diag/generate_e2e.py")
src = p.read_text(encoding="utf-8")

helper = '''

def same_win(a, b) -> bool:
    """两个句柄是不是同一个顶层窗口（Tk 有 wrapper 层，不能直接比）。"""
    if not a or not b:
        return False
    try:
        return capture.root_window(int(a)) == capture.root_window(int(b))
    except Exception:
        return int(a) == int(b)

'''
src = src.replace("\n\ndef pump(root, seconds=0.25):", helper + "\ndef pump(root, seconds=0.25):")

src = src.replace("        fg0 = win32gui.GetForegroundWindow()\n        if fg0 and fg0 != hwnd:",
                  "        fg0 = win32gui.GetForegroundWindow()\n"
                  "        if fg0 and not same_win(fg0, hwnd):")
src = src.replace("            if capture.fg_window_info().get('hwnd') == hwnd:",
                  "            if same_win(capture.fg_window_info().get('hwnd'), hwnd):")
src = src.replace('            if capture.fg_window_info().get("hwnd") == hwnd:',
                  '            if same_win(capture.fg_window_info().get("hwnd"), hwnd):')
src = src.replace('                  f"前台={capture.fg_window_info().get(\'hwnd\')} 窗口={hwnd}；"',
                  '                  f"前台={capture.fg_window_info().get(\'hwnd\')} 窗口={hwnd}；"')
src = src.replace("        if not got_fg:\n            for h in parked:",
                  "        if not got_fg:\n            for h in parked:")
p.write_text(src, encoding="utf-8")
print("已修 same_win 归一化")
