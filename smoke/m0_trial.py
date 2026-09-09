# -*- coding: utf-8 -*-
"""
M0① 双截图闭环 CLI（最小闭环：双截图 → 三级部件定位 → SendInput 点击 → 校验）

约定（本 Spike 的页面定义）：页面 = 目标窗口的整窗矩形（含标题栏/边框），
像素一律物理像素、DPI-aware。真值 = 我们移动窗口时已知的窗口矩形 → 内容整体平移，
因此「定位点 vs 真值偏差」可精确量化，不需要外部 oracle。

子命令：
  capture   把目标窗口存为目标：页面=整窗双截图；部件=手动框选(3次Enter)或
            自动(--text 指定部件文字，OCR 在页面图内查找)。产物 JSON+PNG。
  run       对 targets 目录里的目标循环定位+点击：随机移动窗口 → 页面模板定位 →
            部件三级定位(①UIA树 ②OCR文本/模板相似度 ③页面内坐标) → 点击 →
            校验(点击点仍含文字 + 与真值偏差)。逐次写 data/trials.jsonl。
  selftest  合成图验证定位器（无需真实桌面）。

用法示例：
  python smoke/m0_trial.py capture --name login --cls web-login --title 'M0 演示登录' --text 登录
  python smoke/m0_trial.py run --dir smoke/targets --count 20 --move
"""
import argparse
import ctypes
import random
import time
from pathlib import Path

import cv2
import numpy as np

import m0lib

PAGE_SCORE_MIN = 0.72
TPL_SCORE_MIN = 0.70
TEXT_SIM_MIN = 0.75
DEV_OK_PX = 12          # 定位点与真值（窗口位移已知）允许偏差
PAD = 12                # 部件可点击区 = 文字框外扩 PAD
CAPTURE_DIR = Path(__file__).parent / "targets"
IMG_DIR = Path(__file__).parent / "data" / "target_img"


# ---------------- 窗口几何 ----------------

def window_rect(hwnd):
    import win32gui
    return win32gui.GetWindowRect(hwnd)


def page_of_window(hwnd, min_std=14.0, sim_min=0.40):
    """
    抓目标窗口的页面：置顶置前后抓屏，并与 PrintWindow（遮挡无关的窗口真值）
    做像素同源校验；校验不过重试一次；仍不过则退回 PrintWindow 内容并标记不可见。
    返回 (rect, bgr, method, visible)。rect=整窗物理矩形。
    """
    import win32gui
    try:
        m0lib.bring_to_foreground(hwnd)  # 含最小化还原 + TOPMOST
        time.sleep(0.55)  # 等重绘
        rect = window_rect(hwnd)
        if rect[2] - rect[0] > 0 and rect[3] - rect[1] > 0:
            bgr = m0lib.grab_screen(rect)
            ref = m0lib.grab_window_content(hwnd)
            if ref is not None and ref.std() >= min_std:
                sim = m0lib.pixel_sim(bgr, ref)
                if sim < sim_min:  # 屏幕被遮挡：再试一次置前
                    m0lib.bring_to_foreground(hwnd)
                    time.sleep(0.6)
                    rect = window_rect(hwnd)
                    bgr = m0lib.grab_screen(rect)
                    sim = m0lib.pixel_sim(bgr, ref)
                if sim < sim_min:
                    # 真的不可见 → 用 PrintWindow 内容兜底（页面数据仍可用，标记不可见）
                    if bgr.std() >= min_std:
                        return (rect, ref, "printwindow", False)
                    return (rect, ref, "printwindow", False)
            return (rect, bgr, "screen", True)
        alt = m0lib.grab_window_content(hwnd)
        if alt is not None and alt.std() >= min_std:
            return (window_rect(hwnd), alt, "printwindow", False)
    except Exception as e:
        print("page_of_window err:", e)
    raise RuntimeError("无法取得窗口 %d 内容" % hwnd)


def find_window_by_title(substr):
    import win32gui
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and substr in win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


def _vk(vk):
    return ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000


def wait_key_or_escape(prompt: str, timeout_s=120.0):
    """轮询等待：Enter(VK 0x0D) 返回 'enter'；Esc(0x1B) 返回 'esc'；超时抛异常。"""
    print(prompt, flush=True)
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        if _vk(0x0D) and last != "enter":
            last = "enter"
            time.sleep(0.4)
            return "enter"
        if _vk(0x1B) and last != "esc":
            last = "esc"
            time.sleep(0.4)
            return "esc"
        time.sleep(0.05)
    raise TimeoutError("等待按键超时")


def monitor_rect(mon_idx=1):
    import mss
    with mss.mss() as s:
        m = s.monitors[mon_idx]
    return (m["left"], m["top"], m["width"], m["height"])


# ---------------- capture ----------------

def do_capture(args):
    import win32gui
    if args.title:
        cands = find_window_by_title(args.title)
        if not cands:
            raise SystemExit("按标题 '%s' 未找到可见窗口" % args.title)
        hwnd = cands[0]
        title = win32gui.GetWindowText(hwnd)
        print("使用目标窗口: %r hwnd=%d" % (title, hwnd))
    else:
        fg = m0lib.fg_window_info()
        hwnd = fg["hwnd"]
        title = fg["title"]
        if not hwnd or not title:
            raise SystemExit("没有可用的前台窗口")
        print("前台窗口: %r hwnd=%d" % (title, hwnd))
    rect, page_bgr, cap_method, _ = page_of_window(hwnd)
    print("页面矩形=%s 采集方法=%s" % (rect, cap_method))
    if args.manual:
        p1 = None
        p2 = None
        for i, (label, want) in enumerate([("部件左上角", 1), ("部件右下角", 2)], start=1):
            while True:
                k = wait_key_or_escape("第 %d/3 步：把鼠标移到【%s】，然后按 Enter（Esc 放弃）" % (i, label))
                if k == "esc":
                    raise SystemExit("手动放弃")
                pt = m0lib.cursor_pos()
                if want == 1:
                    p1 = pt
                    break
                p2 = pt
                break
        k = wait_key_or_escape("第 3/3 步：把鼠标移到【实际要点击的位置】，按 Enter 确认（Esc 放弃）")
        if k == "esc":
            raise SystemExit("手动放弃")
        click_pt = m0lib.cursor_pos()
        rect2 = window_rect(hwnd)
        drift = max(abs(rect2[i] - rect[i]) for i in range(4))
        if drift > 10:
            raise SystemExit("capture 过程中窗口被移动了（drift=%dpx），请重试" % drift)
        x0, y0 = min(p1[0], p2[0]), min(p1[1], p2[1])
        x1, y1 = max(p1[0], p2[0]), max(p1[1], p2[1])
        wrect = (x0, y0, x1 - x0, y1 - y0)
        if wrect[2] < 8 or wrect[3] < 8:
            raise SystemExit("部件太小，请重框")
        click_in_page = (click_pt[0] - rect[0], click_pt[1] - rect[1])
        wrect_in_page = (wrect[0] - rect[0], wrect[1] - rect[1], wrect[2], wrect[3])
    else:
        text = args.text or ""
        if args.rect:
            # 显式指定部件矩形（页内坐标，跳过 OCR 找字）——用于标题栏等零副作用目标
            rx = [int(v) for v in args.rect.split(",")]
            if len(rx) != 4 or rx[2] < 8 or rx[3] < 8:
                raise SystemExit("--rect 需 x,y,w,h（页内坐标）")
            wrect_in_page = tuple(rx)
            bx, by, bw, bh = wrect_in_page
            click_in_page = (bx + bw // 2, by + bh // 2)
            if not text:
                text = args.name
            print("显式框选: rect=%s click=%s text=%r" % (wrect_in_page, click_in_page, text))
        else:
            if not text:
                raise SystemExit("自动模式需要 --text 指定部件文字")
            f = m0lib.find_text_ocr(page_bgr, text)
            if not f["ok"]:
                print("窗口内未找到文字 '%s'（OCR 结果数=%d），可尝试 --manual 或 --rect" %
                      (text, f["ocr_count"]))
                r = m0lib.ocr_run(page_bgr)
                print("窗口内 OCR 文字:", r["txts"][:15])
                raise SystemExit(1)
            bx, by, bw, bh = f["box"]
            wrect_in_page = (max(0, bx - PAD), max(0, by - PAD), bw + 2 * PAD, bh + 2 * PAD)
            click_in_page = (bx + bw // 2, by + bh // 2)
            text = f["matched_text"]
            print("自动框选: text=%r box=%s" % (text, wrect_in_page))
    wx, wy, ww, wh = wrect_in_page
    widget_bgr = page_bgr[wy:wy + wh, wx:wx + ww].copy()
    if widget_bgr.size == 0:
        raise SystemExit("部件裁剪为空")
    wid_center = (wx + ww // 2, wy + wh // 2)
    delta = (click_in_page[0] - wid_center[0], click_in_page[1] - wid_center[1])
    r = m0lib.ocr_run(widget_bgr)
    cands = [t for t in r["txts"] if t.strip()]
    ocr_text = (args.text or "").strip() or (cands[0] if cands else "")
    uia = m0lib.uia_walk_find_text(hwnd, ocr_text, max_nodes=2500, max_ms=3000)
    uia_name = uia["hits"][0]["name"] if uia.get("ok") and uia.get("hits") else ""
    mm = monitor_rect(1)
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
    except Exception:
        dpi = 96
    target = m0lib.Target(
        target_id=args.name or ("t_%d" % int(time.time())),
        cls=args.cls, name=args.name,
        captured_at=m0lib.now_iso(),
        display={"monitor": mm, "dpi": int(dpi)},
        page={"file": "%s_page.png" % args.name, "rect": list(rect),
              "window_title": title, "window_class": win32gui.GetClassName(hwnd),
              "hwnd": int(hwnd), "cap_method": cap_method},
        widget={"file": "%s_widget.png" % args.name, "rect_in_page": list(wrect_in_page),
                "center_in_page": list(wid_center), "ocr_candidates": cands[:5]},
        click={"point_in_page": list(click_in_page), "delta_from_widget_center": list(delta)},
        ocr_text=ocr_text, uia_name=uia_name,
    )
    d = args.dir
    Path(d).mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    m0lib.save_png(IMG_DIR / target.page["file"], page_bgr)
    m0lib.save_png(IMG_DIR / target.widget["file"], widget_bgr)
    tpath = Path(d) / ("%s.json" % args.name)
    target.save(tpath)
    print("\n目标已保存: %s" % tpath)
    print("  类别=%s ocr_text=%r uia_name=%r delta=%s" %
          (target.cls, target.ocr_text, target.uia_name,
           target.click["delta_from_widget_center"]))
    print("  OCR 候选:", cands[:5])


# ---------------- run：闭环试跑 ----------------

def locate_page_live(target: m0lib.Target, screen_bgr, expected_rect):
    tpl = m0lib.load_png(IMG_DIR / target.page["file"])
    r = m0lib.find_template(screen_bgr, tpl)
    out = {"ok": r["ok"] and r["score"] >= PAGE_SCORE_MIN, "score": r["score"],
           "rect": r["rect"], "elapsed_ms": r["elapsed_ms"], "dev_px": None}
    if expected_rect and r["rect"]:
        ex, ey, ew, eh = expected_rect
        rx, ry, rw, rh = r["rect"]
        out["dev_px"] = round(max(abs(ex - rx), abs(ey - ry)), 1)
    return out


def locate_widget_chain(target, page_live_bgr, page_rect, hwnd=None):
    """
    三级定位。返回 {chosen:{level,method,confidence,box,center,elapsed_ms,ok}, detail}
    L1 UIA 树 → L2 OCR文本/模板相似度 → L3 页面内坐标映射（比例不变前提）。
    """
    detail = {"l1": None, "l2_ocr": None, "l2_tpl": None, "l3": None}
    l1 = {"ok": False}
    if hwnd and target.ocr_text:
        t0 = time.perf_counter()
        u = m0lib.uia_walk_find_text(hwnd, target.ocr_text, max_nodes=3000, max_ms=2500)
        if u.get("ok") and u.get("hits"):
            # 同名消歧：多个字面命中时，选离“页面内坐标预估”最近的（防导航树/列表其他同名项干扰）
            wr = target.widget["rect_in_page"]
            anchor = (page_rect[0] + wr[0] + wr[2] // 2, page_rect[1] + wr[1] + wr[3] // 2)
            best_hit = None
            best_d = None
            for hh in u["hits"]:
                c = hh["center"]
                d = max(abs(c[0] - anchor[0]), abs(c[1] - anchor[1]))
                if best_d is None or d < best_d:
                    best_d, best_hit = d, hh
            if best_hit is not None and best_d <= 60:
                r = best_hit["rect"]
                l1 = {"ok": True, "method": "uia", "confidence": 1.0, "box": r,
                      "center": (r[0] + r[2] // 2, r[1] + r[3] // 2),
                      "elapsed_ms": (time.perf_counter() - t0) * 1000,
                      "name": best_hit["name"], "disambig_d": round(best_d, 1),
                      "n_hits": len(u["hits"])}
            else:
                # UIA 树有同名项但坐标离预估很远 → 坐标换算不可信，弃用 L1（如实记录）
                l1 = {"ok": False, "elapsed_ms": (time.perf_counter() - t0) * 1000,
                      "reason": "coord_unreliable n=%d best_d=%s" %
                                (len(u.get("hits", [])), best_d)}
        else:
            l1 = {"ok": False, "elapsed_ms": (time.perf_counter() - t0) * 1000,
                  "reason": u.get("reason", "no_handle")}
    detail["l1"] = l1
    l2o = {"ok": False}
    if target.ocr_text:
        t0 = time.perf_counter()
        # 受限搜索：以 L3 预估位置为锚，逐步扩大条带（代价小；布局小幅漂移可覆盖）
        wr = target.widget["rect_in_page"]
        ph, pw = page_live_bgr.shape[:2]
        needle = m0lib.text_needle_short(target.ocr_text, 4)
        f = None
        used_half = None
        for half_w, half_h in ((300, 100), (650, 260)):
            cx0 = max(0, wr[0] + wr[2] // 2 - half_w)
            cy0 = max(0, wr[1] + wr[3] // 2 - half_h)
            sx = min(pw - cx0, 2 * half_w)
            sy = min(ph - cy0, wr[3] + 2 * half_h)
            if sx < 40 or sy < 24:
                continue
            strip = page_live_bgr[cy0:cy0 + sy, cx0:cx0 + sx]
            f = m0lib.find_text_ocr(strip, needle)
            used_half = (half_w, half_h)
            if f["ok"]:
                break
        full_scan = used_half == (650, 260) and not f["ok"]
        if f and f["ok"]:
            bx, by, bw, bh = f["box"]
            cx0 = max(0, wr[0] + wr[2] // 2 - used_half[0])
            cy0 = max(0, wr[1] + wr[3] // 2 - used_half[1])
            abs_box = (page_rect[0] + cx0 + bx, page_rect[1] + cy0 + by, bw, bh)
            l2o = {"ok": True, "method": "ocr_text", "confidence": round(f["score"], 3),
                   "box": abs_box, "center": (abs_box[0] + bw // 2, abs_box[1] + bh // 2),
                   "elapsed_ms": f["elapsed_ms"], "full_scan": bool(full_scan)}
        else:
            l2o = {"ok": False, "elapsed_ms": (f or {}).get("elapsed_ms", 0.0),
                   "full_scan": bool(full_scan)}
    detail["l2_ocr"] = l2o
    l2t = {"ok": False}
    tw = m0lib.load_png(IMG_DIR / target.widget["file"])
    t0 = time.perf_counter()
    f2 = m0lib.find_template(page_live_bgr, tw, score_thr=TPL_SCORE_MIN)
    if f2["ok"]:
        bx, by, bw, bh = f2["rect"]
        abs_box = (page_rect[0] + bx, page_rect[1] + by, bw, bh)
        l2t = {"ok": True, "method": "tpl", "confidence": round(f2["score"], 3),
               "box": abs_box, "center": (abs_box[0] + bw // 2, abs_box[1] + bh // 2),
               "elapsed_ms": f2["elapsed_ms"]}
    else:
        l2t = {"ok": False, "confidence": round(f2["score"], 3), "elapsed_ms": f2["elapsed_ms"]}
    detail["l2_tpl"] = l2t
    wr = target.widget["rect_in_page"]
    l3box = (page_rect[0] + wr[0], page_rect[1] + wr[1], wr[2], wr[3])
    l3 = {"ok": True, "method": "page_coord", "confidence": 1.0, "box": l3box,
          "center": (l3box[0] + wr[2] // 2, l3box[1] + wr[3] // 2), "elapsed_ms": 0.0}
    detail["l3"] = l3
    if l1["ok"]:
        chosen = dict(l1, level=1)
    elif l2o["ok"] or l2t["ok"]:
        a, b = (l2o, l2t)
        if a["ok"] and b["ok"]:
            src = a if a["confidence"] >= b["confidence"] else b
        else:
            src = a if a["ok"] else b
        chosen = dict(src, level=2)
    else:
        chosen = dict(l3, level=3)
    return chosen, detail


def do_run(args):
    tdir = Path(args.dir)
    targets = sorted(tdir.glob("*.json"))
    if not targets:
        raise SystemExit("目录里没有目标: %s" % tdir)
    if args.count <= 0:
        args.count = 10 ** 9
    print("加载目标 %d 个: %s" % (len(targets), [t.stem for t in targets]))
    mm = monitor_rect(1)
    mw, mh = mm[2], mm[3]
    attempt = 0
    rng = random.Random(args.seed)
    while attempt < args.count:
        for tpath in targets:
            if attempt >= args.count:
                break
            attempt += 1
            target = m0lib.Target.load(tpath)
            hwnd = int(target.page.get("hwnd") or 0)
            if hwnd:
                import win32gui
                try:
                    if not win32gui.IsWindow(hwnd):
                        print("[%s] 窗口句柄失效，按标题找回" % tpath.stem)
                        cands = find_window_by_title(target.page.get("window_title", ""))
                        hwnd = cands[0] if cands else 0
                        if not hwnd:
                            m0lib.log_result({"type": "trial", "target_id": target.target_id,
                                              "cls": target.cls, "ok": False,
                                              "note": "window_not_found"})
                            continue
                except Exception:
                    hwnd = 0
            moved = False
            if args.move and hwnd:
                import win32con
                import win32gui
                try:
                    outer = win32gui.GetWindowRect(hwnd)
                    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
                    maxx = max(20, mw - ow - 20)
                    maxy = max(20, mh - oh - 20)
                    if maxx > 40 and maxy > 40:
                        nx = rng.randint(20, maxx)
                        ny = rng.randint(20, maxy)
                        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, nx, ny, 0, 0,
                                              win32con.SWP_NOSIZE)
                        moved = True
                        time.sleep(0.45)
                except Exception as e:
                    print("移动窗口失败:", e)
            # 置顶置前 + 遮挡同源校验（被用户窗口盖住时跳过本轮，避免脏数据/误点用户界面）
            # 置顶置前（TOPMOST 保证绘制在最上层；前台命中记录供统计分层）。
            # 安全由“点击前文字校验”兜底：定位存疑的轮次不发送真实点击。
            raised_ok = False
            if hwnd:
                raised_ok = m0lib.bring_to_foreground(hwnd)
                time.sleep(0.9)  # UWP/桌面窗口重绘需要更长时间
            # 期望真值：置前后的窗口矩形
            page_expected = window_rect(hwnd) if hwnd else None
            t0 = time.perf_counter()
            page = locate_page_live(target, m0lib.grab_screen(), page_expected)
            if not page["ok"] and hwnd:
                # 页面定位失败 → 像用户“点一下窗口”那样再置顶一次后重试（瞬态遮挡/合成竞争）
                m0lib.bring_to_foreground(hwnd)
                time.sleep(1.2)
                page_expected = window_rect(hwnd)
                t0 = time.perf_counter()
                page = locate_page_live(target, m0lib.grab_screen(), page_expected)
            row = {"type": "trial", "target_id": target.target_id, "cls": target.cls,
                   "attempt": attempt, "moved": moved, "raised": raised_ok,
                   "page": page, "widget": {}, "verify": {}, "ok": False}
            if not page["ok"]:
                m0lib.demote_window(hwnd)
                row["total_ms"] = round((time.perf_counter() - t0) * 1000)
                row["note"] = "page_not_found score=%.2f" % page["score"]
                m0lib.log_result(row)
                print("[%03d] %-10s page FAIL score=%.2f (%dms)" %
                      (attempt, target.cls, page["score"], page["elapsed_ms"]), flush=True)
                time.sleep(args.interval)
                continue
            pr = page["rect"]
            page_live = m0lib.grab_screen(pr)
            chosen, detail = locate_widget_chain(target, page_live, pr, hwnd=hwnd)
            row["widget"]["detail"] = {
                "l1": {k: detail["l1"][k] for k in ("ok", "method", "elapsed_ms")
                       if k in detail["l1"]},
                "l2_ocr": {k: detail["l2_ocr"][k] for k in ("ok", "method", "confidence", "elapsed_ms", "full_scan")
                           if k in detail["l2_ocr"]},
                "l2_tpl": {k: detail["l2_tpl"][k] for k in ("ok", "method", "confidence", "elapsed_ms")
                           if k in detail["l2_tpl"]}}
            row["widget"]["chosen_level"] = chosen.get("level")
            row["widget"]["method"] = chosen.get("method")
            row["widget"]["confidence"] = chosen.get("confidence")
            row["widget"]["elapsed_ms"] = round(chosen.get("elapsed_ms", 0), 1)
            cx = chosen["center"][0] + target.click["delta_from_widget_center"][0]
            cy = chosen["center"][1] + target.click["delta_from_widget_center"][1]
            row["widget"]["click_point"] = [cx, cy]
            truth = None
            dev_px = None
            if page_expected:
                truth = (page_expected[0] + target.widget["center_in_page"][0],
                         page_expected[1] + target.widget["center_in_page"][1])
                dev_px = round(max(abs(cx - truth[0]), abs(cy - truth[1])), 1)
            row["widget"]["truth_center"] = list(truth) if truth else None
            row["widget"]["dev_px"] = dev_px
            # 先验证再点击：点击点周边 OCR 仍含目标文字 → 才允许发送真实点击
            # （防误点用户界面：定位存疑就不点，记 miss）
            ver = {"method": "ocr_patch", "ok": False}
            if target.ocr_text:
                patch = m0lib.grab_screen((max(0, cx - 70), max(0, cy - 28), 140, 56))
                needle = m0lib.text_needle_short(target.ocr_text, 4)
                f = m0lib.find_text_ocr(patch, needle, upsample=2)
                ver = {"method": "ocr_patch", "ok": f["ok"], "score": f["score"],
                       "matched": f.get("matched_text")}
            else:
                ver = {"method": "page_score", "ok": page["score"] >= 0.85,
                       "score": page["score"]}
            row["verify"] = ver
            if ver["ok"] and (dev_px is None or dev_px <= DEV_OK_PX):
                m0lib.click_at(cx, cy)
                row["widget"]["click_sent"] = True
            else:
                row["widget"]["click_sent"] = False
                row["note"] = "no_click_guard ver=%s dev=%s" % (ver["ok"], dev_px)
            hit = (page["ok"] and chosen["ok"] and ver["ok"] and
                   row["widget"]["click_sent"] and
                   (dev_px is None or dev_px <= DEV_OK_PX))
            row["ok"] = bool(hit)
            row["total_ms"] = round((time.perf_counter() - t0) * 1000)
            m0lib.log_result(row)
            print("[%03d] %-10s page=%.2f@%dms L%d(%s) dev=%spx ver=%s %s (%dms)" %
                  (attempt, target.cls, page["score"], page["elapsed_ms"],
                   chosen.get("level"), chosen.get("method", "-"), dev_px,
                   ver["ok"], "HIT" if hit else "miss", row["total_ms"]), flush=True)
            m0lib.demote_window(hwnd)
            time.sleep(args.interval)


def do_selftest(args):
    ok1 = m0lib.selftest_template()
    print("模板定位自检:", "PASS" if ok1 else "FAIL")
    if not ok1:
        raise SystemExit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--name", required=True)
    p.add_argument("--cls", required=True, help="web-login/erp-web/desktop/icon-app/dynamic")
    p.add_argument("--text", default="", help="自动模式：部件文字（OCR 在页面图内查找）")
    p.add_argument("--rect", default="", help="显式部件矩形 x,y,w,h（页内物理像素，跳过 OCR 找字）")
    p.add_argument("--title", default="", help="按窗口标题锁定目标（否则用前台窗口）")
    p.add_argument("--manual", action="store_true", help="手动模式：3 次 Enter 框选")
    p.add_argument("--dir", default=str(CAPTURE_DIR))
    p = sub.add_parser("run")
    p.add_argument("--dir", default=str(CAPTURE_DIR))
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--move", action="store_true", help="每轮随机移动窗口后再定位")
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--seed", type=int, default=7)
    sub.add_parser("selftest")
    args = ap.parse_args()
    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    if args.cmd == "capture":
        do_capture(args)
    elif args.cmd == "run":
        do_run(args)
    elif args.cmd == "selftest":
        do_selftest(args)
