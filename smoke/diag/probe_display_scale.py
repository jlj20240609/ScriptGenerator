# -*- coding: utf-8 -*-
"""只读探测：能不能用 UIA 在"设置 → 显示"里切换显示缩放（WP8 真机验证用）。

只读：打开设置界面、找控件、打印找到什么，**不做任何修改**。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import subprocess  # noqa: E402

import uiautomation as auto  # noqa: E402

print("打开 设置 → 显示 …")
subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:display"], shell=False)
time.sleep(4.0)

win = auto.WindowControl(searchDepth=1, Name="设置")
if not win.Exists(3, 1):
    # Win11 的窗口名可能是"设置"或"Settings"
    win = auto.WindowControl(searchDepth=1, Name="Settings")
if not win.Exists(3, 1):
    print("没找到设置窗口 ✗")
    sys.exit(1)
print("设置窗口已打开 ✓")

# 找到缩放下拉：Win11 里是 ComboBox，名字含"缩放"
found = []
for ctrl, depth in auto.WalkControl(win, maxDepth=12):
    try:
        name = (ctrl.Name or "").strip()
        ctype = ctrl.ControlTypeName
    except Exception:
        continue
    if not name:
        continue
    if ("缩放" in name or "Scale" in name) and ctype in (
            "ComboBoxControl", "ListItemControl", "TextControl", "ButtonControl"):
        found.append((ctype, name, ctrl.BoundingRectangle))
        if len(found) >= 12:
            break

if not found:
    print("没找到含「缩放」的控件 ✗（可能需要先展开「显示」页或窗口太小）")
    # 兜底：把顶层控件名打出来，方便判断
    names = []
    for ctrl, _d in auto.WalkControl(win, maxDepth=6):
        try:
            if ctrl.Name:
                names.append(f"{ctrl.ControlTypeName}:{ctrl.Name[:24]}")
        except Exception:
            continue
        if len(names) >= 40:
            break
    print("前 40 个控件：")
    for n in names:
        print("   ", n)
    sys.exit(2)

print(f"找到 {len(found)} 个相关控件：")
for ctype, name, rect in found:
    print(f"   {ctype:18s} {name[:36]!r}  rect={rect}")
print("\n（只读探测结束，未做任何修改）")
