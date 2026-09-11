# -*- coding: utf-8 -*-
"""验证"用户在脚本构建器里的操作不计入步骤"（真机，不需要人来点）。

场景：点一下脚本构建器的标题栏（**应被丢弃**）→ 点一下桌面空白（**应被记录**）。
标题栏是刻意的选择：点它不会有任何副作用（不会触发界面按钮），却是一次真实的
鼠标点击，正好用来验证过滤逻辑。

用法：smoke/.venv-ortcpu/Scripts/python.exe smoke/diag/verify_self_filter.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture                # noqa: E402
from engine import recorder as R          # noqa: E402
from engine import recorder_live as L     # noqa: E402

APP_TITLE = "脚本构建器"


def click_at(xy):
    import win32api
    import win32con
    win32api.SetCursorPos((int(xy[0]), int(xy[1])))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main() -> int:
    capture.init_dpi_aware()
    fail = []

    def check(ok, label, detail=""):
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not ok:
            fail.append(label)
        return ok

    print("=" * 68)
    print("真机验证：点脚本构建器自己的窗口 → 不该进步骤")
    print("=" * 68)

    ws = capture.find_windows_by_title(APP_TITLE)
    if not check(bool(ws), f"找到应用窗口「{APP_TITLE}」（应用要先开着）"):
        return 2
    hwnd = ws[0]
    rect = capture.window_rect(hwnd)
    print(f"  应用窗口 hwnd={hwnd} rect={rect}")

    app_pt = (rect[0] + rect[2] // 2, rect[1] + 14)          # 标题栏正中（无副作用）
    vx, vy, vw, vh = capture.virtual_screen_rect()
    desk_pt = (vx + vw // 2, min(vy + vh - 80, rect[1] + rect[3] + 25))   # 窗口下方桌面
    other_hwnd = capture.window_from_point(*desk_pt)
    print(f"  应用内点击点 {app_pt} → 落在 hwnd={capture.window_from_point(*app_pt)}")
    print(f"  桌面上点击点 {desk_pt} → 落在 hwnd={other_hwnd}")
    if other_hwnd == hwnd:
        print("  × 桌面那个点其实还在应用窗口里，这个验证不成立，换个位置再试")
        return 2

    rec = L.build_recorder(skip_hwnds=[hwnd])
    if not check(rec.start().get("ok"), "录制器启动（带自家窗口过滤）"):
        return 2
    click_at(app_pt)
    time.sleep(0.4)
    click_at(desk_pt)
    time.sleep(0.4)
    res = rec.stop()

    print(f"\n  收到事件 {len(rec.events)} 条，过滤掉自家窗口 {res['skipped_own']} 条")
    for e in rec.events:
        print(f"    · {e['kind']} @({e.get('x')},{e.get('y')})")
    print("  积木：" + " → ".join(res["summary"]["lines"]))

    check(res["skipped_own"] == 1, "点在应用自己窗口上的那次被丢弃了",
          f"skipped_own={res['skipped_own']}")
    check(len(rec.events) == 1, "桌面上的那次正常记录下来了",
          f"{len(rec.events)} 条")
    if rec.events:
        e = rec.events[0]
        check(abs(e["x"] - desk_pt[0]) <= 2 and abs(e["y"] - desk_pt[1]) <= 2,
              "记录的正是桌面那一下（坐标对得上）",
              f"({e['x']},{e['y']}) vs {desk_pt}")

    # 反向对照：不带过滤时，应用内的点击应该被录下来
    rec2 = L.build_recorder()
    if rec2.start().get("ok"):
        click_at(app_pt)
        time.sleep(0.4)
        res2 = rec2.stop()
        check(res2["skipped_own"] == 0 and len(rec2.events) >= 1,
              "反向对照：不带过滤时应用内的点击**会**被录下来（说明上面确实是过滤起的作用）",
              f"events={len(rec2.events)} skipped={res2['skipped_own']}")

    print("=" * 68)
    if fail:
        print(f"结果：{len(fail)} 项未通过")
        for f in fail:
            print("  × " + f)
        return 1
    print("结果：全部通过（自家窗口过滤在真机上有效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
