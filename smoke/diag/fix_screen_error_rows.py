# -*- coding: utf-8 -*-
"""把历史跑批数据里"屏幕不可用"的轮次标出来。

旧版 bench.py 没有 screen_error 字段，那几轮（抓屏被拒/卡死）会被续跑逻辑当成"已完成"跳过，
永久污染基线。这里按 error 文本回填标记，让续跑时重跑它们。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
path = ROOT / "engine" / "scripts" / "bench_raw.jsonl"
if not path.exists():
    print("没有跑批数据")
    sys.exit(0)

rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
KEYS = ("BitBlt", "拒绝访问", "driver_error", "round_timeout", "超时")
fixed = 0
slow = 0
for r in rows:
    err = str(r.get("error") or "")
    hit = any(k in err for k in KEYS) or float(r.get("ms") or 0) > 300000
    if hit and not r.get("screen_error"):
        r["screen_error"] = True
        fixed += 1
    if hit:
        slow += 1
        print(f"  {r.get('case')} #{r.get('round')} ms={int(float(r.get('ms') or 0))} "
              f"err={err[:70]}")

path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8")
print(f"回填 {fixed} 条；共 {slow} 条会被续跑重跑；总记录 {len(rows)} 条")
