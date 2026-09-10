# -*- coding: utf-8 -*-
"""证据统计：这批证据到底能考什么参数、有多少条真能区分。

用法：python smoke/diag/evidence_stat.py [证据文件]
"""
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "engine" / "scripts" / "bench_evidence.jsonl"
rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

by_case = collections.defaultdict(list)
for r in rows:
    by_case[r.get("group") or "?"].append(r)

print(f"证据文件：{path.name}；共 {len(rows)} 条\n")
print(f"{'案例':14s} {'条数':>4s} {'候选':>5s} {'多候选':>6s} {'真值在候选':>9s} "
      f"{'真值靠邻居':>10s} {'干扰更近':>8s}")
for case, rs in sorted(by_case.items()):
    n = len(rs)
    cands = sum(len(r["cands"]) for r in rs)
    multi = sum(1 for r in rs if len(r["cands"]) > 1)
    has_truth = sum(1 for r in rs if r.get("truth_index") is not None)
    by_nb = 0
    dist_disc = 0
    for r in rs:
        t = r.get("truth_index")
        if t is None:
            continue
        tc = r["cands"][t]
        if tc.get("nearby_ok") is True:
            by_nb += 1
            others = [c for i, c in enumerate(r["cands"]) if i != t]
            # 关键：干扰候选离录点**更近**时，"邻居优先"才真正决定选谁
            if any(c.get("dist", 0) < tc.get("dist", 0) for c in others):
                dist_disc += 1
    print(f"{case:14s} {n:4d} {cands:5d} {multi:6d} {has_truth:9d} {by_nb:10d} {dist_disc:8d}")

print("\n说明：「干扰更近」= 真值候选邻居对得上、且至少有一个候选离录点比它更近。"
      "\n      只有这一列才是「邻居优先 vs 就近优先」真正被考到的条数。")
