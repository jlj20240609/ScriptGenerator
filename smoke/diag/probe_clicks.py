# -*- coding: utf-8 -*-
"""探针：合成的点击到底有没有进到 Tk 控件里（区分「点位不对」和「事件被吞」）。

真机 e2e 里出现了一个说不通的现象：全局钩子两次都收到了点击、坐标也对，
Tk 的命中测试和 Win32 的落点窗口都指向目标控件，但只有第一次点击进了应用。
这个探针按固定序列连点几个已知点位，逐次报告 Tk 实际收到了几次。
"""
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture  # noqa: E402

HITS = []
CLICKED = []


def pump(root, seconds=0.2):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.02)


def main() -> int:
    capture.init_dpi_aware()
    root = tk.Tk()
    root.title("点击探针")
    root.geometry("460x260+120+120")
    root.attributes("-topmost", True)
    entry = tk.Entry(root)
    entry.place(x=110, y=38, width=200, height=26)
    btn = tk.Button(root, text="确定", command=lambda: CLICKED.append("ok"))
    btn.place(x=110, y=110, width=120, height=44)
    root.bind_all("<Button-1>", lambda e: HITS.append((e.x_root, e.y_root)))
    root.bind_all("<ButtonRelease-1>", lambda e: HITS.append(("up", e.x_root, e.y_root)))
    root.update()
    root.update_idletasks()
    root.lift()
    root.focus_force()
    pump(root, 0.5)

    hwnd = capture.window_from_point(*center_of(entry))
    print(f"自测窗口 hwnd={hwnd}，前台={capture.fg_window_info().get('hwnd')}")

    from pynput import mouse
    mc = mouse.Controller()
    seq = [("输入框#1", center_of(entry)),
           ("按钮#1", center_of(btn)),
           ("按钮#2", center_of(btn)),
           ("输入框#2", center_of(entry)),
           ("按钮#3", center_of(btn))]
    for name, pt in seq:
        before = len(HITS)
        mc.position = pt
        pump(root, 0.15)
        hit = root.winfo_containing(*pt)
        mc.click(mouse.Button.left, 1)
        pump(root, 0.3)
        got = HITS[before:]
        print(f"  {name} {pt} 命中={hit} Tk收到={got} 按钮回调={len(CLICKED)}", flush=True)
    root.destroy()
    return 0


def center_of(w):
    return (w.winfo_rootx() + w.winfo_width() // 2,
            w.winfo_rooty() + w.winfo_height() // 2)


if __name__ == "__main__":
    sys.exit(main())
