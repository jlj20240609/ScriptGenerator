# -*- coding: utf-8 -*-
"""诊断：邻居校验为什么时好时坏（真值候选上也有 26% 判 False）。

跑法：python smoke/diag/probe_nearby.py
思路：复算 _nearby_ok 的搜索窗，把"窗口位置/大小/窗口里 OCR 读到了什么"全打出来。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, matcher, schema  # noqa: E402
from engine import locator as L  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

capture.init_dpi_aware()
case = B.CASES["interference"]
hwnd = B.ensure_case_window(case)
B.reset_page(hwnd, case)
B.ensure_foreground(hwnd)
time.sleep(1.2)

l, t, r, b = capture.window_rect(hwnd)
page = capture.grab_screen((l, t, r - l, b - t))
ph, pw = page.shape[:2]

sg = schema.load(B.ASSETS / "interference.sgscript.json")
tgt = sg["steps"][0]["target"]
nearby = tgt.get("nearby") or []
print(f"目标框 rect_in_page={tgt['rect_in_page']}")
print(f"邻居记录={[(x['text'], x['offset'], x['rect_in_page']) for x in nearby]}")
print(f"页面上「查询」的位置："
      f"{[(h['matched_text'], list(h['box'])) for h in matcher.find_text_all_ocr(page, '查询', thr=0.8)]}")
print(f"页面上「物料编码」的位置："
      f"{[(h['matched_text'], list(h['box'])) for h in matcher.find_text_all_ocr(page, '物料编码', thr=0.8)]}")

res = L.locate_widget(page, (0, 0, pw, ph), tgt)
det = (res.get("detail") or {}).get("l2_ocr") or {}
print(f"\n定位：ok={res['ok']} method={res.get('method')} level={res.get('level')} "
      f"box={res.get('box')}")
print(f"  候选：{det.get('top3')}")
print(f"  nearby_rejected={det.get('nearby_rejected')}")

print("\n逐个候选复算邻居窗口（照抄 _nearby_ok 的逻辑，但把中间量打出来）：")
for item in det.get("top3") or []:
    box, score, dist, nb = item[0], item[1], item[2], item[3]
    cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
    print(f"  候选 box={list(box)} 中心=({cx},{cy}) dist={dist} 记录的 nearby_ok={nb}")
    for it in nearby[:2]:
        off = it.get("offset") or [0, 0]
        rect = it.get("rect_in_page") or [0, 0, 160, 28]
        ex, ey = cx + int(off[0]), cy + int(off[1])
        half_w = max(int(rect[2]) // 2 + 40, 90)
        half_h = max(int(rect[3]) // 2 + 20, 40)
        x0, y0 = max(0, ex - half_w), max(0, ey - half_h)
        sx, sy = min(pw - x0, 2 * half_w), min(ph - y0, 2 * half_h)
        if sx < 24 or sy < 16:
            print(f"    邻居 {it['text']!r}: 窗口太小 sx={sx} sy={sy} → 跳过")
            continue
        strip = page[y0:y0 + sy, x0:x0 + sx]
        hit = matcher.find_text_ocr(strip, matcher.text_needle_short(it["text"]), thr=0.72)
        words = matcher.ocr_run(strip)["txts"]
        print(f"    邻居 {it['text']!r}: 期望中心=({ex},{ey}) 窗口=({x0},{y0},{sx},{sy}) "
              f"→ 命中={hit['ok']} score={round(hit.get('score', 0), 3)}")
        print(f"      窗口内 OCR={words}")
