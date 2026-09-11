# -*- coding: utf-8 -*-
"""M2-WP8 跨显示缩放的**真机**验证（不改系统设置）。

思路：用 Edge 的 `--force-device-scale-factor` 让页面按不同缩放渲染（等价于"换到另一档 DPI
时页面图像按比例放大"），再用**同一份 125% 下录制的脚本资产**去定位，看：
  · 整窗页面定位是否命中（跨 DPI 时应由"当前 DPI ÷ 录制 DPI"预判尺度）
  · 页面原点误差多少
  · 部件定位与点击安全闸是否仍通过

两个场景：
  ① 基线：渲染 1.25（与系统 125% 一致）→ 应与平时完全一样；
  ② 跨 DPI：渲染 1.5（页面比录制时大 1.2 倍），并声明"录制于 96 DPI"
     → 触发尺度预判（120/96 = 1.25，覆盖真实比值 1.2）。

跑法：python smoke/diag/verify_scale_live.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, locator, matcher, schema  # noqa: E402
from engine.scripts import bench as B  # noqa: E402
from engine.scripts.live_regress import copy_fixture, find_edge  # noqa: E402

capture.init_dpi_aware()
edge = find_edge()
if not edge:
    print("没找到 msedge.exe")
    sys.exit(1)
TITLE = "M0 演示登录"
FIXTURE = "web-login.html"


def close_all():
    import win32gui
    for h in capture.find_windows_by_title(TITLE):
        try:
            win32gui.PostMessage(h, 0x0010, 0, 0)
        except Exception:
            pass
    time.sleep(1.2)


def open_scaled(factor):
    dst = copy_fixture(FIXTURE)
    subprocess.Popen([edge, f"--user-data-dir={Path(copy_fixture(FIXTURE)).parent / 'scale_profile'}",
                      "--no-first-run", "--no-default-browser-check",
                      f"--force-device-scale-factor={factor}",
                      "--window-size=1020,780", f"--app={dst.as_uri()}"])
    t0 = time.time()
    while time.time() - t0 < 40:
        ws = capture.find_windows_by_title(TITLE)
        if ws:
            time.sleep(3.0)
            return ws[0]
        time.sleep(1.5)
    return 0


def run_case(tag, factor, spec_dpi_override=None):
    close_all()
    hwnd = open_scaled(factor)
    if not hwnd:
        print(f"[{tag}] 窗口没起来 ✗")
        return None
    capture.bring_to_foreground(hwnd)
    time.sleep(1.0)
    l, t, r, b = capture.window_rect(hwnd)
    page_rect = (l, t, r - l, b - t)
    print(f"\n[{tag}] 渲染 {factor}×  窗口 {page_rect[2]}x{page_rect[3]}  "
          f"系统 DPI={capture.dpi_of()}")
    sg = schema.load(B.ASSETS / "login.sgscript.json")
    tgt = sg["steps"][0]["target"]
    spec = tgt["page"]
    rec_dpi = (spec.get("capture_meta") or {}).get("dpi")
    if spec_dpi_override:
        spec["capture_meta"] = dict(spec.get("capture_meta") or {}, dpi=spec_dpi_override)
    print(f"[{tag}] 资产录制 DPI={rec_dpi}"
          + (f" → 测试里改写为 {spec_dpi_override}" if spec_dpi_override else ""))
    screen = capture.grab_screen()
    pg = locator.locate_page(screen, spec)
    det = (pg.get("detail") or {})
    print(f"[{tag}] 页面定位 ok={pg['ok']} method={pg.get('method')} "
          f"rect={pg.get('rect')}")
    print(f"[{tag}]   page_tpl={det.get('page_tpl')}")
    print(f"[{tag}]   page_scales={det.get('page_scales')}")
    print(f"[{tag}]   page_feature={det.get('page_feature')}")
    dev = None
    if pg["ok"]:
        dev = max(abs(pg["rect"][0] - page_rect[0]), abs(pg["rect"][1] - page_rect[1]))
        print(f"[{tag}]   页面原点误差 = {dev}px")
    if pg["ok"]:
        lw = locator.locate_widget_on_screen(screen, pg["rect"], tgt)
        print(f"[{tag}] 部件定位 ok={lw['ok']} method={lw.get('method')} "
              f"level={lw.get('level')} box={lw.get('box')}")
    close_all()
    return {"dev": dev, "ok": pg["ok"], "method": pg.get("method")}


print("=" * 60)
base = run_case("基线", 1.25)
cross = run_case("跨DPI", 1.5, spec_dpi_override=96)
print("\n" + "=" * 60)
print("结论：")
for tag, r in (("基线(1.25×)", base), ("跨DPI(1.5× 且声明录制于 96DPI)", cross)):
    if r is None:
        print(f"  {tag}: 未跑成")
    else:
        print(f"  {tag}: ok={r['ok']} method={r['method']} 原点误差={r['dev']}px")
