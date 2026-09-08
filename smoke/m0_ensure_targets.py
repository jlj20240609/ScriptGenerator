# -*- coding: utf-8 -*-
"""目标自愈 v2：确保 web 目标窗口存在、与 target JSON 的 hwnd 一致、内容可被捕获。
缺失 → 拉起；句柄变更/内容不对 → 轮询重试捕获（最多 3 次）。
用法: python smoke/m0_ensure_targets.py [--names login,erp,dyn]
退出码: 0=全部就绪
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import m0lib
import m0_trial

FIX = Path(__file__).resolve().parent / "fixtures"
EDGE = None
for cand in (Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
             Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"):
    if cand.exists():
        EDGE = str(cand)
        break
EDGE_PROFILE = os.path.join(os.environ.get("TEMP", "."), "m0_edge_profile")

SPECS = {
    "login": ("web-login", "M0 演示登录", "登录", "web-login.html"),
    "erp": ("erp-web", "M0 ERP 查询", "库存查询", "erp-web.html"),
    "dyn": ("dynamic", "M0 动态监控台", "服务状态", "dynamic-web.html"),
}


def launch_app(html_name):
    mapping = {"web-login.html": "login.html", "erp-web.html": "erp.html",
               "dynamic-web.html": "dynamic.html"}
    tmp = Path(os.environ.get("TEMP", ".")) / "m0edge_ascii"
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / mapping.get(html_name, html_name)
    shutil.copy(FIX / html_name, dst)
    subprocess.Popen([EDGE, "--user-data-dir=%s" % EDGE_PROFILE, "--no-first-run",
                      "--no-default-browser-check", "--window-size=1020,780",
                      "--app=%s" % dst.as_uri()])


def capture_one(name):
    cls, title_sub, text, html = SPECS[name]
    ns = SimpleNamespace(cmd="capture", name=name, cls=cls, text=text,
                         title=title_sub, manual=False, dir=str(m0_trial.CAPTURE_DIR))
    m0_trial.do_capture(ns)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="login,erp,dyn")
    ap.add_argument("--max-wait", type=float, default=45.0)
    args = ap.parse_args()
    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    ok = True
    for name in args.names.split(","):
        name = name.strip()
        if name not in SPECS:
            continue
        _, title_sub, _, html = SPECS[name]
        jpath = m0_trial.CAPTURE_DIR / ("%s.json" % name)
        json_hwnd = 0
        if jpath.exists():
            json_hwnd = int(m0lib.Target.load(jpath).page.get("hwnd") or 0)
        # 1) 窗口缺失则拉起
        wins = m0_trial.find_window_by_title(title_sub)
        if not wins:
            if not EDGE:
                print("%-6s 无 msedge，无法拉起" % name)
                ok = False
                continue
            launch_app(html)
            t0 = time.time()
            while time.time() - t0 < args.max_wait:
                wins = m0_trial.find_window_by_title(title_sub)
                if wins:
                    break
                time.sleep(2)
        if not wins:
            print("%-6s 拉起超时仍未找到窗口" % name)
            ok = False
            continue
        hwnd = wins[0]
        # 2) 句柄与 json 一致且窗口存在 → 就绪；否则重捕获（含轮询重试）
        if hwnd == json_hwnd and m0_trial.window_rect(hwnd)[2] - m0_trial.window_rect(hwnd)[0] > 50:
            print("%-6s 就绪 (hwnd=%d)" % (name, hwnd))
            continue
        done = False
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(6)  # 等 Edge 冷启动渲染完成
                capture_one(name)
                done = True
                break
            except (SystemExit, RuntimeError) as e:
                print("%-6s 捕获尝试 %d 失败: %s" % (name, attempt + 1, e))
                time.sleep(4)
        if not done:
            ok = False
    print("全部就绪" if ok else "存在未就绪目标")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
