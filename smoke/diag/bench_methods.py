# -*- coding: utf-8 -*-
"""小工具：按案例统计定位方式分布（按 (案例,轮号) 去重取最新一轮）。

用法：python smoke/diag/bench_methods.py [案例名]
"""
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / "engine" / "scripts" / "bench_raw.jsonl"
rows = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    rows[(r.get("case"), r.get("round"))] = r

want = sys.argv[1] if len(sys.argv) > 1 else ""
by_case = collections.defaultdict(lambda: {"n": 0, "ok": 0, "clean": 0, "delta": [],
                                           "m": collections.Counter()})
for (case, _round), r in rows.items():
    if want and case != want:
        continue
    s = by_case[case]
    s["n"] += 1
    s["ok"] += 1 if r.get("status") == "ok" else 0
    s["clean"] += 1 if (r.get("status") == "ok" and not r.get("prompts_manual")) else 0
    if r.get("page_delta"):
        s["delta"].append(r["page_delta"])
    for k, v in (r.get("methods") or {}).items():
        s["m"][k] += v

for case, s in sorted(by_case.items()):
    dl = sorted(s["delta"])
    print(f"[{case}] n={s['n']} ok={s['ok']} 零人工={s['clean']} "
          f"页面变化中位={dl[len(dl) // 2] if dl else '—'}")
    for k, v in s["m"].most_common():
        print(f"    {k:<30} {v}")
