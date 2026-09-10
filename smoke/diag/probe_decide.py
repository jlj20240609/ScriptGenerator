# -*- coding: utf-8 -*-
"""决定性诊断：用真实鼠标在覆盖层上拖拽（不经任何自动钩子），记录覆盖层上报的坐标。

配合：
  1) app 带 --remote-debugging-port=9222 启动；
  2) 本脚本先用 CDP 点“截图目标”，再用真实鼠标拖两次；
  3) 读 %TEMP%\\m1_ui_debug.log 里的 [覆盖层]/[选区] 行对照。
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture  # noqa: E402

capture.init_dpi_aware()
u32 = ctypes.windll.user32
NODE = "node"


def cursor():
    pt = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
    import ctypes.wintypes as wt
    pt = wt.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move(x, y, steps=10):
    x0, y0 = cursor()
    for i in range(1, steps + 1):
        u32.SetCursorPos(int(x0 + (x - x0) * i / steps), int(y0 + (y - y0) * i / steps))
        time.sleep(0.012)


def drag(x1, y1, x2, y2):
    move(x1, y1)
    time.sleep(0.25)
    p = cursor()
    print(f"  按下点 目标({x1},{y1}) 实际光标{p}")
    u32.mouse_event(0x0002, 0, 0, 0, 0)          # LEFTDOWN
    time.sleep(0.2)
    move(x2, y2, steps=12)
    time.sleep(0.2)
    p2 = cursor()
    u32.mouse_event(0x0004, 0, 0, 0, 0)          # LEFTUP
    print(f"  松开点 目标({x2},{y2}) 实际光标{p2} 行程 {p2[0]-p[0]}×{p2[1]-p[1]}")


def cdp(expr):
    r = subprocess.run([NODE, str(Path(__file__).with_name("cdp_eval.mjs")), "9222", expr],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    print("  CDP:", (r.stdout or r.stderr).strip()[:200])


page = [h for h in capture.find_windows_by_title("M0 演示登录") if not capture.is_iconic(h)]
if not page:
    print("登录页不在场")
    sys.exit(1)
capture.bring_to_foreground(page[0])
time.sleep(1.0)
capture.demote_window(page[0])
time.sleep(0.3)

page_rect = capture.window_rect(page[0])
print("登录页 rect(l,t,r,b):", page_rect)

def stroke_near(px, py, rad=10):
    """在物理坐标 (px,py) 附近数一下“框的描边色”像素（#38bdf8）。"""
    import cv2
    import numpy as np
    img = capture.grab_screen(None)
    if img is None:
        return -1, None
    y0, y1 = max(0, py - rad), min(img.shape[0], py + rad + 1)
    x0, x1 = max(0, px - rad), min(img.shape[1], px + rad + 1)
    sub = img[y0:y1, x0:x1]
    b = sub[:, :, 0].astype(np.int16)
    g = sub[:, :, 1].astype(np.int16)
    r = sub[:, :, 2].astype(np.int16)
    mask = (np.abs(b - 248) < 40) & (np.abs(g - 189) < 40) & (np.abs(r - 56) < 40)
    return int(mask.sum()), sub


print("① 用 CDP 点“截图目标”")
cdp("document.getElementById('btnPickTarget').click(); 'ok'")
time.sleep(2.0)
print("   覆盖层窗口:", [(h, capture.window_rect(h))
                        for h in capture.find_windows_by_title("框选")])

print("② 第一步：框页面（物理坐标）；拖到中途按住不放时量一下画出来的框")
pl, pt, pr, pb = page_rect
x1, y1 = pl + 60, pt + 60
x2, y2 = pr - 60, pb - 60
xm, ym = (x1 + x2) // 2, (y1 + y2) // 2
move(x1, y1)
time.sleep(0.25)
u32.mouse_event(0x0002, 0, 0, 0, 0)
time.sleep(0.2)
move(xm, ym, steps=12)
time.sleep(0.6)
cur = cursor()
n_cur, _ = stroke_near(cur[0], cur[1])
n_start, _ = stroke_near(x1, y1)
n_far, _ = stroke_near(x1 + 40, y1 - 40)
print(f"   光标物理 {cur}（行程 {cur[0]-x1}×{cur[1]-y1}）")
print(f"   描边像素数：光标处 {n_cur} / 按下点 {n_start} / 框外 40px 处 {n_far}")
print("   → 判定：光标处与按下点都应是框角（>0），框外应为 0")
move(x2, y2, steps=14)
time.sleep(0.25)
u32.mouse_event(0x0004, 0, 0, 0, 0)
print("   页面框完成")
time.sleep(3.0)

print("③ 第二步：框工号输入框（先问引擎“请输入工号”在哪）")
from engine import locator  # noqa: E402
img = capture.grab_screen(None)
res = locator.locate_widget_on_screen(img, (pl, pt, pr - pl, pb - pt),
                                      {"text": "请输入工号", "match": "text_first"})
if not res["ok"]:
    print("   没定位到输入框:", res.get("method"), res.get("elapsed_ms"))
else:
    bx, by, bw, bh = res["box"]
    print("   输入框:", res["box"], res["method"], f"{res['elapsed_ms']:.0f}ms")
    drag(bx - 40, by - 6, bx + bw + 60, by + bh + 6)
time.sleep(4.0)
print("完成")
