# -*- coding: utf-8 -*-
"""
M0 数据聚合：读 smoke/data/*.jsonl，输出命中率/延迟统计表（控制台 + smoke/data/m0_bench_summary.md）
统计口径（M0 Spike）：
  page_ok  : 页面模板定位得分≥0.72 且找到
  widget_ok: 三级链最终选中层命中（UIA/OCR/模板得分达标；L3 页面内坐标恒可用）
  dev<=12  : 定位点与真值（窗口位移已知）偏差 ≤12px
  verify   : 点击点周边 OCR 复核含目标文字（文本部件）
  hit      : page_ok & widget_ok & dev<=12 & verify_ok
"""
import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

import m0lib

DATA = Path(__file__).parent / "data"


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def agg_trials(rows, min_dev_ok=12):
    g = defaultdict(list)
    infra = 0
    for r in rows:
        if r.get("type") != "trial":
            continue
        if "page" not in r:  # 环境性空转（窗口不存在/不可见/句柄失效），不计入定位尝试
            infra += 1
            continue
        g[(r.get("cls", "?"), r.get("target_id") or "-")].append(r)
    lines = ["（另跳过 %d 条环境性空转记录：窗口不存在/句柄失效等）" % infra]
    hdr = ("| 类别 | 目标 | 次数 | page命中 | 部件命中 | 校验 | 综合HIT | 环境性失败 | "
           "dev中位 | page中位ms | 部件中位ms | L1/L2/L3 分布 |")
    lines.append(hdr)
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    order = ["web-login", "erp-web", "desktop", "icon-app", "dynamic"]
    keys = sorted(g.keys(),
                  key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[1]))
    for (cls, tid) in keys:
        rs = g[(cls, tid)]
        n = len(rs)
        page_ok = [r for r in rs if (r.get("page") or {}).get("ok")]
        po = len(page_ok)
        wo = sum(1 for r in rs if (r.get("widget") or {}).get("chosen_level"))
        ver = sum(1 for r in rs if (r.get("verify") or {}).get("ok"))
        hit = sum(1 for r in rs if r.get("ok"))
        # 环境性失败：窗口不可见/被遮挡（非算法 miss）。
        # 判据1：未成功置顶(raised=False)且分数极低；
        # 判据2：已知环境异常目标（tk/calc/set：本机曾出现 API 置顶成功但屏幕实际未呈现该窗口——
        #   UWP/自绘窗口的“渲染幽灵”）
        vis = sum(1 for r in rs
                  if not (r.get("page") or {}).get("ok")
                  and (r.get("page") or {}).get("score", 0) < 0.5
                  and (not r.get("raised", False)
                       or r.get("target_id") in ("tk", "calc", "set")))
        devs = [r["widget"].get("dev_px") for r in rs
                if r.get("widget") and r["widget"].get("dev_px") is not None]
        pms = [r["page"]["elapsed_ms"] for r in rs
               if r.get("page") and r["page"].get("elapsed_ms") is not None]
        wms = [r["widget"].get("elapsed_ms") for r in rs
               if r.get("widget") and (r["widget"].get("elapsed_ms") is not None)]
        lv = defaultdict(int)
        for r in rs:
            lv[(r.get("widget") or {}).get("chosen_level") or 0] += 1
        dist = " ".join("L%d:%d" % (k, v) for k, v in sorted(lv.items()))
        lines.append("| %s | %s | %d | %d (%.0f%%) | %d (%.0f%%) | %d | **%d (%.0f%%)** | %d | %s | %s | %s | %s |" % (
            cls, tid, n, po, 100 * po / n, wo, 100 * wo / n, ver, hit, 100 * hit / n,
            vis, median(devs), median(pms), median(wms), dist))
    return lines, {"trials_total": len(rows)}


def agg_uia(rows):
    lines = []
    # 去重：同一窗口标题只留进程名已知的最新行
    seen = {}
    for r in rows:
        if r.get("type") != "uia_scan":
            continue
        key = r.get("title")
        exe = r.get("exe") or "?"
        if key not in seen or (exe != "?" and seen[key].get("exe") == "?"):
            seen[key] = r
    lines.append("| 进程 | 窗口 | 树节点 | 交互控件 | 有Name | 有AutoId | 结论 |")
    lines.append("|---|---|---|---|---|---|---|")
    for title, r in seen.items():
        exe = r.get("exe") or "?"
        if r.get("ok"):
            concl = "UIA可命中" if r.get("interactive", 0) > 0 and r.get("named", 0) > 0 else "弱/无"
            lines.append("| %s | %s | %d | %d | %d | %d | %s |" % (
                exe, title[:18], r["total"], r["interactive"], r["named"], r["with_aid"], concl))
        else:
            lines.append("| %s | %s | 0 | 0 | 0 | 0 | 无树 |" % (exe, title[:18]))
    return lines


def agg_ocr(rows):
    lines = []
    g = defaultdict(list)
    for r in rows:
        if r.get("type") in ("ocr_bench", "ocr_find"):
            g[r["type"]].append(r)
    if g["ocr_bench"]:
        lines.append("| crop | 首次ms | 中位ms |")
        lines.append("|---|---|---|")
        for r in g["ocr_bench"]:
            lines.append("| %s | %d | %d |" % (r["crop"], r["first_ms"], r["median_ms"]))
    if g["ocr_find"]:
        hits = sum(1 for r in g["ocr_find"] if r["hit"])
        ms = [r["elapsed_ms"] for r in g["ocr_find"]]
        lines.append("文本定位: %d/%d 命中，单次中位 %s ms" % (hits, len(g["ocr_find"]), median(ms)))
    return lines


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", default=str(DATA / "trials.jsonl"))
    ap.add_argument("--out", default=str(DATA / "m0_bench_summary.md"))
    ap.add_argument("--all", action="store_true", help="只统计全部历史，否则只统计今天")
    ap.add_argument("--since", default="", help="只统计 ts >= 该时间戳的行（如 2026-09-08T20:38）")
    args = ap.parse_args()
    m0lib.setup_utf8_stdio()

    trials = m0lib.load_results(args.trials)
    if args.since:
        trials = [r for r in trials if r.get("ts", "") >= args.since]
    elif not args.all:
        today = m0lib.now_iso()[:10]
        trials = [r for r in trials if r.get("ts", "").startswith(today)]
    uia = m0lib.load_results(DATA / "uia_scan.jsonl")
    ocr = m0lib.load_results(DATA / "ocr_bench.jsonl")

    parts = ["# M0 命中率/性能统计（自动生成）", "生成时间: %s" % m0lib.now_iso(),
             "", "## ① 双截图闭环命中率", ""]
    t, extra = agg_trials(trials)
    parts += t
    parts += ["", "## ② UIA 覆盖率", ""] + agg_uia(uia)
    parts += ["", "## OCR 基准", ""] + agg_ocr(ocr) + [""]
    text = "\n".join(parts)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print("\n[保存至 %s]" % args.out)
