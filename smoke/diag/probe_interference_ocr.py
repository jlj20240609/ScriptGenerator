# -*- coding: utf-8 -*-
"""诊断：干扰页上 OCR 到底读到几个「查询」、各自分数如何。

跑法：python smoke/diag/probe_interference_ocr.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, matcher  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

capture.init_dpi_aware()
case = B.CASES["interference"]
hwnd = B.ensure_case_window(case)
B.reset_page(hwnd, case)
B.ensure_foreground(hwnd)
time.sleep(1.0)

l, t, r, b = capture.window_rect(hwnd)
page = capture.grab_screen((l, t, r - l, b - t))
import cv2  # noqa: E402
out = ROOT / "smoke" / "diag" / "interference_page.png"
cv2.imwrite(str(out), page)
print(f"页面图 {page.shape[1]}x{page.shape[0]} → {out}")
words = matcher.ocr_run(page)["txts"]
print("整页 OCR 文字：")
for w in words:
    print(f"   {w!r}")

print("\n各门槛下「查询」的命中：")
for thr in (0.5, 0.7, 0.75, 0.8):
    hits = matcher.find_text_all_ocr(page, "查询", thr=thr)
    print(f"  thr={thr}: {[(h['matched_text'], round(h['score'], 3), list(h['box'])) for h in hits]}")

print("\n关键文字的框（用来还原布局：谁在哪一行/哪一列）：")
for w in ("物料查询", "物料编码", "物料名称", "按编码精确匹配", "按名称模糊匹配",
          "供应商查询", "供应商", "盘点单据", "库存统计", "保存", "保存并关闭", "查询"):
    hits = matcher.find_text_all_ocr(page, w, thr=0.5)
    got = [(h["matched_text"], list(h["box"])) for h in hits]
    print(f"  {w!r:22} → {got if got else '未渲染/未读到'}")

sg = B.schema.load(B.ASSETS / "interference.sgscript.json")
tgt = sg["steps"][0]["target"]
print(f"\n资产目标框 rect_in_page={tgt['rect_in_page']}")
print(f"邻居记录={[(x['text'], x['offset']) for x in (tgt.get('nearby') or [])]}")
print(f"\n直接看这个目标在页面上的候选（走 ②a）：")
res = B.locator.locate_widget(page, (0, 0, page.shape[1], page.shape[0]), tgt)
det = (res.get("detail") or {}).get("l2_ocr") or {}
print(f"  ok={res['ok']} method={res.get('method')} level={res.get('level')}")
print(f"  cands={det.get('cands')} near={det.get('near')} far={det.get('far')}")
print(f"  top={det.get('top3')}")
