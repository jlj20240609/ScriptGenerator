# -*- coding: utf-8 -*-
"""
engine/scripts/dpi_report.py — M2-WP8 跨分辨率验证：环境记录 + 跑批归档 + 对比报告。

用法：
  python engine/scripts/dpi_report.py --env                 # 看当前 DPI/缩放/分辨率
  python engine/scripts/dpi_report.py --archive 125dpi      # 把当前 bench_raw.jsonl 归档
  python engine/scripts/dpi_report.py --compare 125dpi 150dpi [--compare ...]  # 生成对比报告

为什么要有归档这一步：跑批数据文件只有一份（bench_raw.jsonl），换 DPI 再跑就会把上一套
覆盖掉。归档一份 + 记下当时的 DPI/分辨率，"跨机验证一次"才有据可查。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

ARCH_DIR = ROOT / "engine" / "scripts" / "bench_archives"
DOC = ROOT / "docs" / "M2_跨分辨率报告.md"


def env_info() -> dict:
    """当前显示环境：DPI、缩放百分比、物理与逻辑分辨率。

    DPI-aware 进程里 GetSystemMetrics 给的是**物理像素**，逻辑尺寸 = 物理 / 缩放。
    M1 的教训：DPI 不感知的进程会把虚拟化的逻辑尺寸当成"物理"，定位与截图全错。
    """
    capture.init_dpi_aware()
    x, y, w, h = capture.virtual_screen_rect()
    dpi = capture.dpi_of()
    scale = (dpi or 96) / 96.0
    return {"dpi": int(dpi or 96), "scale": round(scale, 4),
            "percent": int(round(scale * 100)),
            "phys": [int(w), int(h)],
            "logical": [int(round(w / scale)), int(round(h / scale))]}


def archive(name: str, src: Path = None, force: bool = False) -> Path:
    """把当前跑批数据归档到 bench_archives/bench_raw_<name>.jsonl（附环境 meta）。"""
    src = src or B.RAW
    if not src.exists():
        raise FileNotFoundError(f"没有可归档的数据：{src}")
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARCH_DIR / f"bench_raw_{name}.jsonl"
    if dst.exists() and not force:
        raise FileExistsError(f"归档已存在（换个名字或用 --force）：{dst}")
    # 必须**去重**：跑批过程中可能有重复轮次（例如中途重启过），直接搬原始行会让报告把
    # 同一轮算两次（实测：125dpi 归档显示 140 轮，实际 132 轮）。
    rows = list(B.load_dedup(src).values())
    dst.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    meta = {"name": name, "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "env": env_info(), "rows": len(rows), "source": str(src)}
    (ARCH_DIR / f"bench_raw_{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst


def load_archive(name: str):
    """读一份归档 → (rows, meta)。"""
    p = ARCH_DIR / f"bench_raw_{name}.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"找不到归档：{p}")
    mp = ARCH_DIR / f"bench_raw_{name}.meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {"name": name}
    return B.load_raw(p), meta


def compare_table(archives: list) -> str:
    """多份归档 → Markdown 对比表（archives = [(name, rows, meta), ...]）。"""
    lines = ["| 环境 | 案例 | 轮数 | 成功率 | 无人工介入(严格) | 无人工介入(仅异常) | 误报 |",
             "|---|---|---|---|---|---|---|"]
    total = {"n": 0, "ok": 0, "clean": 0, "mis": 0}
    for name, rows, meta in archives:
        env = meta.get("env") or {}
        env_txt = f"{name}（{env.get('percent', '?')}% · {env.get('dpi', '?')} DPI）"
        st = B.summarize(rows)
        for case, s in st["by_case"].items():
            lines.append(f"| {env_txt} | {case} | {s['n']} | {s['ok_rate']:.0f}% | "
                         f"{s['clean_rate']:.0f}% | {s['clean_manual_rate']:.0f}% | "
                         f"{s['misreport']} |")
        lines.append(f"| **{env_txt} 合计** | — | {st['total']} | {st['ok_rate']:.1f}% | "
                     f"**{st['clean_rate']:.1f}%** | {st['clean_manual_rate']:.1f}% | "
                     f"{st['misreport']} |")
        total["n"] += st["total"]
        total["ok"] += st["ok"]
        total["clean"] += st["clean"]
        total["mis"] += st["misreport"]
    return "\n".join(lines)


def write_doc(archives: list) -> str:
    """生成 docs/M2_跨分辨率报告.md。"""
    envs = [f"{n}（{(m.get('env') or {}).get('percent', '?')}%）" for n, _, m in archives]
    rates = []
    for _n, rows, _m in archives:
        st = B.summarize(rows)
        rates.append(st["clean_rate"])
    ok_all = all(r >= 90.0 for r in rates) if rates else False
    lines = ["# M2 跨分辨率报告（WP8）", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 参与对比的环境：{'、'.join(envs)}",
             "- 口径：无人工介入 = 运行 ok 且不需要人处理异常（脚本自带的「提示我」不算）；"
             "误报 = 报成功但终态断言不成立", "",
             "## 1. 对比表", "", compare_table(archives), "",
             "## 2. 结论", "",
             f"- 各环境下无人工介入率："
             + "、".join(f"{n} {r:.1f}%" for (n, _, _), r in zip(archives, rates)),
             f"- 是否全部达到 DoD ≥90%：**{'是' if ok_all else '否'}**"]
    if not ok_all:
        lines.append("- 未达标的环境需要看该批报告里的「未达标轮次明细」（轮号 + 原因 + 截图）。")
    lines += ["", "## 3. 环境记录", ""]
    for name, rows, meta in archives:
        env = meta.get("env") or {}
        lines.append(f"- **{name}**：{env.get('percent', '?')}%（{env.get('dpi', '?')} DPI）；"
                     f"物理 {env.get('phys')}；逻辑 {env.get('logical')}；"
                     f"{meta.get('rows', len(rows))} 轮；归档于 {meta.get('archived_at', '?')}")
    lines += ["", "## 4. 复现方式", "",
              "```",
              "python engine/scripts/bench.py --rounds 20 --fresh      # 在当前缩放下跑一批",
              "python engine/scripts/dpi_report.py --archive 125dpi    # 归档并记录环境",
              "# 手动把 Windows 显示缩放改成 150%，重新登录桌面后：",
              "python engine/scripts/bench.py --rounds 20 --fresh",
              "python engine/scripts/dpi_report.py --archive 150dpi",
              "python engine/scripts/dpi_report.py --compare 125dpi 150dpi",
              "```"]
    doc = "\n".join(lines) + "\n"
    DOC.write_text(doc, encoding="utf-8")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", action="store_true", help="打印当前显示环境")
    ap.add_argument("--archive", default="", help="把当前跑批数据归档为这个名字")
    ap.add_argument("--force", action="store_true", help="覆盖同名归档")
    ap.add_argument("--compare", nargs="*", default=[], help="对比这些归档并生成报告")
    args = ap.parse_args()

    if args.env or not (args.archive or args.compare):
        e = env_info()
        print(f"当前显示：{e['percent']}%（{e['dpi']} DPI）；"
              f"物理 {e['phys'][0]}x{e['phys'][1]}；逻辑 {e['logical'][0]}x{e['logical'][1]}")
    if args.archive:
        p = archive(args.archive, force=args.force)
        print(f"已归档：{p}（另存 meta：bench_raw_{args.archive}.meta.json）")
    if args.compare:
        archives = []
        for name in args.compare:
            rows, meta = load_archive(name)
            archives.append((name, rows, meta))
            print(f"读入 {name}：{len(rows)} 轮")
        write_doc(archives)
        print(f"对比报告：{DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
