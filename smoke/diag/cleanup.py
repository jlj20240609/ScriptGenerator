# -*- coding: utf-8 -*-
"""交还电脑前的清理：关掉演示用的 fixture 窗口、取消本项目窗口的置顶。

只动本项目自己的窗口（“M0 演示登录 …”“脚本构建器”“框选”），不碰用户其它窗口。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from engine import capture  # noqa: E402

capture.init_dpi_aware()
MINE = ("M0 演示登录", "M1 动态工作台", "M0 ERP 查询", "脚本构建器", "框选")

closed, demoted = [], []
for sub in MINE:
    for h in capture.find_windows_by_title(sub):
        try:
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            if ex & win32con.WS_EX_TOPMOST:
                capture.demote_window(h)
                demoted.append((h, sub))
        except Exception:
            pass
        if sub == "脚本构建器":      # 界面窗口交给 Electron 自己退出，这里不强关
            continue
        try:
            win32gui.PostMessage(h, 0x0010, 0, 0)      # WM_CLOSE
            closed.append((h, sub))
        except Exception:
            pass
        time.sleep(0.4)

print("已取消置顶:", demoted or "无")
print("已关闭演示窗口:", closed or "无")
time.sleep(0.6)
for sub in MINE:
    left = capture.find_windows_by_title(sub)
    if left:
        print("  仍在场:", sub, left)
print("清理完成")
