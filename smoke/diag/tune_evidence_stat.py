# -*- coding: utf-8 -*-
"""诊断 tune_evidence.jsonl 的证据质量（临时工具）。"""
import collections
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "engine/scripts/tune_evidence.jsonl"
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
g = collections.defaultdict(lambda: [0, 0, 0, 0])
for r in rows:
    v = g[r["group"]]
    v[0] += 1
    v[1] += len(r["cands"])
    v[2] += 1 if r["truth_index"] is not None else 0
    v[3] += 1 if r["truth_index"] is None else 0
print("{:18s} {:>3s} {:>6s} {:>4s} {:>4s}".format("group", "n", "cands", "pos", "neg"))
for k, v in sorted(g.items()):
    print("{:18s} {:3d} {:6d} {:4d} {:4d}".format(k, v[0], v[1], v[2], v[3]))
print()
for r in rows:
    cs = [(round(c["score"], 2), c["dist"], c["nearby_ok"]) for c in r["cands"]]
    print("{:14s} truth={:>4} cands={}".format(r["id"], str(r["truth_index"]), cs))
