# -*- coding: utf-8 -*-
"""陪跑准备：干净重开登录演示页并等窗口尺寸稳定，随后复核内容与归属。"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from engine import capture, matcher  # noqa: E402

TMP = Path(os.environ.get("TEMP", "."))
EDGE = next((str(p) for p in [
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    / "Microsoft/Edge/Application/msedge.exe"] if p.exists()), None)

# ① 关掉所有旧的登录窗口（含残留）
for _ in range(3):
    ws = capture.find_windows_by_title("M0 演示登录")
    if not ws:
        break
    for h in ws:
        win32gui.PostMessage(h, 0x0010, 0, 0)
    time.sleep(1.0)
print("旧窗口已清:", capture.find_windows_by_title("M0 演示登录"))

# ② 标准参数重开（不做强制缩放，避免尺寸/缩放漂移）
dst = TMP / "web_login.html"
dst.write_text((ROOT / "smoke" / "fixtures" / "web-login.html").read_text(encoding="utf-8"),
               encoding="utf-8")
subprocess.Popen([EDGE, f"--user-data-dir={TMP / 'm1_edge_profile'}", "--no-first-run",
                  "--no-default-browser-check", "--window-size=1020,780",
                  f"--app={dst.as_uri()}"])
hwnd, t0 = 0, time.time()
while time.time() - t0 < 40:
    ws = capture.find_windows_by_title("M0 演示登录")
    if ws:
        hwnd = ws[0]
        break
    time.sleep(1.0)
if not hwnd:
    print("拉起失败")
    sys.exit(2)

# ③ 等尺寸连续稳定（实测：Edge app 窗口启动后可能自己变一次尺寸）
stable, prev = 0, None
t0 = time.time()
while time.time() - t0 < 30:
    r = capture.window_rect(hwnd)
    if r == prev:
        stable += 1
        if stable >= 3:
            break
    else:
        stable = 0
        prev = r
    time.sleep(1.0)
print("窗口尺寸稳定:", capture.window_rect(hwnd), f"（连续稳定 {stable} 次）")

# ④ 切到最前并复核
capture.bring_to_foreground(hwnd)
time.sleep(0.9)
capture.demote_window(hwnd)
l, t, r, b = capture.window_rect(hwnd)
own, cover = capture.window_for_rect([l, t, r - l, b - t])
print(f"区域归属: {own} 覆盖率 {cover} {capture.window_title(own) if own else ''}")
img = capture.grab_screen((l, t, r - l, b - t))
print("页面内容:", matcher.ocr_run(img)["txts"][:8] if img is not None else None)
for name, (fx, fy) in {"工号框": (0.5, 0.5), "登录按钮": (0.47, 0.60),
                       "提示行": (0.47, 0.72)}.items():
    cx, cy = int(l + (r - l) * fx), int(t + (b - t) * fy)
    top = int(capture.window_from_point(cx, cy) or 0)
    print(f"  {name} ({cx},{cy}) → {'登录页 OK' if top == hwnd else '被挡: ' + capture.window_title(top)}")
