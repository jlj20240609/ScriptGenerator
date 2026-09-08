# -*- coding: utf-8 -*-
"""目标自愈：确保 5 类目标窗口存在且 target JSON 的 hwnd 有效；缺失则自动拉起/重捕获。
用法: python smoke/m0_ensure_targets.py [--names login,erp,dyn,calc,tk] [--recapture]
退出码: 0=全部就绪
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import m0lib
import m0_trial

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "smoke" / "fixtures"

EDGE = None
for p in (os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)") + "/Microsoft/Edge/Application/msedge.exe",
          os.environ.get("ProgramFiles", "C:/Program Files") + "/Microsoft/Edge/Application/msedge.exe"):
    if Path(p).exists():
        EDGE = p
        break
EDGE_PROFILE = os.path.join(os.environ.get("TEMP", "."), "m0_edge_profile")

SPECS = {
    "login": ("web-login", "M0 演示登录", "登录", "web-login.html"),
    "erp": ("erp-web", "M0 ERP 查询", "库存查询", "erp-web.html"),
    "dyn": ("dynamic", "M0 动态监控台", "服务状态", "dynamic-web.html"),
}


def ascii_copy(html_name):
    """Edge --app 需要 ASCII file:// URL：把 fixture 拷到 ASCII 临时目录"""
    tmp = Path(os.environ.get("TEMP", ".")) / "m0edge_ascii"
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / html_name.replace(".html", "").replace("_", "")
    # 同一文件多份拷贝会撞名，用原始名替换为 ascii 短名映射
    mapping = {"web-login.html": "login.html", "erp-web.html": "erp.html",
               "dynamic-web.html": "dynamic.html"}
    dst = tmp / mapping.get(html_name, html_name)
    import shutil
    shutil.copy(FIX / html_name, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="login,erp,dyn")
    args = ap.parse_args()
    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    ok = True
    for name in args.names.split(","):
        name = name.strip()
        if name not in SPECS:
            print("跳过未知目标", name)
            continue
        cls, title_sub, text, html = SPECS[name]
        wins = m0_trial.find_window_by_title(title_sub)
        if wins:
            hwnd = wins[0]
            if m0_trial.window_rect(hwnd)[2] - m0_trial.window_rect(hwnd)[0] > 50:
                print("%-6s 窗口已在 (hwnd=%d)" % (name, hwnd))
                continue
        # 拉起
        if not EDGE:
            print("%-6s 无 msedge，无法拉起" % name)
            ok = False
            continue
        url = ascii_copy(html).as_uri()
        subprocess.Popen([EDGE, "--user-data-dir=%s" % EDGE_PROFILE, "--no-first-run",
                          "--window-size=1020,780", "--app=%s" % url])
        time.sleep(6)
        wins = m0_trial.find_window_by_title(title_sub)
        if not wins:
            print("%-6s 拉起后仍未找到窗口" % name)
            ok = False
            continue
        hwnd = wins[0]
        time.sleep(2)
        ns = SimpleNamespace(cmd="capture", name=name, cls=cls, text=text,
                             title=title_sub, manual=False, dir=str(m0_trial.CAPTURE_DIR))
        try:
            m0_trial.do_capture(ns)
            print("%-6s 已重捕获 (hwnd=%d)" % (name, hwnd))
        except SystemExit as e:
            print("%-6s 重捕获失败: %s" % (name, e))
            ok = False
    print("全部就绪" if ok else "存在未就绪目标")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
