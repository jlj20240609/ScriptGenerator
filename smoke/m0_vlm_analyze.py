# -*- coding: utf-8 -*-
"""VLM 像素定位误差分析：从 vlm_geo.jsonl 提取点式样本，
做 x/y 方向线性校准回归（est = a*truth + b），报告 R²、斜率、截距、残差与命中率@多档容差。
结论判定：若 a≈1 且 R²>0.9 → 误差随机小，可直接用；若 R² 高但 a≠1/截距大 → 可校准；
若 R² 低 → 不可靠，纯 VLM 像素定位判不可用。
"""
import json
import sys
from pathlib import Path

import numpy as np

import m0lib

m0lib.setup_utf8_stdio()
p = Path(__file__).parent / "data" / "vlm_geo.jsonl"
rows = []
if p.exists():
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))

point_rows = [r for r in rows
              if r.get("style") in ("point_px", "point_norm", "think")
              and r.get("est") and r.get("truth")]
bbox_rows = [r for r in rows if r.get("style") == "bbox_px" and r.get("iou") is not None]
print("点式样本 %d，框式样本 %d" % (len(point_rows), len(bbox_rows)))

# 按 (model, style) 分组
from collections import defaultdict
groups = defaultdict(list)
for r in point_rows:
    groups[(r.get("model"), r.get("style"))].append(r)
for (model, style), rs in sorted(groups.items()):
    X = np.array([r["truth"][0] for r in rs], float)
    Y = np.array([r["est"][0] for r in rs], float)
    Xy = np.array([r["truth"][1] for r in rs], float)
    Yy = np.array([r["est"][1] for r in rs], float)
    print("\n[%s / %s] n=%d" % (model, style, len(rs)))
    for name, (t, e) in (("x", (X, Y)), ("y", (Xy, Yy))):
        A = np.vstack([t, np.ones_like(t)]).T
        a, b = np.linalg.lstsq(A, e, rcond=None)[0]
        pred = A @ np.array([a, b])
        ss_res = float(((e - pred) ** 2).sum())
        ss_tot = float(((e - e.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        resid_std = float(np.std(e - pred))
        print("  %s: est = %.3f*truth %+.1f | R²=%.3f 残差std=%.1fpx" % (name, a, b, r2, resid_std))
    devs = [r["dev_px"] for r in rs if r.get("dev_px") is not None]
    ib = sum(1 for r in rs if r.get("in_box"))
    if devs:
        devs.sort()
        print("  dev: 中位=%.0f p75=%.0f p90=%.0f max=%.0f | 框内 %d/%d (%.0f%%) | ≤60px: %d (%.0f%%)" %
              (np.median(devs), np.percentile(devs, 75), np.percentile(devs, 90),
               max(devs), ib, len(rs), 100 * ib / len(rs),
               sum(1 for d in devs if d <= 60), 100 * sum(1 for d in devs if d <= 60) / len(devs)))

if bbox_rows:
    ious = [r["iou"] for r in bbox_rows]
    for thr in (0.3, 0.5):
        n = sum(1 for v in ious if v >= thr)
        print("\n框式 IoU≥%.1f: %d/%d (%.0f%%)" % (thr, n, len(ious), 100 * n / len(ious)))
    print("框式 IoU 中位: %.2f" % np.median(ious))
    # bbox 中心偏差
    cdev = [r.get("dev_px") for r in bbox_rows if r.get("dev_px") is not None]
    if cdev:
        print("框中心 dev 中位: %.1f" % np.median(cdev))

# 耗时统计
if rows:
    ms = [r["elapsed_ms"] for r in rows if r.get("elapsed_ms")]
    print("\n请求耗时: n=%d 中位=%.0fms p90=%.0fms" % (len(ms), np.median(ms),
                                                     np.percentile(ms, 90)))
