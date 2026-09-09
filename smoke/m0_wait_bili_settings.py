# -*- coding: utf-8 -*-
"""轮询 bilibili 窗口内容，检测是否已切到静态设置页（最多等 90s）"""
import sys
import time
import win32gui

import m0lib
import m0_trial

m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

KEYWORDS = ["设置", "账号", "播放", "隐私", "通用", "版本"]
t0 = time.time()
last_raised = 0
while time.time() - t0 < 120:
    ws = m0_trial.find_window_by_title("哔哩哔哩")
    if not ws:
        print("bilibili 窗口未找到")
        sys.exit(1)
    h = ws[0]
    if win32gui.IsIconic(h):
        m0lib.bring_to_foreground(h)
        time.sleep(1.5)
    rect = win32gui.GetWindowRect(h)
    if rect[2] - rect[0] < 50 or rect[3] - rect[1] < 50:
        print("窗口 rect 异常:", rect, "等待…")
        time.sleep(4)
        continue
    bgr = m0lib.grab_screen(rect)
    r = m0lib.ocr_run(bgr)
    joined = " ".join(r["txts"])
    hit = [k for k in KEYWORDS if k in joined]
    print("[%s] rect=%s 命中词=%s OCR样例=%s" %
          (time.strftime("%H:%M:%S"), rect, hit, r["txts"][:6]))
    if len(hit) >= 2 or any(k in joined for k in ("账号设置", "播放设置", "隐私设置")):
        print("PAGE_OK 设置类静态页已就绪")
        sys.exit(0)
    time.sleep(5)
print("TIMEOUT 未检测到设置页（当前页关键词不足）")
