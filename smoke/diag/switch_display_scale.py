# -*- coding: utf-8 -*-
"""切换 Windows 显示缩放（M2-WP8 真机验证用），并**校验是否真的生效**。

用法：
  python smoke/diag/switch_display_scale.py 150     # 切到 150%
  python smoke/diag/switch_display_scale.py 125     # 切回 125%

安全：只动"设置 → 显示 → 缩放"这一个下拉；执行后立刻用 DPI 校验，
不生效就如实报出来（有些机器会要求注销才生效，那种情况本脚本没法用于验证）。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uiautomation as auto  # noqa: E402

from engine import capture  # noqa: E402

want = (sys.argv[1] if len(sys.argv) > 1 else "").strip().rstrip("%")
if want not in ("100", "125", "150", "175", "200"):
    print("用法：python smoke/diag/switch_display_scale.py 150（或 125）")
    sys.exit(2)
label = f"{want}%"

capture.init_dpi_aware()
before = int(capture.dpi_of() or 0)
print(f"切换前 DPI={before}（约 {round(before / 96 * 100)}%）")


def _settings_window():
    subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:display"], shell=False)
    time.sleep(3.5)
    for name in ("设置", "Settings"):
        w = auto.WindowControl(searchDepth=1, Name=name)
        if w.Exists(2, 1):
            return w
    return None


win = _settings_window()
if win is None:
    print("打不开设置窗口 ✗")
    sys.exit(1)

combo = auto.ComboBoxControl(searchDepth=14, Name="缩放")
if not combo.Exists(3, 1):
    print("找不到缩放下拉 ✗")
    sys.exit(1)
print(f"缩放下拉 rect={combo.BoundingRectangle}")

combo.Click()
time.sleep(1.0)
# 选项名带后缀（如「125% (推荐)」），所以用**包含**匹配而不是精确匹配
# ——用精确匹配会找不到，实测就这么把用户的缩放留在了 150% 上，白折腾一轮。
item = None
for ctrl, _d in auto.WalkControl(win, maxDepth=22):
    try:
        if ctrl.ControlTypeName == "ListItemControl" and label in (ctrl.Name or ""):
            item = ctrl
            break
    except Exception:
        continue
if item is None:
    print(f"下拉里没有含 {label} 的选项 ✗")
    try:
        combo.SendKeys("{ESC}")
    except Exception:
        pass
    sys.exit(1)
print(f"选中 {label}（实际选项名 {item.Name!r}）…")
item.Click()
time.sleep(4.0)

try:
    win.Close()
except Exception:
    pass

capture.init_dpi_aware()
after = int(capture.dpi_of() or 0)
print(f"切换后 DPI={after}（约 {round(after / 96 * 100)}%）")
expect = {"100": 96, "125": 120, "150": 144, "175": 168, "200": 192}[want]
if abs(after - expect) <= 2:
    print(f"✓ 已切到 {label}")
    sys.exit(0)
print(f"✗ 没有生效（期望 DPI≈{expect}，实际 {after}）——可能需要注销/重新登录才生效，"
      f"那种情况下没法用于本次验证")
sys.exit(3)
