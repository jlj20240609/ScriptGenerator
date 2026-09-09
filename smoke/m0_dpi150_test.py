# -*- coding: utf-8 -*-
"""
M0④ 150% DPI 复测工具：临时切换系统缩放 125%→150% → 跑 Electron AUTOTEST → 恢复 125%。
用法: python smoke/m0_dpi150_test.py
注意: 会短暂改变全系统缩放（约 20-40 秒），期间桌面图标/窗口会变大；
      结束后自动恢复 125%。需在用户允许时执行。
"""
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import winreg

ROOT = Path(__file__).resolve().parent
ELECTRON_DIR = ROOT / "electron_proto"


def current_logpixels():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Desktop\Winlogon", 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, "LogPixels")[0]
    except FileNotFoundError:
        return None  # 未显式设置=跟随系统 96 基准（100% 由注册表 DPI 描述）

def set_logpixels(v):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Control Panel\Desktop\Winlogon") as k:
        winreg.SetValueEx(k, "LogPixels", 0, winreg.REG_DWORD, v)
    # 广播设置变更让资源管理器/应用生效
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 0, 0x0002, 5000, None)
    # 资源管理器需要重启才能整体生效（简化：仅对新启动进程生效即足够——引擎/Electron 每次新建进程）
    time.sleep(2)


def main():
    if "--restore-only" in sys.argv:
        if current_logpixels() is not None:
            set_logpixels(120)
        print("已恢复 125%（LogPixels=120）")
        return
    old = current_logpixels()
    print("当前 LogPixels:", old)
    print("切换到 150%（LogPixels=144）…")
    set_logpixels(144)
    os.environ["M0_AUTOTEST"] = "1"
    electron_exe = ELECTRON_DIR / "node_modules" / "electron" / "dist" / "electron.exe"
    try:
        r = subprocess.run([str(electron_exe), "."], cwd=str(ELECTRON_DIR),
                           capture_output=True, text=True, timeout=90)
        print("electron returncode:", r.returncode)
        tail = (r.stdout + r.stderr).splitlines()
        for line in tail:
            if "AUTOTEST" in line or "overlay ready" in line:
                print(line)
        if not any("AUTOTEST" in x for x in tail):
            print("--- electron 输出尾部（诊断）---")
            print("\n".join(tail[-15:]))
    except subprocess.TimeoutExpired:
        print("electron 超时 90s")
    finally:
        print("恢复 125%（LogPixels=120）…")
        set_logpixels(120)
        print("完成。")


if __name__ == "__main__":
    main()
