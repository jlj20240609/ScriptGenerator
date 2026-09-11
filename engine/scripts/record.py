# -*- coding: utf-8 -*-
"""
engine/scripts/record.py — M3-WP1：真机操作录制（命令行）。

用法：
  python engine/scripts/record.py --out smoke/rec/脚本1.json
  python engine/scripts/record.py --out x.json --no-ocr     # 不做 OCR 反查（快，只靠图像匹配）
  python engine/scripts/record.py --out x.json --blocks-only # 只看切出来的积木，不出步骤

流程：开始录制 → 你去做一遍操作 → 回到这个窗口按回车停止 → 它把操作切成积木、
每个动作取「点击那一刻」的页面图并反查点到的部件 → 写出可直接回放的脚本。

为什么要有这个命令行入口：录制的可用性最终只能由真机回答（钩子挂没挂上、坐标对不对、
点到的部件认不认得出来）。UI（M3-WP2）之前先用它把 WP1 验完，免得把问题带进 UI。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture               # noqa: E402
from engine import recorder as R        # noqa: E402
from engine import recorder_live as L   # noqa: E402
from engine import schema               # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="操作录制（M3-WP1）")
    ap.add_argument("--out", default="", help="脚本输出路径（.json）")
    ap.add_argument("--name", default="", help="脚本名（默认取输出文件名）")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="定时自动停止（秒）；默认等你按回车")
    ap.add_argument("--no-ocr", action="store_true", help="不做 OCR 反查（更快，只靠图像）")
    ap.add_argument("--blocks-only", action="store_true", help="只输出积木，不出步骤")
    ap.add_argument("--no-page-on-first-only", action="store_true",
                    help="每个目标都内嵌整页图（默认只在第一块和换页时内嵌）")
    ap.add_argument("--stop-hotkey", default="ctrl+alt+q",
                    help="全局停止热键（默认 ctrl+alt+q；这个组合本身不会被录进去）")
    args = ap.parse_args(argv)

    pages = L.LivePages(ocr=not args.no_ocr,
                        page_on_first_only=not args.no_page_on_first_only)
    hook = L.PynputHooker(stop_combo=args.stop_hotkey)
    rec = R.Recorder(hooker=hook, grabr=capture.grab_screen,
                     window_of=pages.window_of, page_of=pages.page_of,
                     widget_of=pages.widget_of)

    res = rec.start()
    if not res.get("ok"):
        print(f"× 没法开始录制：{res.get('note')}")
        return 2
    print("● 开始录制。现在去做一遍你要自动化的操作。")
    print("  点几个地方、打几段字都行；操作之间的停顿会自动变成「等一下」。")
    stop = threading.Event()

    def _wait_enter():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        stop.set()

    threading.Thread(target=_wait_enter, daemon=True).start()
    if args.seconds > 0:
        print(f"  {args.seconds:.0f} 秒后自动停止（也可以随时按 {args.stop_hotkey}）。")
        deadline = time.time() + args.seconds
    else:
        print(f"  做完后按 {args.stop_hotkey} 停止（切回本窗口按回车也行）。")
        deadline = None
    while not stop.is_set():
        if hook.stopped.wait(0.1):
            break
        if deadline is not None and time.time() >= deadline:
            break
    print("  （已停止）")

    t0 = time.perf_counter()
    res = rec.stop()
    if not res.get("ok"):
        print(f"× 停止失败：{res.get('note')}")
        return 2
    blocks, summary = res["blocks"], res["summary"]
    print(f"\n● 录到 {summary['total']} 个动作，用时 {res['elapsed_s']}s"
          f"（其中 {summary['unsupported']} 个现有动作表示不了，只留痕）")
    for i, line in enumerate(summary["lines"], 1):
        print(f"  {i:>2}. {line}")
    if not blocks:
        print("× 一个动作都没录到——钩子可能没挂上（或被安全软件拦了）。")
        return 3
    if args.blocks_only:
        return 0

    print("\n● 正在反查每个动作点到的是什么（OCR + 图像）……")
    got = rec.steps(page_provider=pages.page_of)
    steps, notes = got["steps"], got["notes"]
    print(f"  出步骤 {len(steps)} 条，跳过 {len(got['skipped'])} 条。")
    for n in notes:
        print(f"  ! {n}")
    if not steps:
        print("× 没有可用的步骤。若全是「没拿到页面/部件」，用 --no-ocr 再试一次。")
        return 4

    sg = schema.new_script(args.name or Path(args.out or "录制的脚本").stem)
    sg["steps"] = steps
    try:
        schema.check(sg)
    except Exception as e:
        print(f"× 生成的脚本没过 schema 校验：{e}")
        return 5
    text = schema.dump(sg)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\n● 已写出：{out}（{len(text) / 1024:.1f} KB，{len(steps)} 步）")
    else:
        print("\n" + text)
    print(f"  反查共耗时 {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
