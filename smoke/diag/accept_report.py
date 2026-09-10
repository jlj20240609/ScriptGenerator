# -*- coding: utf-8 -*-
"""真人验收取证：截屏 + OCR + 定位日志统计（被动，不动鼠标键盘）。

用法（你跑完验收后我执行）：
    python smoke/diag/accept_report.py
"""
import json
import os
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, matcher  # noqa: E402

capture.init_dpi_aware()
OUT = Path(__file__).with_name("accept")
OUT.mkdir(exist_ok=True)
STAMP = time.strftime("%H%M%S")

print("=== 窗口状态 ===")
for title, tag in (("脚本构建器", "ui"), ("M0 演示登录", "page")):
    hits = [h for h in capture.find_windows_by_title(title)]
    for h in hits[:2]:
        l, t, r, b = capture.window_rect(h)
        vis, ico = bool(capture.is_iconic(h)), capture.is_iconic(h)
        print(f"[{tag}] hwnd={h} 最小化={int(bool(ico))} {capture.window_rect(h)}")
        if capture.is_iconic(h):
            continue
        img = capture.grab_screen((l, t, r - l, b - t))
        if img is None:
            continue
        p = OUT / f"{tag}_{STAMP}.png"
        cv2.imwrite(str(p), img)
        txts = matcher.ocr_run(img)["txts"]
        print(f"   截图 → {p.name}；OCR({len(txts)}): {txts[:26]}")

print("\n=== 定位日志统计（本次运行新增部分）===")
loc = Path(os.environ.get("TEMP", ".")) / "m1_ui_loc.jsonl"
if not loc.exists():
    print("  没有定位日志文件")
else:
    rows = []
    for line in loc.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    # 只统计最近 15 分钟内的记录（对应本次验收）
    now = time.time()
    recent = []
    for r in rows[-400:]:
        ts = r.get("ts", "")
        try:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if now - t < 900:
            recent.append(r)
    print(f"  最近 15 分钟事件 {len(recent)} 条")
    by_event = {}
    for r in recent:
        by_event.setdefault(r.get("event"), []).append(r)
    for ev, items in by_event.items():
        print(f"  · {ev}: {len(items)} 次")
        for r in items[-4:]:
            ms = (r.get("screen_meta") or {}).get("dpi")
            extra = r.get("extra") or {}
            print(f"      {r.get('ts','')[11:19]} step={r.get('step_id')} "
                  f"method={r.get('method')} conf={r.get('confidence')} "
                  f"ok={extra.get('ok')} level={extra.get('level')} "
                  f"ms={extra.get('elapsed_ms')} dpi={ms} "
                  f"{('reason=' + str(extra.get('reason'))) if extra.get('reason') else ''}")
    fails = [r for r in recent if r.get("event") in ("procedural_fail", "outcome_check")
             and (r.get("extra") or {}).get("ok") is False]
    print(f"  失败/未命中事件: {len(fails)} 条")
    for r in fails[-6:]:
        print(f"      {r.get('ts','')[11:19]} {r.get('event')} {r.get('step_id')} "
              f"{(r.get('extra') or {}).get('reason') or ''}")

print("\n=== 保存过的脚本 ===")
for p in sorted(Path(os.environ.get("TEMP", ".")).glob("*.sgscript.json")):
    try:
        sg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    steps = sg.get("steps") or []
    print(f"  {p.name}  {p.stat().st_size} 字节  步骤 {len(steps)}  targets_rev={sg.get('targets_rev')}")
    for st in steps:
        t = st.get("target") or {}
        c = (st.get("condition") or {}).get("target") or {}
        print(f"     - {st.get('type')}/{st.get('action') or ''} "
              f"text={(t.get('text') or c.get('text') or '')!r} "
              f"nearby={len(t.get('nearby') or [])}")
