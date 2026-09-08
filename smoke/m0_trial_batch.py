# -*- coding: utf-8 -*-
"""M0 批量试跑：对 smoke/targets 下每个目标各跑 N 轮闭环定位+点击。
用法: python smoke/m0_trial_batch.py --n 8 [--no-move] [--interval 1.0]
输出: 控制台逐条 + smoke/data/trials.jsonl
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import m0lib
import m0_trial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8, help="每目标轮数")
    ap.add_argument("--move", dest="move", action="store_true", help="每轮随机移动窗口")
    ap.add_argument("--no-move", dest="move", action="store_false")
    ap.set_defaults(move=True)
    ap.add_argument("--interval", type=float, default=0.8)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--names", default="", help="逗号分隔的目标名子集（默认全部）")
    args = ap.parse_args()

    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    tdir = m0_trial.CAPTURE_DIR
    want = {s.strip() for s in args.names.split(",") if s.strip()}
    paths = sorted(tdir.glob("*.json"))
    paths = [p for p in paths if not want or p.stem in want]
    if not paths:
        raise SystemExit("没有目标文件")
    print("目标顺序: %s" % [p.stem for p in paths])
    with tempfile.TemporaryDirectory(prefix="m0run_") as tmp:
        for p in paths:
            tmpdir = Path(tmp) / p.stem
            tmpdir.mkdir(parents=True)
            shutil.copy(p, tmpdir / p.name)
            ns = SimpleNamespace(cmd="run", dir=str(tmpdir), count=args.n,
                                 move=args.move, interval=args.interval, seed=args.seed)
            print("\n############ 目标 %s 开始 %d 轮 ############" % (p.stem, args.n), flush=True)
            try:
                m0_trial.do_run(ns)
            except Exception as e:
                print("!! %s run 异常: %r" % (p.stem, e), flush=True)
    print("\n批量试跑完成")


if __name__ == "__main__":
    main()
