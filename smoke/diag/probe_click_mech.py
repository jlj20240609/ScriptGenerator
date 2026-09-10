# -*- coding: utf-8 -*-
"""诊断：为什么"点一下"没反应——光标落点对不对？pynput 点击与 SendInput 点击哪个有效？

跑法：python smoke/diag/probe_click_mech.py
"""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import win32api  # noqa: E402

from engine import capture, matcher  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

OUT = ROOT / "smoke" / "diag"
capture.init_dpi_aware()
case = B.CASES["login_full"]

hwnd, shot, page_rect, spec = B._case_window_and_page(case, must="登录")
B.reset_page(hwnd, case)
hwnd, shot, page_rect, spec = B._case_window_and_page(case, must="登录")
h, w = shot.shape[:2]
print(f"page_rect={page_rect} 页面图 {w}x{h}")


def ocr_words(img):
    return matcher.ocr_run(img)["txts"]


def tip_now(tag):
    s = capture.grab_screen(page_rect)
    words = ocr_words(s)
    hit = [x for x in words if "密码错误" in x or "请输入工号" in x and "密码" in x]
    print(f"  [{tag}] 提示={hit or '无'} / 全量前 12={words[:12]}")
    return s


# 1) 输入工号 + 错误密码（用引擎自己的动作，验证键盘输入没问题）
wgt = B._widget_factory(shot, page_rect, spec)
head = [
    {"id": "s1", "type": "action", "action": "type", "params": {"text": "demo"},
     "target": wgt("请输入工号")},
    {"id": "s2", "type": "action", "action": "type", "params": {"text": "wrong-pass"},
     "target": wgt("请输入密码")},
]
from engine.executor import LiveDriver, RunConfig, run_script  # noqa: E402

rep = run_script({"version": "1.0", "name": "fill", "targets_rev": 0, "steps": head},
                 LiveDriver({"hwnd": hwnd}), cfg=RunConfig(guard=True),
                 loc_logger=B.CountLogger(), human=B.BenchHuman())
print(f"输入两步 status={rep.get('status')}")
tip_now("输入后")
from pynput.mouse import Button, Controller  # noqa: E402

# 2) 光标落点核对：按钮中心（页面内坐标来自 OCR）
btn = matcher.find_text_all_ocr(shot, "登录", thr=0.8)
if not btn:
    print("找不到按钮文字，退出")
    sys.exit(1)
b = btn[0]["box"]
bx = page_rect[0] + b[0] + b[2] // 2
by = page_rect[1] + b[1] + b[3] // 2
print(f"按钮文字盒={list(b)} → 目标屏幕坐标=({bx},{by})")
win32api.SetCursorPos((bx, by))
time.sleep(0.4)
print(f"SetCursorPos 后 GetCursorPos={win32api.GetCursorPos()}（应与目标一致）")


def sendinput_click(x, y):
    """SendInput 绝对坐标点击（0..65535 归一化，最可靠的一种）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    sm_cx = user32.GetSystemMetrics(0)
    sm_cy = user32.GetSystemMetrics(1)
    ax = int(x * 65535 / max(1, sm_cx - 1))
    ay = int(y * 65535 / max(1, sm_cy - 1))

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    flags = [0x0001 | 0x8000, 0x0002 | 0x8000]      # MOVE|ABSOLUTE, LEFTDOWN|ABSOLUTE
    for i, fl in enumerate(flags + [0x0004 | 0x8000]):   # + LEFTUP
        inp = INPUT(type=0, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=fl,
                                          time=0, dwExtraInfo=None))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.05)


print("方式 A：**引擎自己的 click 动作**（窗口已是前台）")
rep2 = run_script({"version": "1.0", "name": "engine-click", "targets_rev": 0,
                   "steps": [{"id": "s1", "type": "action", "action": "click",
                              "params": {}, "target": wgt("登录", pad_x=24)}]},
                  LiveDriver({"hwnd": hwnd}), cfg=RunConfig(guard=True),
                  loc_logger=B.CountLogger(), human=B.BenchHuman())
print(f"  引擎 click status={rep2.get('status')} err={rep2.get('error')}")
for st in rep2.get("steps") or []:
    print(f"    · {st.get('status')} {st.get('label') or st.get('action')} "
          f"box={st.get('box')} detail={st.get('detail') or ''}")
print(f"  点击后 GetCursorPos={win32api.GetCursorPos()}（目标是 ({bx},{by})）")
time.sleep(1.4)
s_a = tip_now("引擎点击后")

if not [x for x in ocr_words(s_a) if "密码错误" in x]:
    print("方式 B：pynput 手动点击（同一坐标）")
    Controller().click(Button.left)
    time.sleep(1.5)
    tip_now("pynput 点击后")

print("方式 C：SendInput 绝对坐标点击")
sendinput_click(bx, by)
time.sleep(1.5)
s_b = tip_now("SendInput 点击后")
cv2.imwrite(str(OUT / "click_after_sendinput.png"), s_b)
print(f"图：{OUT / 'click_after_sendinput.png'}")
