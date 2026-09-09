# -*- coding: utf-8 -*-
"""
engine.cli — M1 引擎命令行（引擎可脱离 UI 独立运行，见《项目开发过程文档》§2-3）。

用法：
  python -m engine validate <script.sgscript.json>    校验脚本
  python -m engine run <script.sgscript.json> [--loc-log PATH] [--no-guard] [--title 子串]
      用真实桌面运行（前置窗口可选）；Ctrl+C 停止（无限循环兜底）。
  python -m engine selftest                          本地图像/OCR 自检
"""
from __future__ import annotations

import argparse
import sys

from engine import schema
from engine.errors import EngineError, ERRORS


def setup_utf8_stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _cmd_validate(args) -> int:
    sg = schema.load(args.script)
    print(f"OK：{args.script} 通过 .sgscript.json v1.0 校验"
          f"（{len(sg['steps'])} 个步骤）")
    return 0


def _cmd_run(args) -> int:
    from engine import capture
    from engine.executor import ConsoleHuman, LiveDriver, RunConfig, run_script
    from engine.logger import LocLogger

    capture.init_dpi_aware()
    sg = schema.load(args.script)
    win_ctx = {}
    if args.title:
        win_ctx["title"] = args.title
    driver = LiveDriver(win_ctx=win_ctx or None)
    cfg = RunConfig(guard=not args.no_guard)
    loc_log = LocLogger(args.loc_log)
    human = ConsoleHuman()

    def on_row(row):
        st = row.get("status")
        if st in ("ok", "fail", "skipped", "stopped", "iter") and row.get("label"):
            mark = {"ok": "✓", "fail": "✗", "skipped": "⤼", "stopped": "■", "iter": "…"}.get(st, "")
            print(f"  {mark} {row['label']}")
        elif st == "ok_pending":
            print(f"  … {row.get('label', '')}")

    print(f"运行脚本：{sg.get('name', args.script)}")
    try:
        report = run_script(sg, driver, cfg=cfg, loc_logger=loc_log, human=human,
                            sink=on_row)
    except EngineError as e:
        print(f"运行被停止：{e}")
        return 2
    status = report.get("status")
    print(f"\n结果：{'完成' if status == 'ok' else status} | "
          f"步骤 {len(report['steps'])} 行 | 点击 {report['counters']['clicks']} 次")
    return 0 if status == "ok" else 1


def _cmd_selftest(args) -> int:
    ok = True
    from engine import matcher
    import numpy as np

    def mk_text_img(w, h, text, size=24):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (w, h), (245, 247, 250))
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
        except Exception:
            font = ImageFont.load_default()
        d.text((10, max(4, (h - size) // 2)), text, font=font, fill=(20, 20, 20))
        return np.asarray(img)[:, :, ::-1].copy()

    # 1) 文本条带
    strip = mk_text_img(300, 90, "库存查询")
    f = matcher.find_text_ocr(strip, "库存查询")
    print("OCR 条带 300×90:", "OK" if f["ok"] else "FAIL", f.get("matched_text"),
          "%.0fms" % f["elapsed_ms"])
    ok = ok and f["ok"]
    # 2) 模板定位（偏移 + 缩放）
    page = mk_text_img(420, 240, "M1 selftest page", size=26)
    screen = np.full((900, 1400, 3), 18, np.uint8)
    sc = 1.15
    import cv2
    pw, ph = int(420 * sc), int(240 * sc)
    small = cv2.resize(page, (pw, ph))
    screen[120:120 + ph, 300:300 + pw] = small
    r = matcher.find_template(screen, page, scales=(0.8, 0.9, 1.0, 1.15, 1.25))
    exp = (300, 120)
    hit = r["ok"] and abs(r["rect"][0] - exp[0]) <= 3 and abs(r["rect"][1] - exp[1]) <= 3
    print("模板定位(1.15× 偏移):", "OK" if hit else "FAIL",
          r.get("rect"), "score=%.3f" % r.get("score", -1), "%.0fms" % r["elapsed_ms"])
    ok = ok and hit
    print("\n引擎自检:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine",
                                 description="ScriptGenerator M1 引擎（Python）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="校验 .sgscript.json")
    p.add_argument("script")
    p = sub.add_parser("run", help="解释运行脚本（真实桌面）")
    p.add_argument("script")
    p.add_argument("--loc-log", default="engine_loc.jsonl", help="定位日志 JSONL 路径")
    p.add_argument("--no-guard", action="store_true", help="关闭点击安全闸（调试用）")
    p.add_argument("--title", default="", help="按窗口标题子串前置目标窗口")
    sub.add_parser("selftest", help="本地图像/OCR 自检")
    args = ap.parse_args(argv)
    setup_utf8_stdio()
    try:
        if args.cmd == "validate":
            return _cmd_validate(args)
        if args.cmd == "run":
            return _cmd_run(args)
        if args.cmd == "selftest":
            return _cmd_selftest(args)
    except EngineError as e:
        print(f"错误 [{e.code}]：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止")
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
