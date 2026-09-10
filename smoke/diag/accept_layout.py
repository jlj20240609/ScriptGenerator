# -*- coding: utf-8 -*-
"""陪跑准备：把登录演示页与脚本构建器摆成互不遮挡（被动，不动鼠标/不抢焦点）。

屏幕物理 1536×960：构建器最小 1125×750，登录页缩到 460×700（强制 1:1 缩放），
两者并排：登录页在左（卡片内容全可见），构建器在右下。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from engine import capture  # noqa: E402

FIX = ROOT / "smoke" / "fixtures"
TMP = Path(os.environ.get("TEMP", "."))
EDGE = next((str(p) for p in [
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    / "Microsoft/Edge/Application/msedge.exe"] if p.exists()), None)
if not EDGE:
    print("未找到 msedge.exe")
    sys.exit(2)

# 1) 关掉旧登录窗口
for h in capture.find_windows_by_title("M0 演示登录"):
    win32gui.PostMessage(h, 0x0010, 0, 0)
    time.sleep(0.6)

# 2) 1:1 缩放 + 460×700 重开（fixture 每次都刷新副本）
dst = TMP / "web_login.html"
dst.write_text((FIX / "web-login.html").read_text(encoding="utf-8"), encoding="utf-8")
subprocess.Popen([EDGE, f"--user-data-dir={TMP / 'm1_edge_profile'}", "--no-first-run",
                  "--no-default-browser-check", "--force-device-scale-factor=1",
                  "--window-size=460,700", f"--app={dst.as_uri()}"])
t0 = time.time()
page = None
while time.time() - t0 < 40:
    hits = capture.find_windows_by_title("M0 演示登录")
    if hits:
        page = hits[0]
        break
    time.sleep(1.0)
if not page:
    print("登录页拉起失败")
    sys.exit(2)
time.sleep(2.5)
win32gui.SetWindowPos(page, win32con.HWND_TOP, 5, 5, 0, 0,
                      win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
time.sleep(0.6)
print("登录页:", capture.window_rect(page))

# 3) 构建器挪到右下（最小尺寸 900×600 DIP = 1125×750 物理）
apps = capture.find_windows_by_title("脚本构建器")
if not apps:
    print("没找到“脚本构建器”窗口")
    sys.exit(2)
app = apps[0]
win32gui.SetWindowPos(app, win32con.HWND_TOP, 431, 205, 1125, 750,
                      win32con.SWP_SHOWWINDOW)
time.sleep(1.0)
print("构建器:", capture.window_rect(app))

# 4) 复核：登录卡片关键位置上方是不是登录页
px, py, pw, ph = capture.window_rect(page)
checks = {"工号输入框": (0.55, 0.35), "密码输入框": (0.55, 0.48),
          "登录按钮": (0.47, 0.62), "提示行位置": (0.47, 0.74)}
ok = True
for name, (dx, dy) in checks.items():
    cx, cy = int(px + pw * dx), int(py + ph * dy)
    top = capture.window_from_point(cx, cy)
    title = capture.window_title(int(top)) if top else "(无)"
    good = title.startswith("M0 演示登录")
    ok = ok and good
    print(f"  {name} ({cx},{cy}) 上方: {title}  {'OK' if good else '!! 被挡'}")
print("布局检查:", "通过" if ok else "有问题")
