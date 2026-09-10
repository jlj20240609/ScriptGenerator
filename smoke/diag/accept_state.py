# -*- coding: utf-8 -*-
"""统一状态取证：先让进程 DPI 感知，再列窗口 / 截图 / OCR（避免虚拟化坐标混乱）。"""
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import win32gui  # noqa: E402

from engine import capture, matcher  # noqa: E402

capture.init_dpi_aware()
OUT = Path(__file__).with_name("accept")
OUT.mkdir(exist_ok=True)

print("虚拟屏:", capture.virtual_screen_rect())
print("可见窗口：")
def cb(h, _):
    if win32gui.IsWindowVisible(h):
        t = (win32gui.GetWindowText(h) or "").strip()
        if t:
            print(f"   {h} iconic={int(win32gui.IsIconic(h))} {capture.window_rect(h)} {t[:44]!r}")
    return True
win32gui.EnumWindows(cb, None)
print("前台:", capture.fg_window_info())

for title, tag in (("脚本构建器", "ui"), ("框选", "overlay"), ("M0 演示登录", "page")):
    hits = [h for h in capture.find_windows_by_title(title) if not capture.is_iconic(h)]
    if not hits:
        print(f"[{tag}] 不在场")
        continue
    l, t, r, b = capture.window_rect(hits[0])
    img = capture.grab_screen((l, t, r - l, b - t))
    if img is None:
        print(f"[{tag}] 抓取失败")
        continue
    p = OUT / f"{tag}.png"
    cv2.imwrite(str(p), img)
    res = matcher.ocr_run(img)
    print(f"[{tag}] {r-l}×{b-t} 截图{img.shape[1]}×{img.shape[0]} → {p}")
    print(f"    OCR({len(res['txts'])}):", res["txts"][:24])
