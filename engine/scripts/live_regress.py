# -*- coding: utf-8 -*-
"""
M1 波次2：真实窗口回归与校准演示（引擎真窗验收）

场景（对照《M1_引擎设计清单》§5/§6 回归资产）：
  login / erp / dyn  — Edge app 打开 fixtures（1020,780 → 外层 1288×988 @125% DPI，
                       与 migrate_m0_assets 产物口径一致）
  - 页面模板 0.8~1.25× 命中（dev ≤20px 对窗口真值；dyn 额外断言模板得分 ≥0.8）
  - 页内部件定位（OCR/模板）dev ≤20px；记录单步耗时（验收 §5.7 ≤500ms 真窗口径）
  - 页面内约束：部件定位结果必须在页面范围内
  erp_v2  — erp-v2.html 改版窗口上运行迁移的 erp 资产（旧“库存查询”已改名）
            → 点击闸拦下 → 自动校准（语义桩，无云端）→ 写回 → 复跑成功
            dev ≤20px；report.calib 含 first_run + updated；targets_rev 递增
  bili    — 需真实哔哩哔哩客户端（人工开窗），本脚本检测窗口在场才跑（默认跳过登记）

用法：
  python engine/scripts/live_regress.py --case login            # 单场景
  python engine/scripts/live_regress.py --all                  # 全部
退出码：0=全部断言通过；1=存在失败；2=前置条件不满足（无 Edge/窗口被占等）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, locator, matcher, schema  # noqa: E402

FIX = ROOT / "smoke" / "fixtures"
ASSETS = ROOT / "engine" / "tests" / "assets" / "live"
TMP = Path(os.environ.get("TEMP", ".")) / "m1_regress"
TMP.mkdir(parents=True, exist_ok=True)
EDGE_PROFILE = os.path.join(os.environ.get("TEMP", "."), "m1_edge_profile")
DATA_LOG = Path(__file__).resolve().parent / "live_regress.jsonl"

CASES = {
    # name: (fixture, title 子串, 资产名, 部件文字, 断言分组)
    "login": ("web-login.html", "M0 演示登录", "login", "登录", "page_widget"),
    "erp": ("erp-web.html", "M0 ERP 查询", "erp", "库存查询", "page_widget"),
    "dyn": ("dynamic-web.html", "M0 动态监控台", "dyn", "服务状态", "dyn"),
    "erp_v2": ("erp-v2.html", "M0 ERP 查询 · 进销存管理台 V2", "erp", "库存中心", "calib"),
    "dyn_feed": ("dynamic-feed.html", "M1 动态工作台 · 内容流", "dyn_feed", "热门", "feed"),
}


def find_edge():
    for cand in (Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
                 / "Microsoft/Edge/Application/msedge.exe",
                 Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                 / "Microsoft/Edge/Application/msedge.exe"):
        if cand.exists():
            return str(cand)
    return None


def copy_fixture(html_name):
    """把 fixture 复制到纯 ASCII 临时路径（Edge --app= 对非 ASCII 路径不稳）。

    每次都覆盖：曾因“只复制一次”的旧副本让回归跑到过期页面（2026-09-09 踩坑）。
    """
    ascii_name = html_name.replace("-", "_")
    dst = TMP / ascii_name
    dst.write_text((FIX / html_name).read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def close_windows(title_sub):
    for h in capture.find_windows_by_title(title_sub):
        try:
            import win32gui
            win32gui.PostMessage(h, 0x0010, 0, 0)   # WM_CLOSE
        except Exception:
            pass
        time.sleep(0.4)


def ensure_window(edge, title_sub, html_name, timeout_s=50.0, size=None):
    """拉起 fixture 窗口（无边框 app 模式，与 M0 采集口径一致）。"""
    ws = capture.find_windows_by_title(title_sub)
    if ws:
        return ws[0]
    dst = copy_fixture(html_name)
    w, h = size or (1020, 780)
    subprocess.Popen([edge, f"--user-data-dir={EDGE_PROFILE}", "--no-first-run",
                      "--no-default-browser-check", f"--window-size={w},{h}",
                      f"--app={dst.as_uri()}"])
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        ws = capture.find_windows_by_title(title_sub)
        if ws:
            time.sleep(3.0)           # 等渲染稳定（M0 经验）
            return ws[0]
        time.sleep(1.5)
    return None


class LiveHuman:
    """无人值守：任何人工兜底都按停止处理（失败时快速失败而不是挂起）。"""

    def notify(self, message):
        raise SystemExit(3)

    def prompt_not_found(self, message, target_text):
        return "stop"

    def prompt_outcome_fail(self, message):
        return "stop"


def run_feed_demo():
    """
    强动态页（bili feed 型，真窗 dynamic-feed.html）：
    现场“录制”帧 A（顶栏+内容流）→ 等主区变化 → 运行：整窗模板失配 →
    校验模式：语义定位顶栏词 → 行带锚跨帧复验写回 page.anchors →
    后续帧仅凭锚定位页面与部件（dev ≤20px，无人工）。
    """
    from engine import ai as ai_mod
    from engine.calibrator import Calibrator
    from engine.executor import LiveDriver, RunConfig, run_script
    from engine.logger import LocLogger

    _fixture, title, asset, text, _group = CASES["dyn_feed"]
    driver = LiveDriver({"title": title})
    hwnd = driver._resolve_hwnd()
    capture.bring_to_foreground(hwnd)
    time.sleep(2.2)
    wr = driver.window_rect()
    if wr is None:
        return False, {"note": "窗口矩形不可用"}
    screen = driver.grab_screen()[0]
    x0, y0, ww, wh = wr
    page_img = screen[y0:y0 + wh, x0:x0 + ww]
    # 现场录制：顶栏词“热门”
    f = matcher.find_text_ocr(page_img, text)
    if not f["ok"]:
        time.sleep(1.5)
        screen = driver.grab_screen()[0]
        page_img = screen[y0:y0 + wh, x0:x0 + ww]
        f = matcher.find_text_ocr(page_img, text)
    if not f["ok"]:
        return False, {"note": "顶栏词 OCR 采集失败"}
    bx = f["box"]
    PAD = 6
    cx0, cy0 = max(0, bx[0] - PAD), max(0, bx[1] - PAD)
    cx1 = min(ww, bx[0] + bx[2] + PAD)
    cy1 = min(wh, bx[1] + bx[3] + PAD)
    spec = schema.page_spec(
        image_dataurl=matcher.bgr_to_dataurl(page_img),
        size=(ww, wh), context={"process": "msedge.exe", "title": title,
                                "class": "Chrome_WidgetWin_1"},
        rect_in_screen=list(wr),
        capture_meta={"dpi": 120, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "cap_method": "screen", "visible": True})
    widget = schema.widget_target(
        page=spec, image_dataurl=matcher.bgr_to_dataurl(
            page_img[cy0:cy1, cx0:cx1]),
        text=text, semantic=f"内容流顶栏导航“{text}”",
        rect_in_page=[cx0, cy0, cx1 - cx0, cy1 - cy0],
        center_in_page=[(cx0 + cx1) // 2, (cy0 + cy1) // 2])
    sg = schema.new_script(name=f"M1 回归 · dyn_feed（现场录制）")
    sg["steps"] = [{"id": "f1", "type": "action", "action": "click",
                    "target": widget, "params": {}}]
    time.sleep(2.6)                       # 主区多帧变化（整窗模板应失配）
    loc_log = LocLogger(TMP / "dyn_feed_loc.jsonl")
    cal = Calibrator(driver, ai=ai_mod.SemanticStub(),
                     window_rect=driver.window_rect, anchor_dt_s=1.8)
    cfg = RunConfig(l1_retries=1, l1_retry_interval_s=0.6,
                    l2_poll_interval_s=0.4, l2_timeout_s=2.0)
    cfg.calibrate_first_run = True
    rep = run_script(sg, driver, cfg=cfg, loc_logger=loc_log, human=LiveHuman(),
                     calibrator=cal)
    calib = rep.get("calib", [])
    updated = [c for c in calib if c.get("updated")]
    reasons = [c.get("reason") for c in calib]
    anchors = spec.get("anchors") or []
    print(f"[dyn_feed] status={rep.get('status')} calib={reasons} "
          f"updated={len(updated)} anchors={len(anchors)}")
    print(f"[dyn_feed] calib 详情: "
          f"{[{'reason': c.get('reason'), 'ok': c.get('ok'), 'updated': c.get('updated'),
               'note': (c.get('note') or '')[:80]} for c in calib]}")
    ok = rep.get("status") == "ok" and updated and anchors
    dev = None
    if ok:
        # 下一帧验证：内容再变后页面可定位（整窗若命中更佳；动态页则走锚）
        time.sleep(1.5)
        screen = driver.grab_screen()[0]
        r2 = locator.locate_page(screen, spec)
        ok2 = bool(r2["ok"])
        w2 = None
        if ok2:
            pr = r2["rect"]
            w2 = locator.locate_widget_on_screen(screen, pr, widget)
            ok2 = bool(w2 and w2["ok"])
            if ok2:
                page_img2 = screen[pr[1]:pr[1] + pr[3], pr[0]:pr[0] + pr[2]]
                f2 = matcher.find_text_ocr(page_img2, text)
                if f2["ok"]:
                    dev = max(abs(w2["center"][0] - (pr[0] + f2["center"][0])),
                              abs(w2["center"][1] - (pr[1] + f2["center"][1])))
                else:
                    ci = widget["center_in_page"]
                    dev = max(abs(w2["center"][0] - (wr[0] + ci[0])),
                              abs(w2["center"][1] - (wr[1] + ci[1])))
        print(f"[dyn_feed] 复验定位: {'OK' if ok2 else 'FAIL'} "
              f"method={r2.get('method')} dev={dev}px"
              + ("（≤20 OK）" if dev is not None and dev <= 20 else ""))
        ok = ok and ok2 and (dev is None or dev <= 20)
    _write_rows([{"case": "dyn_feed", "ts": time.time(), "ok": ok,
                  "calib": reasons, "anchors": len(anchors), "dev_px": dev}])
    return ok, {"dev": dev, "anchors": len(anchors), "calib": reasons}


def run_bili_demo():
    """
    真实哔哩哔哩客户端动态页复核（验收②/④的 bili 场景）：
    旧 feed 整窗模板（bili_page.png，1725×1075）对当前动态 feed 必然失配 →
    校验模式：语义定位顶栏搜索框（placeholder“搜索你感兴趣的视频”恒定）→
    行带锚跨帧复验写回 page.anchors + 整窗刷新 → 复跑定位 dev ≤20px。
    动作=点击搜索框（仅聚焦，无导航副作用）；不关闭用户窗口。
    """
    from engine import ai as ai_mod
    from engine.calibrator import Calibrator
    from engine.executor import LiveDriver, RunConfig, run_script
    from engine.logger import LocLogger

    title = "哔哩哔哩"
    wins = capture.find_windows_by_title(title)
    if not wins:
        return False, {"note": "哔哩哔哩窗口不在场"}
    old = json.loads((ROOT / "smoke" / "targets" / "bili.json")
                     .read_text(encoding="utf-8"))
    old_page = matcher.load_png(ROOT / "smoke" / "data" / "target_img"
                                / old["page"]["file"])
    old_widget = matcher.load_png(ROOT / "smoke" / "data" / "target_img"
                                  / old["widget"]["file"])
    spec = schema.page_spec(
        image_dataurl=matcher.bgr_to_dataurl(old_page),
        size=(old_page.shape[1], old_page.shape[0]),
        context={"process": "bilibili.exe", "title": title,
                 "class": old["page"].get("window_class", "")},
        rect_in_screen=old["page"]["rect"],
        capture_meta={"dpi": old.get("display", {}).get("dpi", 120),
                      "ts": old.get("captured_at", ""),
                      "cap_method": "screen", "visible": True})
    w = old["widget"]
    full_text = (old.get("uia_name") or "").strip() or "搜索你感兴趣的视频"
    widget = schema.widget_target(
        page=spec,
        image_dataurl=matcher.bgr_to_dataurl(old_widget),
        text=full_text,
        semantic="哔哩哔哩顶栏搜索框（搜索你感兴趣的视频）",
        rect_in_page=w["rect_in_page"],
        center_in_page=w["center_in_page"],
        uia={"name": full_text})
    sg = schema.new_script(name="M1 回归 · bili（真实客户端动态页）")
    sg["steps"] = [{"id": "b1", "type": "action", "action": "click",
                    "target": widget, "params": {}}]
    driver = LiveDriver({"title": title})
    hwnd = driver._resolve_hwnd()
    capture.bring_to_foreground(hwnd)
    time.sleep(2.5)
    loc_log = LocLogger(TMP / "bili_loc.jsonl")
    cal = Calibrator(driver, ai=ai_mod.SemanticStub(),
                     window_rect=driver.window_rect, anchor_dt_s=2.0)
    cfg = RunConfig(l1_retries=1, l1_retry_interval_s=0.6,
                    l2_poll_interval_s=0.4, l2_timeout_s=2.0)
    cfg.calibrate_first_run = True
    rep = run_script(sg, driver, cfg=cfg, loc_logger=loc_log, human=LiveHuman(),
                     calibrator=cal)
    calib = rep.get("calib", [])
    updated = [c for c in calib if c.get("updated")]
    reasons = [c.get("reason") for c in calib]
    anchors = spec.get("anchors") or []
    print(f"[bili] status={rep.get('status')} calib={reasons} "
          f"updated={len(updated)} anchors={len(anchors)}")
    print(f"[bili] calib 详情: "
          f"{[{'reason': c.get('reason'), 'ok': c.get('ok'), 'updated': c.get('updated'),
               'note': (c.get('note') or '')[:90]} for c in calib]}")
    ok = rep.get("status") == "ok" and updated
    dev = None
    method = None
    if ok:
        # 等 feed 变化/稳定后复验定位
        time.sleep(2.5)
        screen = driver.grab_screen()[0]
        r2 = locator.locate_page(screen, spec)
        ok2 = bool(r2["ok"])
        method = r2.get("method") if ok2 else None
        if ok2:
            pr = r2["rect"]
            w2 = locator.locate_widget_on_screen(screen, pr, widget)
            ok2 = bool(w2 and w2["ok"])
            if ok2:
                page_img2 = screen[pr[1]:pr[1] + pr[3], pr[0]:pr[0] + pr[2]]
                f2 = matcher.find_text_ocr(page_img2, full_text)
                if f2["ok"]:
                    dev = max(abs(w2["center"][0] - (pr[0] + f2["center"][0])),
                              abs(w2["center"][1] - (pr[1] + f2["center"][1])))
                else:
                    wr = driver.window_rect()
                    ci = widget["center_in_page"]
                    if wr and ci:
                        dev = max(abs(w2["center"][0] - (wr[0] + ci[0])),
                                  abs(w2["center"][1] - (wr[1] + ci[1])))
        print(f"[bili] 复验定位: {'OK' if ok2 else 'FAIL'} method={method} "
              f"dev={dev}px" + ("（≤20 OK）" if dev is not None and dev <= 20
                                else ""))
        ok = ok and ok2 and (dev is None or dev <= 20)
    _write_rows([{"case": "bili_live", "ts": time.time(), "ok": ok,
                  "calib": reasons, "anchors": len(anchors),
                  "method": method, "dev_px": dev}])
    return ok, {"dev": dev, "anchors": len(anchors), "calib": reasons}


def _rows_from_log(path):
    rows = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def locate_case(case, hwnd, rounds=4, seed=11):
    """移窗/定位回归：页面模板 + 部件，dev 对窗口真值；逐轮记 JSONL。"""
    fixture, title, asset, text, _group = case
    sg = schema.load(ASSETS / f"{asset}.sgscript.json")
    spec = sg["steps"][0]["target"]["page"]
    target = sg["steps"][0]["target"]
    rng = random.Random(seed)
    rows = []
    all_ok = True
    import mss
    with mss.mss() as s:
        mm = s.monitors[0]
    mw, mh = mm["width"], mm["height"]
    # 预热轮：吸收前台激活/合成首帧（不记录）
    capture.bring_to_foreground(hwnd)
    time.sleep(2.0)
    locator.locate_page(capture.grab_screen(), spec)
    for rnd in range(rounds):
        capture.bring_to_foreground(hwnd)
        time.sleep(2.0)                  # Edge 前台激活/重绘稳定（0.8s 实测不足）
        rect0 = capture.window_rect(hwnd)
        if rnd > 0:
            maxx = max(60, mw - (rect0[2] - rect0[0]) - 40)
            maxy = max(60, mh - (rect0[3] - rect0[1]) - 40)
            if maxx > 60 and maxy > 60:
                import win32con
                import win32gui
                nx, ny = rng.randint(40, maxx), rng.randint(40, maxy)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, nx, ny, 0, 0,
                                      win32con.SWP_NOSIZE)
                time.sleep(1.6)          # Edge 合成/重绘稳定（M0 0.9s 经验不够稳）
        # 一轮内至多 3 次“取真值→定位”，吸收渲染时序抖动（M0 do_run 同款重试）
        round_ok = False
        fail_row = None
        for _try in range(3):
            truth = capture.window_rect(hwnd)
            screen = capture.grab_screen()
            t0 = time.perf_counter()
            r = locator.locate_page(screen, spec)
            page_ms = (time.perf_counter() - t0) * 1000
            row = {"case": asset, "round": rnd, "try": _try, "ts": time.time(),
                   "truth_rect": list(truth)}
            if not r["ok"]:
                fail_row = dict(row, ok=False, stage="page",
                                score=r.get("detail", {}).get("page_tpl", {}).get("score"))
                time.sleep(1.0)
                continue
            dev = max(abs(r["rect"][0] - truth[0]), abs(r["rect"][1] - truth[1]))
            if dev > 20:
                fail_row = dict(row, ok=False, stage="page_drift",
                                page_score=r.get("confidence"),
                                page_dev_px=round(dev, 1))
                time.sleep(1.0)
                continue
            row.update({"page_ok": True, "page_method": r["method"],
                        "page_score": r.get("confidence"),
                        "page_sim": r.get("sim"),
                        "page_dev_px": round(dev, 1), "page_ms": round(page_ms, 1)})
            pr = r["rect"]
            t0 = time.perf_counter()
            w = locator.locate_widget_on_screen(screen, pr, target,
                                                page_scale=r.get("scale", 1.0))
            w_ms = (time.perf_counter() - t0) * 1000
            row["widget_ms"] = round(w_ms, 1)
            if not w["ok"]:
                fail_row = dict(row, widget_ok=False, ok=False, stage="widget")
                time.sleep(1.0)
                continue
            in_page = (w["box"][0] >= pr[0] - 1 and w["box"][1] >= pr[1] - 1
                       and w["box"][0] + w["box"][2] <= pr[0] + pr[2] + 1
                       and w["box"][1] + w["box"][3] <= pr[1] + pr[3] + 1)
            # dev 真值优先用资产录制坐标（页面未改版时恒定，M0 口径 dev 0–1px）
            ci = target.get("center_in_page")
            s_ = r.get("scale", 1.0)
            truth_c = (truth[0] + int(ci[0] * s_), truth[1] + int(ci[1] * s_)) \
                if ci else None
            wdev = None
            if truth_c:
                wdev = max(abs(w["center"][0] - truth_c[0]),
                           abs(w["center"][1] - truth_c[1]))
            # 模板路径耗时（≤500ms 验收口径：页面锁定后部件模板单档）
            tpl_ms = None
            if target.get("image"):
                w_tpl = matcher.dataurl_to_bgr(target["image"])
                page_img0 = screen[pr[1]:pr[1] + pr[3], pr[0]:pr[0] + pr[2]]
                t0 = time.perf_counter()
                matcher.find_template(page_img0, w_tpl, scales=(round(s_, 4),))
                tpl_ms = (time.perf_counter() - t0) * 1000
            widget_ok = bool(in_page and (wdev is None or wdev <= 20))
            row.update({"widget_ok": widget_ok, "widget_level": w.get("level"),
                        "widget_method": w.get("method"),
                        "widget_dev_px": wdev,
                        "widget_tpl_ms": (None if tpl_ms is None
                                          else round(tpl_ms, 1)),
                        "in_page": in_page, "ok": widget_ok, "stage": "widget"})
            if widget_ok:
                round_ok = True
                rows.append(row)
                break
            fail_row = row
            time.sleep(1.0)
        if not round_ok:
            rows.append(fail_row or {"case": asset, "round": rnd, "ok": False,
                                     "stage": "unknown"})
            all_ok = False
    return all_ok, rows


def run_calib_demo():
    """erp 资产 在 erp-v2 窗口上运行：点击闸失败 → 自动校准 → 写回 → 复跑。"""
    from engine import ai as ai_mod
    from engine.calibrator import Calibrator
    from engine.executor import LiveDriver, RunConfig, run_script
    from engine.logger import LocLogger
    from engine import executor as X

    _fixture, title, asset, _text, _group = CASES["erp_v2"]
    sg = schema.load(ASSETS / f"{asset}.sgscript.json")
    driver = LiveDriver({"title": title})
    loc_log = LocLogger(TMP / "erp_v2_loc.jsonl")
    cfg = RunConfig(l1_retries=1, l1_retry_interval_s=0.5,
                    l2_poll_interval_s=0.4, l2_timeout_s=2.0)
    cal = Calibrator(driver, ai=ai_mod.SemanticStub(),   # 语义桩：短前缀可恢复改名
                     window_rect=driver.window_rect)
    cfg.calibrate_first_run = True
    human = LiveHuman()
    rep = run_script(sg, driver, cfg=cfg, loc_logger=loc_log, human=human,
                     calibrator=cal)
    return sg, rep, driver, loc_log


def _write_rows(rows):
    with open(DATA_LOG, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="", help="login/erp/dyn/erp_v2 之一")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--keep-windows", action="store_true",
                    help="结束后保留 fixture 窗口（默认关闭自己拉起的窗口）")
    ap.add_argument("--open-only", action="store_true",
                    help="只把 fixture 窗口摆好（给真人演示/联调用），不跑回归")
    ap.add_argument("--size", default="", help="--open-only 时的窗口尺寸 CSS 像素 WxH")
    args = ap.parse_args()

    capture.init_dpi_aware()
    edge = find_edge()
    if not edge:
        print("前置不满足：未找到 msedge.exe")
        return 2
    names = [c.strip() for c in args.case.split(",") if c.strip()]
    if args.all:
        names = list(CASES)
    if not names:
        names = ["login"]

    if args.open_only:
        # 只摆窗口：真人演示/联调前的环境准备（不动鼠标、不跑脚本）
        for name in names:
            if name not in CASES:
                print(f"[{name}] 不是 fixture 用例，跳过")
                continue
            html, title, _asset, _text, _kind = CASES[name]
            size = None
            if args.size and "x" in args.size.lower():
                w, h = args.size.lower().split("x", 1)
                size = (int(w), int(h))
            hwnd = ensure_window(edge, title, html, size=size)
            if not hwnd:
                print(f"[{name}] 窗口拉起失败")
                return 2
            l, t, r, b = capture.window_rect(hwnd)
            print(f"[{name}] 已就绪：{title}  物理 ({l},{t}) {r - l}×{b - t}")
        print("窗口已摆好（--open-only，不跑回归）")
        return 0

    overall = True
    for name in names:
        # 每 case 只保留自己的窗口（多窗共存时 Edge 合成/前台切换会引入定位噪声）；
        # bili 为真实客户端场景（用户窗口，不独占清理）
        if name in CASES:
            for _f, _t, *_rest in CASES.values():
                if _t != CASES[name][1]:
                    close_windows(_t)
            time.sleep(1.2)
        if name == "erp_v2":
            close_windows("M0 ERP 查询")            # 关闭 v1 防干扰
            time.sleep(0.8)
            hwnd = ensure_window(edge, CASES["erp_v2"][1], CASES["erp_v2"][0])
            if not hwnd:
                print("[erp_v2] 窗口拉起失败")
                overall = False
                continue
            sg, rep, driver, loc_log = run_calib_demo()
            # 写回落盘验证：校准后的脚本 JSON 可保存/加载/校验（验收 §5.8）
            out_path = TMP / "erp_calibrated.sgscript.json"
            schema.dump(sg, out_path)
            sg_re = schema.load(out_path)
            save_ok = not schema.validate(sg_re)
            print(f"[erp_v2] 写回脚本落盘 {out_path.name}: "
                  f"{'OK' if save_ok else 'FAIL'}")
            calib = rep.get("calib", [])
            updated = [c for c in calib if c.get("updated")]
            reasons = [c.get("reason") for c in calib]
            ok_step = rep.get("status") == "ok"
            text_now = sg["steps"][0]["target"].get("text")
            text_fixed = text_now and text_now != "库存查询"
            print(f"[erp_v2] status={rep.get('status')} calib={reasons} "
                  f"updated={len(updated)} text={text_now!r} "
                  f"targets_rev={sg.get('targets_rev')}")
            print(f"[erp_v2] calib 详情: "
                  f"{[{'reason': c.get('reason'), 'ok': c.get('ok'), 'updated': c.get('updated'), 'note': (c.get('note') or '')[:60]} for c in calib]}")
            ok = ok_step and updated and text_fixed and save_ok
            dev = None
            if ok:
                # 复跑验证：写回后 target 在新窗口上定位，dev 对 OCR 真值
                screen = driver.grab_screen()[0]
                tgt = sg["steps"][0]["target"]
                r2 = locator.locate_page(screen, tgt["page"])
                ok2 = bool(r2["ok"])
                if ok2:
                    pr = r2["rect"]
                    w2 = locator.locate_widget_on_screen(
                        screen, pr, tgt, page_scale=r2.get("scale", 1.0))
                    ok2 = bool(w2["ok"])
                    if ok2:
                        # dev 真值：优先 OCR 现读；miss 回退校准写回的页内中心（稳定口径）
                        page_img = screen[pr[1]:pr[1] + pr[3], pr[0]:pr[0] + pr[2]]
                        f = matcher.find_text_ocr(page_img, "库存中心")
                        if f["ok"]:
                            dev = max(abs(w2["center"][0] - (pr[0] + f["center"][0])),
                                      abs(w2["center"][1] - (pr[1] + f["center"][1])))
                        else:
                            ci = tgt.get("center_in_page")
                            wr = capture.window_rect(driver._resolve_hwnd())
                            if ci and wr:
                                dev = max(abs(w2["center"][0] - (wr[0] + ci[0])),
                                          abs(w2["center"][1] - (wr[1] + ci[1])))
                print(f"[erp_v2] 复跑页面定位: {'OK' if ok2 else 'FAIL'}"
                      f" dev={dev}px" + ("（≤20 OK）" if dev is not None and dev <= 20
                                         else ""))
                ok = ok and ok2 and (dev is None or dev <= 20)
            overall = overall and ok
            continue
        if name in CASES and CASES[name][4] == "feed":
            hwnd = ensure_window(edge, CASES[name][1], CASES[name][0])
            if not hwnd:
                print("[dyn_feed] 窗口拉起失败")
                overall = False
                continue
            ok, _info = run_feed_demo()
            overall = overall and ok
            continue
        if name == "bili":                    # 真实 bili 客户端（不拉窗、不关窗）
            ok, _info = run_bili_demo()
            overall = overall and ok
            continue
        # 常规场景
        fixture, title, asset, text, group = CASES[name]
        hwnd = ensure_window(edge, title, fixture)
        if not hwnd:
            print(f"[{name}] 窗口拉起失败")
            overall = False
            continue
        ok, rows = locate_case(CASES[name], hwnd, rounds=args.rounds)
        _write_rows(rows)
        ms = [r for r in rows if r.get("widget_ms") is not None]
        wmed = sorted(r["widget_ms"] for r in ms)[len(ms) // 2] if ms else None
        tpls = [r.get("widget_tpl_ms") for r in rows
                if r.get("widget_tpl_ms") is not None]
        tpl_med = sorted(tpls)[len(tpls) // 2] if tpls else None
        scores = [r.get("page_score") for r in rows if r.get("page_score")]
        ok_rows = [r for r in rows if r.get("ok")]
        print(f"[{name}] {len(ok_rows)}/{len(rows)} 轮通过 | "
              f"页面模板分 {[round(s, 3) for s in scores][:6]} | "
              f"部件 dev {[r.get('widget_dev_px') for r in ok_rows]} | "
              f"部件定位中位 {wmed:.0f}ms | 部件模板中位 {tpl_med:.0f}ms")
        if tpl_med is not None:
            tpl_ok = tpl_med <= 500
            print(f"[{name}] 模板路径单步耗时中位 {tpl_med:.0f}ms"
                  f"（≤500ms {'OK' if tpl_ok else 'FAIL'}）")
            ok = ok and tpl_ok
        if group == "dyn" and scores:
            dyn_ok = min(scores) >= 0.8
            print(f"[{name}] 心跳页模板最低分 {min(scores):.3f} "
                  f"({'≥0.8 OK' if dyn_ok else 'FAIL'})")
            ok = ok and dyn_ok
        if not ok:
            for r in rows:
                if not r.get("ok"):
                    print("  失败轮:", {k: r[k] for k in
                                        ("round", "stage", "page_dev_px",
                                         "widget_dev_px", "in_page",
                                         "page_score") if k in r})
        overall = overall and ok
    if not args.keep_windows:
        for _fixture, title, *_rest in CASES.values():
            close_windows(title)
        time.sleep(0.6)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
