"""定位质量统计 CLI（纯离线解析定位日志，不抓屏、不点击、不改脚本）。

用法：
    python engine/scripts/loc_stats.py <日志.jsonl>
    python engine/scripts/loc_stats.py --app                 # 读界面的 %TEMP%/m1_ui_loc.jsonl
    python engine/scripts/loc_stats.py <日志.jsonl> --json out.json --md out.md
    python engine/scripts/loc_stats.py <日志.jsonl> --step s3_rdty

三个口径（避免各说各话）：
    一次命中率 = 部件定位中 ok=true 的比例
    层级占比   = 成功定位里各层占多少（l1 UI树 / l2 部件相似度 / l3 页面内坐标兜底）
    盲点率     = 成功定位里靠第 3 层兜底的比例 —— 越高越危险
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import locstats  # noqa: E402


def _default_logs():
    """没给路径时按优先级找：仓库根的 engine_loc.jsonl、界面的 %TEMP%/m1_ui_loc.jsonl。"""
    cands = [ROOT / "engine_loc.jsonl"]
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        cands.append(Path(tmp) / "m1_ui_loc.jsonl")
    return cands


def main(argv=None):
    ap = argparse.ArgumentParser(description="定位质量统计")
    ap.add_argument("log", nargs="?", help="定位日志 JSONL 路径")
    ap.add_argument("--app", action="store_true",
                    help="读界面写到临时目录的 m1_ui_loc.jsonl")
    ap.add_argument("--json", dest="json_out", help="把报告写成 JSON")
    ap.add_argument("--md", dest="md_out", help="把报告写成 Markdown")
    ap.add_argument("--step", help="只看某一步（如 s3_rdty）")
    ap.add_argument("--tail", type=int, help="只看日志最后 N 行 —— 圈定最近一次运行用这个")
    ap.add_argument("--since", help="只看这个时间之后的行（ISO 时间，或只写 HH:MM）")
    args = ap.parse_args(argv)

    if args.log:
        path = Path(args.log)
    elif args.app:
        tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
        path = Path(tmp) / "m1_ui_loc.jsonl"
    else:
        found = [p for p in _default_logs() if p.exists()]
        if not found:
            print("没找到定位日志。给一个路径，或先跑一次脚本再执行本命令。")
            return 2
        path = found[0]

    rows = locstats.load_rows(path)
    if not rows:
        print(f"{path} 里没有可解析的定位日志行。")
        return 2
    if args.since:
        key = args.since.strip()
        if len(key) <= 5:                       # 只写 HH:MM → 比当天时间部分
            rows = [r for r in rows if str(r.get("ts") or "")[11:16] >= key]
        else:
            rows = [r for r in rows if str(r.get("ts") or "") >= key]
    if args.tail:
        rows = rows[-int(args.tail):]
    if not rows:
        print("按 --since/--tail 过滤后没有剩余的行。")
        return 2
    if args.step:
        rows = [r for r in rows if str(r.get("step_id")) == args.step]
        if not rows:
            print(f"日志里没有步骤 {args.step} 的行。")
            return 2

    rep = locstats.analyze(rows)
    rep["source"] = str(path)
    print(f"日志：{path}\n")
    print(locstats.render_text(rep))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"\nJSON 已写入 {args.json_out}")
    if args.md_out:
        Path(args.md_out).write_text(locstats.render_markdown(rep), encoding="utf-8")
        print(f"Markdown 已写入 {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
