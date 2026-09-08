# -*- coding: utf-8 -*-
"""
M0② UIA 覆盖率扫描：统计当前打开的顶层窗口的 UI 树可命中比例。
指标：窗口数 / 可枚举控件数 / 交互控件数 / 有 Name / 有 AutomationId / 有 ControlType。
输出：控制台表格 + smoke/data/uia_scan.jsonl
用法：python smoke/m0_uia_scan.py [--max-nodes 8000] [--top 20]
可选：--include-chrome 也扫 Chromium 系进程内部（树大，慢）
"""
import argparse
import json
import time
from pathlib import Path

import m0lib


def enum_top_windows():
    import win32gui
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title or len(title) > 120:
            return True
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        out.append({"hwnd": hwnd, "title": title, "pid": pid})
        return True

    win32gui.EnumWindows(cb, None)
    return out


def process_name(pid):
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return "?"
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_ulong(32768)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            if not ok:
                return "?"
            name = buf.value.split("\\")[-1] or "?"
            return name
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return "?"


def scan_window(info, max_nodes, max_ms):
    import uiautomation as auto
    t0 = time.perf_counter()
    root = auto.ControlFromHandle(info["hwnd"])
    if root is None:
        return {"ok": False, "reason": "no_root"}
    total, interactive, named, with_aid = 0, 0, 0, 0
    ctl_types = {}
    visited = 0
    deadline = time.perf_counter() + max_ms / 1000
    found_children = False

    def walk(e, depth):
        nonlocal total, interactive, named, with_aid, visited, found_children
        if depth > 12 or time.perf_counter() > deadline or visited >= max_nodes:
            return
        try:
            children = e.GetChildren()
        except Exception:
            return
        for c in children:
            visited += 1
            found_children = True
            try:
                ctl = type(c).__name__
                nm = (c.Name or "") if c.Name else ""
                aid = (c.AutomationId or "") if c.AutomationId else ""
                # BoundingRectangle 访问会触发跨进程查询，此处只统计树结构
            except Exception:
                ctl, nm, aid = "?", "", ""
            total += 1
            ctl_types[ctl] = ctl_types.get(ctl, 0) + 1
            if ctl in m0lib._UIA_INTERACTIVE:
                interactive += 1
            if nm:
                named += 1
            if aid:
                with_aid += 1
            walk(c, depth + 1)

    walk(root, 0)
    elapsed = (time.perf_counter() - t0) * 1000
    return {"ok": True, "elapsed_ms": round(elapsed), "total": total,
            "interactive": interactive, "named": named, "with_aid": with_aid,
            "visited": visited, "timed_out": visited >= max_nodes or time.perf_counter() > deadline,
            "has_children": found_children,
            "top_types": sorted(ctl_types.items(), key=lambda kv: -kv[1])[:6]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-nodes", type=int, default=6000)
    ap.add_argument("--max-ms", type=float, default=5000.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    wins = enum_top_windows()
    rows = []
    print("检测到 %d 个可见顶层窗口，扫描前 %d 个" % (len(wins), min(args.top, len(wins))))
    print("%-6s %-24s %-10s %8s %8s %8s %8s %10s %s" %
          ("#", "process", "title[:24]", "total", "interact", "named", "aid", "ms", "types[:3]"))
    seen_pids = set()
    for i, w in enumerate(wins[:args.top]):
        exe = process_name(w["pid"])
        # 同进程多窗口只扫第一个，避免重复计数
        if w["pid"] in seen_pids and i < args.top:
            continue
        seen_pids.add(w["pid"])
        r = scan_window(w, args.max_nodes, args.max_ms)
        if not r.get("ok"):
            print("%-6d %-24s %-10s 无 UI 树: %s" % (i + 1, exe[:24], w["title"][:10], r.get("reason")))
            rows.append({"type": "uia_scan", "exe": exe, "title": w["title"], "ok": False,
                         "reason": r.get("reason")})
            continue
        print("%-6d %-24s %-10.10s %8d %8d %8d %8d %10d %s" %
              (i + 1, exe[:24], w["title"], r["total"], r["interactive"], r["named"],
               r["with_aid"], r["elapsed_ms"], "/".join(t[0][:8] for t in r["top_types"][:3])))
        rows.append({"type": "uia_scan", "exe": exe, "title": w["title"], "ok": True, **r})

    out_path = Path(__file__).parent / "data" / "uia_scan.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("\n记录追加写入", out_path)
