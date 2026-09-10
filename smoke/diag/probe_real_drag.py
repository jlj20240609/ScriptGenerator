# -*- coding: utf-8 -*-
"""真实鼠标框选 + 拖拽中途截图：一次看清"覆盖层画的框"与"真实光标"是否一致。

判定方法：拖到中途按住不放 → 截整屏 → 在覆盖层里按颜色找出蓝色选择框的边缘
（#38bdf8 → BGR 约 (248,189,56)）→ 与光标物理坐标比较（整屏截图是 1.25 倍放大）。
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import win32api  # noqa: E402
import win32con  # noqa: E402

from engine import capture, matcher  # noqa: E402

import ctypes  # noqa: E402

capture.init_dpi_aware()          # 必须在取窗口坐标/截图之前，否则拿到的是虚拟化坐标
u32 = ctypes.windll.user32

OUT = Path(__file__).with_name("accept")
OUT.mkdir(exist_ok=True)
SW, SH = capture.virtual_screen_rect()[2:]


def _abs(x, y):
    u32.SetCursorPos(int(round(x)), int(round(y)))


def move(x, y, steps=10):
    x0, y0 = win32api.GetCursorPos()
    for i in range(1, steps + 1):
        _abs(x0 + (x - x0) * i / steps, y0 + (y - y0) * i / steps)
        time.sleep(0.012)


def click(x, y):
    move(x, y)
    time.sleep(0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def find_text_box(hwnd, text, verbose=False):
    """返回该文字中心的**物理屏幕坐标**。

    注意：引擎截图是 1.25 倍（DPI 放大）的位图，OCR 盒要先除回物理尺寸。
    """
    l, t, r, b = capture.window_rect(hwnd)
    img = capture.grab_screen((l, t, r - l, b - t))
    if img is None:
        return None
    sc = img.shape[1] / max(1, (r - l))         # 截图坐标 → 物理坐标
    res = matcher.ocr_run(img)
    if not res["txts"] and res.get("error"):    # OCR 冷启动超时 → 重试一次
        print(f"   OCR 首次失败（{res.get('error')}，{res.get('elapsed_ms', 0):.0f}ms）→ 重试")
        time.sleep(1.0)
        res = matcher.ocr_run(img)
    if verbose:
        cv2.imwrite(str(OUT / f"dump_{text}.png"), img)
        print(f"   窗口 {hwnd} rect=({l},{t},{r-l},{b-t}) 截图 {img.shape[1]}×{img.shape[0]} "
              f"均值={img.mean():.1f} 缩放={sc:.3f} OCR ok={res.get('ok')} "
              f"err={res.get('error')} {res.get('elapsed_ms', 0):.0f}ms")
        print("   窗口内文字:", res["txts"][:30])
    for txt, box in zip(res["txts"], res["boxes"]):
        if text in txt.replace(" ", ""):
            bx, by = box[0] / sc, box[1] / sc
            bw, bh = box[2] / sc, box[3] / sc
            return (int(l + bx + bw / 2), int(t + by + bh / 2),
                    (round(bx), round(by), round(bw), round(bh)))
    return None


def box_of_color():
    """整屏截图里找覆盖层选择框（蓝色）边界 → 物理坐标。"""
    img = capture.grab_screen(None)
    if img is None:
        return None
    b = img[:, :, 0].astype(np.int16)
    g = img[:, :, 1].astype(np.int16)
    r = img[:, :, 2].astype(np.int16)
    mask = (np.abs(b - 248) < 60) & (np.abs(g - 189) < 60) & (np.abs(r - 56) < 60)
    ys, xs = np.where(mask)
    if len(xs) < 200:
        return None
    cv2.imwrite(str(OUT / "drag_mid.png"), img)
    sc = 1536 / img.shape[1]                    # 截图→物理
    return (xs.min() * sc, ys.min() * sc, xs.max() * sc, ys.max() * sc)


apps = [h for h in capture.find_windows_by_title("脚本构建器") if not capture.is_iconic(h)]
page_hits = [h for h in capture.find_windows_by_title("M0 演示登录") if not capture.is_iconic(h)]
if not apps or not page_hits:
    print("构建器或登录页不在场")
    sys.exit(1)
app, page = apps[0], page_hits[0]

capture.bring_to_foreground(page)
time.sleep(1.0)
capture.demote_window(page)
time.sleep(0.3)

capture.bring_to_foreground(app)
time.sleep(0.8)
capture.demote_window(app)
time.sleep(0.4)
hit = find_text_box(app, "截图目标", verbose=True)
if not hit:
    print("没找到“截图目标”")
    sys.exit(1)
print("点击“截图目标”", hit[:2], hit[2])
click(hit[0], hit[1])
# 校验：选区覆盖层是否真的出现
t0 = time.time()
while time.time() - t0 < 6:
    if capture.find_windows_by_title("框选"):
        break
    time.sleep(0.4)
print("覆盖层出现:", bool(capture.find_windows_by_title("框选")))
time.sleep(1.5)

pl, pt, pr, pb = capture.window_rect(page)
x1, y1 = pl + 40, pt + 40
x2, y2 = pr - 40, pb - 40
xm, ym = (x1 + x2) // 2, (y1 + y2) // 2
print(f"页面框：({x1},{y1}) → ({x2},{y2})；中途点 ({xm},{ym})")

move(x1, y1)
time.sleep(0.3)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.2)
move(xm, ym, steps=12)
time.sleep(0.6)
got = box_of_color()
cur = win32api.GetCursorPos()
print(f"中途光标物理 {cur}；覆盖层画出的框 {None if got is None else tuple(round(v) for v in got)}")
if got:
    print(f"  框左/上 与 起点误差：{round(got[0]-x1,1)}, {round(got[1]-y1,1)}")
    print(f"  框右/下 与 中途光标误差：{round(got[2]-cur[0],1)}, {round(got[3]-cur[1],1)}")
    print(f"  框尺寸 {round(got[2]-got[0],1)}×{round(got[3]-got[1],1)}"
          f"（光标行程 {cur[0]-x1}×{cur[1]-y1}）")
time.sleep(0.3)
move(x2, y2, steps=14)
time.sleep(0.3)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
print("页面框已完成，等界面消化…")
time.sleep(3.0)
print("现在框部件（工号输入框）")
hits = find_text_box(page, "请输入工号")
if not hits:
    print("没找到“请输入工号”")
    sys.exit(1)
wx, wy, _wb = hits
widget = (wx - 60, wy - 12, wx + 160, wy + 14)
print(f"部件框：{widget}")
move(widget[0], widget[1])
time.sleep(0.25)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.15)
move(widget[2], widget[3], steps=12)
time.sleep(0.2)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(4.0)
print("完成")
