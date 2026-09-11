# -*- coding: utf-8 -*-
"""探针 v2：把 e2e 里的上下文逐段加回来，定位「第二次点击没进应用」的真凶。

探针 v1 已证明：光靠合成点击 + Tk，按钮每次都能命中且回调触发。
所以嫌疑在 e2e 特有的东西上：全局钩子、合成打字、无 update 的 1.5s 停顿。
本脚本按 --steps 逐段打开，观察按钮回调在第几步开始不触发。
"""
import argparse
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


def center_of(w):
    return (w.winfo_rootx() + w.winfo_width() // 2,
            w.winfo_rooty() + w.winfo_height() // 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recorder", action="store_true", help="挂上录制器（全局钩子）")
    ap.add_argument("--type", action="store_true", help="先合成打一段字")
    ap.add_argument("--sleep", type=float, default=0.0, help="点击前先空睡这么多秒（不 update）")
    args = ap.parse_args()

    capture.init_dpi_aware()
    root = tk.Tk()
    root.title("点击探针2")
    root.geometry("460x260+120+120")
    root.attributes("-topmost", True)
    entry = tk.Entry(root)
    entry.place(x=110, y=38, width=200, height=26)
    btn = tk.Button(root, text="确定", command=lambda: CLICKED.append("ok"))
    btn.place(x=110, y=110, width=120, height=44)
    root.bind_all("<Button-1>", lambda e: HITS.append((e.x_root, e.y_root)))
    root.update()
    root.update_idletasks()
    root.lift()
    root.focus_force()
    pump(root, 0.5)
    print(f"配置：录制器={args.recorder} 打字={args.type} 空睡={args.sleep}s", flush=True)

    from pynput import keyboard, mouse
    mc, kc = mouse.Controller(), keyboard.Controller()
    rec = None
    if args.recorder:
        from engine import recorder_live as L
        rec = L.build_recorder()
        print("  录制器启动：", rec.start(), flush=True)

    entry_xy, btn_xy = center_of(entry), center_of(btn)
    mc.position = entry_xy
    pump(root, 0.15)
    mc.click(mouse.Button.left, 1)
    pump(root, 0.25)
    print(f"  输入框点击后：Tk 收到 {len(HITS)} 次，焦点={root.focus_get()}", flush=True)

    if args.type:
        kc.type("demo")
        pump(root, 0.4)
        print(f"  打字后输入框={entry.get()!r}", flush=True)

    if args.sleep:
        time.sleep(args.sleep)

    mc.position = btn_xy
    pump(root, 0.15)
    before = len(HITS)
    mc.click(mouse.Button.left, 1)
    pump(root, 0.5)
    print(f"  按钮点击：Tk 新增 {len(HITS) - before} 次事件，命中={root.winfo_containing(*btn_xy)}，"
          f"按钮回调={len(CLICKED)}", flush=True)

    if rec is not None:
        res = rec.stop()
        print(f"  录制器：录到 {res['summary']['total']} 块，存帧 {res['frames']} 张", flush=True)
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
