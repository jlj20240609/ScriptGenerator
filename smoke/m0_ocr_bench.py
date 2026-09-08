# -*- coding: utf-8 -*-
"""
M0 OCR 基准：测 RapidOCR 稳态延迟（首次含模型加载）与不同裁剪尺寸的耗时。
输出：控制台表格 + smoke/data/ocr_bench.jsonl
用法：python smoke/m0_ocr_bench.py [--repeats 3]
"""
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

import m0lib


def bench_sizes(screen_bgr, repeats=3):
    sizes = [("page-full 1280x800", screen_bgr[:800, :1280]),
             ("half-window 640x480", screen_bgr[100:580, 200:840]),
             ("widget 300x90", screen_bgr[300:390, 400:700]),
             ("chip 150x44", screen_bgr[320:364, 420:570])]
    rows = []
    print("\n=== OCR 延迟基准（%s）===" % m0lib.now_iso())
    print("%-22s %12s %10s %10s" % ("crop", "first_ms", "median_ms", "boxes"))
    for name, crop in sizes:
        # 首调用：冷启动（模型加载）
        t0 = time.perf_counter()
        r = m0lib.ocr_run(crop)
        first = (time.perf_counter() - t0) * 1000
        meds = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            r2 = m0lib.ocr_run(crop)
            meds.append((time.perf_counter() - t0) * 1000)
        meds.sort()
        median = meds[len(meds) // 2]
        print("%-22s %10.0f %10.0f %10d" % (name, first, median, len(r["txts"])))
        rows.append({"type": "ocr_bench", "crop": name, "first_ms": round(first),
                     "median_ms": round(median), "raw_ms": [round(x) for x in meds],
                     "boxes": len(r["txts"]), "engine": r["engine"]})
    return rows


def bench_find_text(screen_bgr, text, repeats=5, region=None):
    """真实屏幕上重复定位一段文字：命中率 + 单次延迟。"""
    print("\n=== 文本定位基准: '%s' × %d ===" % (text, repeats))
    print("%-8s %10s %10s %10s %s" % ("#", "elapsed", "hit", "sim", "center"))
    hits = 0
    recs = []
    for i in range(repeats):
        img = screen_bgr if region is None else screen_bgr[region[1]:region[1] + region[3],
                                                           region[0]:region[0] + region[2]]
        r = m0lib.find_text_ocr(img, text)
        ok = r["ok"]
        hits += int(ok)
        print("%-8d %10.0f %10s %10.2f %s" % (i + 1, r["elapsed_ms"], ok,
                                              r["score"], r["center"] if ok else "-"))
        recs.append({"type": "ocr_find", "text": text, "hit": ok,
                     "elapsed_ms": round(r["elapsed_ms"]), "score": round(r["score"], 3),
                     "center": r["center"], "ocr_count": r["ocr_count"],
                     "upsample": r.get("upsample", 0)})
    print("命中率: %d/%d" % (hits, repeats))
    return recs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--find-text", default="", help="附加：对当前屏幕重复定位该文字")
    ap.add_argument("--find-n", type=int, default=5)
    ap.add_argument("--no-sizes", action="store_true")
    args = ap.parse_args()

    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    all_rows = []
    if not args.no_sizes:
        screen = m0lib.grab_screen()
        all_rows += bench_sizes(screen, repeats=args.repeats)
    if args.find_text:
        screen = m0lib.grab_screen()
        all_rows += bench_find_text(screen, args.find_text, repeats=args.find_n)
    for row in all_rows:
        m0lib.log_result(row, Path(__file__).parent / "data" / "ocr_bench.jsonl")
    print("\n记录写入 smoke/data/ocr_bench.jsonl")
