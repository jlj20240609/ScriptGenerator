# -*- coding: utf-8 -*-
"""列出"设置 → 显示 → 缩放"下拉里的全部选项（用于精确恢复到 125%）。"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uiautomation as auto  # noqa: E402

subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:display"], shell=False)
time.sleep(3.5)
win = None
for n in ("设置", "Settings"):
    w = auto.WindowControl(searchDepth=1, Name=n)
    if w.Exists(2, 1):
        win = w
        break
if win is None:
    print("设置窗口没打开 ✗")
    sys.exit(1)

combo = auto.ComboBoxControl(searchDepth=14, Name="缩放")
if not combo.Exists(3, 1):
    print("没有缩放下拉 ✗")
    sys.exit(1)
print(f"下拉 rect={combo.BoundingRectangle}")
try:
    print("当前值(ValuePattern):", repr(combo.GetValuePattern().Value))
except Exception as e:
    print("读当前值失败:", repr(e))

combo.Click()
time.sleep(1.2)

print("展开后，窗口内所有 ListItem / 含百分号的控件：")
n = 0
for ctrl, depth in auto.WalkControl(win, maxDepth=22):
    try:
        name = (ctrl.Name or "").strip()
        ctype = ctrl.ControlTypeName
    except Exception:
        continue
    if not name:
        continue
    if ctype in ("ListItemControl", "TextControl") and ("%" in name or name.isdigit()):
        print(f"   d={depth:2d} {ctype:18s} {name!r} rect={ctrl.BoundingRectangle}")
        n += 1
    elif ctype == "ListItemControl":
        print(f"   d={depth:2d} {ctype:18s} {name!r} (无百分号)")
        n += 1
    if n > 30:
        break
if n == 0:
    print("   （没找到列表项，可能下拉没展开；把顶层控件也打出来）")
    for ctrl, depth in auto.WalkControl(win, maxDepth=8):
        try:
            if ctrl.Name:
                print(f"   d={depth} {ctrl.ControlTypeName}:{ctrl.Name[:30]}")
        except Exception:
            continue
        if depth > 6:
            break
try:
    combo.SendKeys("{ESC}")
except Exception:
    pass
try:
    win.Close()
except Exception:
    pass
