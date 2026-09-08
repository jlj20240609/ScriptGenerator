# -*- coding: utf-8 -*-
"""
M0 Spike 共享库（可丢弃产物，仅供 smoke/ 下验证脚本使用）
职责：DPI 初始化 / 截图 / OCR / UIA / 模板匹配 / 点击 / 目标存取 / 结果日志
坐标约定：全部使用物理像素（mss 截图坐标 == win32 光标坐标，DPI-aware 进程内）。
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import difflib
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import cv2
import numpy as np

# ---------------- 基础环境 ----------------

def init_dpi_aware() -> None:
    """进程级 DPI 感知：per-monitor v2 -> per-monitor -> system。须在创建任何窗口/取坐标前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()


def setup_utf8_stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


# ---------------- 截图 ----------------

_GRAB = None


def grab_screen(rect=None) -> np.ndarray:
    """截取屏幕（全虚拟屏或 rect=(x,y,w,h)），返回 BGR ndarray（H,W,3 uint8）。"""
    global _GRAB
    import mss

    if _GRAB is None:
        _GRAB = mss.mss()
    if rect is None:
        mon = _GRAB.monitors[0]
        img = np.asarray(_GRAB.grab(mon))[:, :, :3]  # BGRA -> BGR
        return img
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        raise ValueError(f"bad rect {rect}")
    img = np.asarray(_GRAB.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3]
    return img


def save_png(path, bgr) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), bgr)


def load_png(path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


# ---------------- 模板匹配 ----------------

def find_template(screen_bgr, tpl_bgr, scales=(1.0, 1.05, 0.95, 1.10, 0.90),
                  score_thr=0.70) -> dict:
    """
    全屏/区域内模板匹配，返回 {ok, score, rect(x,y,w,h), center(x,y), elapsed_ms, scale}。
    大模板（>~100k 像素）自动金字塔：先降采样粗定位，再在原图局部精修。
    多尺度取最高分。
    """
    t0 = time.perf_counter()
    s_h, s_w = screen_bgr.shape[:2]
    t_h, t_w = tpl_bgr.shape[:2]
    if t_h > s_h or t_w > s_w:
        return {"ok": False, "score": -1.0, "rect": None, "center": None,
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "scale": 1.0}

    def _match_once(sc_img, sc_tpl, sc):
        res = cv2.matchTemplate(sc_img, sc_tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, mxl = cv2.minMaxLoc(res)
        return float(mx), int(mxl[0]), int(mxl[1])

    coarse_px = 130_000  # 模板超过此像素数走金字塔
    best = {"score": -1.0, "x": 0, "y": 0, "w": 0, "h": 0, "scale": 1.0}
    for sc in scales:
        th, tw = int(round(t_h * sc)), int(round(t_w * sc))
        if th < 8 or tw < 8 or th > s_h or tw > s_w:
            continue
        tpl = cv2.resize(tpl_bgr, (tw, th), interpolation=cv2.INTER_AREA) if sc != 1.0 else tpl_bgr
        if th * tw <= coarse_px:
            mx, bx, by = _match_once(screen_bgr, tpl, sc)
            if mx > best["score"]:
                best = {"score": mx, "x": bx, "y": by, "w": tw, "h": th, "scale": sc}
            continue
        # 金字塔：ds 使模板约 ≤120k 像素
        ds = max(2, int((th * tw / coarse_px) ** 0.5))
        small = cv2.resize(screen_bgr, (s_w // ds, s_h // ds), interpolation=cv2.INTER_AREA)
        stpl = cv2.resize(tpl, (tw // ds, th // ds), interpolation=cv2.INTER_AREA)
        mx, bx, by = _match_once(small, stpl, sc)
        if mx > best["score"]:
            # 精修：原图局部窗口搜索
            cx, cy = bx * ds, by * ds
            m = ds * 3
            x0, y0 = max(0, cx - m), max(0, cy - m)
            x1 = min(s_w, cx + tw + m)
            y1 = min(s_h, cy + th + m)
            if x1 - x0 >= tw and y1 - y0 >= th:
                region = screen_bgr[y0:y1, x0:x1]
                mx2, lx, ly = _match_once(region, tpl, sc)
                if mx2 > mx:
                    best = {"score": mx2, "x": x0 + lx, "y": y0 + ly,
                            "w": tw, "h": th, "scale": sc}
                else:
                    best = {"score": mx, "x": cx, "y": cy, "w": tw, "h": th, "scale": sc}
            else:
                best = {"score": mx, "x": cx, "y": cy, "w": tw, "h": th, "scale": sc}
    if best["score"] >= score_thr:
        return {"ok": True, "score": best["score"],
                "rect": (best["x"], best["y"], best["w"], best["h"]),
                "center": (best["x"] + best["w"] // 2, best["y"] + best["h"] // 2),
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "scale": best["scale"]}
    return {"ok": False, "score": best["score"], "rect": None, "center": None,
            "elapsed_ms": (time.perf_counter() - t0) * 1000, "scale": best["scale"]}


def pixel_sim(a, b, size=(144, 90), thr=48.0) -> float:
    """两图“同源度”≈1-差异像素占比（对动态内容/白底 UI 稳健，替代互相关）。"""
    import cv2 as _cv2
    ta = _cv2.resize(a, size)
    tb = _cv2.resize(b, size)
    if ta.ndim == 3:
        ta = _cv2.cvtColor(ta, _cv2.COLOR_BGR2GRAY)
        tb = _cv2.cvtColor(tb, _cv2.COLOR_BGR2GRAY)
    diff = np.abs(ta.astype(np.int16) - tb.astype(np.int16))
    return float(1.0 - (diff > thr).mean())


def text_similar(a: str, b: str, thr=0.75) -> float:
    a = "".join(a.split()).lower()
    b = "".join(b.split()).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:  # 行内含目标词（OCR 常把整行连读）
        return 0.98
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------- OCR ----------------

_OCR_ENGINE = None


def ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_run(bgr, timeout_s=60.0) -> dict:
    """OCR 一张 BGR 图，返回 {txts:[...], boxes:[(x,y,w,h) 整数, 对齐图坐标], scores, elapsed_ms}"""
    t0 = time.perf_counter()
    out = ocr_engine()(bgr)
    elapsed = (time.perf_counter() - t0) * 1000
    boxes, txts, scores = [], [], []
    if out is not None and out.boxes is not None:
        for box, txt, sc in zip(out.boxes, out.txts, out.scores):
            arr = np.asarray(box)
            x0, y0 = int(arr[:, 0].min()), int(arr[:, 1].min())
            x1, y1 = int(arr[:, 0].max()), int(arr[:, 1].max())
            boxes.append((x0, y0, x1 - x0, y1 - y0))
            txts.append(str(txt))
            scores.append(float(sc))
    return {"txts": txts, "boxes": boxes, "scores": scores,
            "elapsed_ms": elapsed, "engine": "rapidocr3.8"}


def find_text_ocr(bgr, text, thr=0.75, upsample=2) -> dict:
    """
    在图中找文字 text，返回 {ok, box(x,y,w,h), center, score, elapsed_ms, matched_text}。
    先原图 OCR；空检测（det 偶发故障）自动重试一次；找不到且允许放大时 2x 放大再试（小字更稳）。
    """
    def _search(img_bgr):
        r = ocr_run(img_bgr)
        if not r["boxes"]:
            time.sleep(0.25)
            r = ocr_run(img_bgr)  # det 偶发空结果 → 重试一次
        best = None
        for bx, txt, sc in zip(r["boxes"], r["txts"], r["scores"]):
            sim = text_similar(txt, text)
            if sim >= thr and (best is None or sim > best["sim"]):
                best = {"sim": sim, "box": bx, "txt": txt, "sc": sc}
        return r, best

    t0 = time.perf_counter()
    r, best = _search(bgr)
    used_upsample = 0.0
    if best is None and upsample > 1 and bgr.shape[0] * upsample <= 8192:
        img2 = cv2.resize(bgr, None, fx=upsample, fy=upsample, interpolation=cv2.INTER_CUBIC)
        r2, best2 = _search(img2)
        if best2 is not None:
            used_upsample = upsample
            best = best2
            # 坐标除回原图
            bx, by, bw, bh = best["box"]
            best["box"] = (bx // upsample, by // upsample, bw // upsample, bh // upsample)
            best["_from_up"] = True
    if best is None:
        return {"ok": False, "box": None, "center": None, "score": 0.0,
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "matched_text": None,
                "ocr_count": len(r["txts"]), "upsample": used_upsample}
    x, y, w, h = best["box"]
    return {"ok": True, "box": best["box"], "center": (x + w // 2, y + h // 2),
            "score": best["sim"], "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "matched_text": best["txt"], "ocr_count": len(r["txts"]), "upsample": used_upsample}


# ---------------- Windows / UIA ----------------

def win32(module_name, attr):
    import importlib
    m = importlib.import_module(module_name)
    return getattr(m, attr)


def fg_window_info() -> dict:
    win32gui = win32("win32gui", "win32gui")
    hwnd = win32gui.GetForegroundWindow()
    rect = win32gui.GetWindowRect(hwnd)
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    return {"hwnd": hwnd, "rect": rect, "title": title, "class": cls}


def move_window(hwnd, x, y) -> None:
    import win32con
    import win32gui
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, x, y, 0, 0,
                          win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


def bring_to_foreground(hwnd) -> bool:
    """把窗口带到前台（真实用户视角的页面可见性）。返回是否成功置前。
    Windows 前台锁限制：先发 Alt 键解锁，再 SetForegroundWindow（经典 workaround）。
    置前同时设 TOPMOST，保证即使前台抢占失败也画在最上层。"""
    import time as _t
    import win32api
    import win32con
    import win32gui
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE if win32gui.IsIconic(hwnd) else win32con.SW_SHOW)
    except Exception:
        pass
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:
        pass
    for _ in range(3):
        try:
            # Alt 键释放前台锁
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32gui.SetForegroundWindow(hwnd)
            _t.sleep(0.25)
            if win32gui.GetForegroundWindow() == hwnd:
                _t.sleep(0.15)
                return True
        except Exception:
            pass
        _t.sleep(0.3)
    return False


def demote_window(hwnd) -> None:
    """取消窗口 TOPMOST，恢复普通 z 序（避免测试窗常驻用户桌面上层）。"""
    import win32con
    import win32gui
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:
        pass


def grab_window_content(hwnd) -> np.ndarray or None:
    """PrintWindow 抓取窗口自身内容（即使被遮挡）。失败或内容异常返回 None。"""
    import win32gui
    import win32ui
    try:
        user32 = ctypes.windll.user32
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
        hwnd_dc = user32.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not ok:
            ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not ok:
            return None
        bmp_info = bmp.GetInfo()
        buf = bmp.GetBitmapBits(True)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((bmp_info["bmHeight"],
                                                          bmp_info["bmWidth"], 4))
        arr = np.ascontiguousarray(arr[:, :, 2::-1])  # BGRA -> BGR
        if arr.std() < 10:  # 大概率黑屏/空白
            return None
        return arr
    except Exception:
        return None
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            user32.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def cursor_pos():
    p = win32("win32api", "win32api").GetCursorPos()
    return p  # 物理像素


_UIA_INTERACTIVE = {"ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl",
                    "ComboBoxControl", "ListControl", "ListItemControl", "HyperlinkControl",
                    "TabControl", "TabItemControl", "MenuItemControl", "TreeItemControl",
                    "SliderControl", "SpinnerControl", "DataItemControl", "CustomControl"}


def _uia_ctl_type(e):
    return type(e).__name__


def uia_walk_find_text(hwnd, text, max_nodes=4000, max_ms=4000.0) -> dict:
    """
    在 hwnd 的 UI 树内找 Name 与 text 相似(>=0.8) 的控件。
    返回 {ok, hits:[{name,rect,center,type,automation_id}, ...(≤6, 树序)], nodes, elapsed_ms}
    """
    import uiautomation as auto
    t0 = time.perf_counter()
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return {"ok": False, "hits": [], "nodes": 0,
                "elapsed_ms": (time.perf_counter() - t0) * 1000}
    target_text = "".join(text.split()).lower()
    hits = []
    visited = 0
    deadline = time.perf_counter() + max_ms / 1000

    def walk(e, depth):
        nonlocal visited
        if depth > 10 or time.perf_counter() > deadline or visited >= max_nodes or len(hits) >= 6:
            return
        try:
            children = e.GetChildren()
        except Exception:
            return
        for c in children:
            visited += 1
            if len(hits) >= 6:
                return
            try:
                name = (c.Name or "").strip()
            except Exception:
                name = ""
            if name:
                sim = text_similar(name, target_text, thr=0.8)
                if sim >= 0.8:
                    try:
                        r = c.BoundingRectangle
                        rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                    except Exception:
                        rect = None
                    try:
                        aid = c.AutomationId or ""
                    except Exception:
                        aid = ""
                    if rect is not None and rect[2] > 0 and rect[3] > 0:
                        hits.append({"name": name, "rect": rect,
                                     "center": (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2),
                                     "type": _uia_ctl_type(c), "automation_id": aid,
                                     "sim": round(sim, 3)})
                        continue  # 继续找同名字面（可能有多个）
            walk(c, depth + 1)

    walk(root, 0)
    if not hits:
        return {"ok": False, "hits": [], "nodes": visited,
                "elapsed_ms": (time.perf_counter() - t0) * 1000,
                "reason": "timeout" if time.perf_counter() > deadline else "not_found"}
    return {"ok": True, "hits": hits, "nodes": visited,
            "elapsed_ms": (time.perf_counter() - t0) * 1000}


def text_needle_short(text: str, n=4) -> str:
    """长目标文字取前 n 个字符作为短探针（避免 OCR 行分段导致整句匹配失败）。"""
    t = "".join(text.split())
    return t[:n] if len(t) > n else t


# ---------------- 鼠标键盘 ----------------

def click_at(x, y, delay=0.05) -> None:
    from pynput.mouse import Button, Controller
    m = Controller()
    m.position = (int(x), int(y))
    time.sleep(delay)
    m.click(Button.left, 1)


def type_text(text, interval=0.02) -> None:
    from pynput.keyboard import Controller as K
    k = K()
    for ch in text:
        k.press(ch)
        k.release(ch)
        time.sleep(interval)


# ---------------- 结果日志 ----------------

def log_result(data: dict, path=None) -> None:
    if path is None:
        path = Path(__file__).parent / "data" / "trials.jsonl"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_iso(), **data}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_results(path=None):
    if path is None:
        path = Path(__file__).parent / "data" / "trials.jsonl"
    rows = []
    if not Path(path).exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


# ---------------- 目标（双截图产物）----------------

@dataclass
class Target:
    schema: int = 1
    target_id: str = ""
    cls: str = ""            # 类别: web-login / erp-web / desktop / icon-app / dynamic
    name: str = ""
    captured_at: str = ""
    display: dict = field(default_factory=dict)   # 采集时显示器信息
    page: dict = field(default_factory=dict)      # page.png 相对路径 + 采集时屏幕 rect + hwnd 线索
    widget: dict = field(default_factory=dict)    # widget.png + 采集时在 page 内的 rect + 点击点
    click: dict = field(default_factory=dict)     # 相对 widget 中心的偏移 dx/dy
    ocr_text: str = ""                            # 用于 OCR/文本匹配的目标文字
    uia_name: str = ""                            # 采集时在 UI 树上命中的名称（若有）

    def save(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path) -> "Target":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return Target(**{k: d.get(k) for k in Target.__dataclass_fields__})


# 冒烟自检：合成偏移图上的模板定位应准确找回
def selftest_template() -> bool:
    from PIL import Image, ImageDraw, ImageFont
    # 造一张有内容的“页面”模板
    page = Image.new("RGB", (420, 240), (245, 247, 250))
    d = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    except Exception:
        font = ImageFont.load_default()
    d.text((30, 30), "M0 page marker Login", font=font, fill=(20, 20, 20))
    d.rectangle((30, 90, 380, 140), fill=(66, 133, 244))
    d.text((60, 103), "Sign in button", font=font, fill=(255, 255, 255))
    tpl = np.asarray(page)[:, :, ::-1].copy()  # RGB->BGR
    # 放到一张大图偏移 (333, 77) 处
    canvas = np.full((900, 1400, 3), 18, np.uint8)
    canvas[77:77 + 240, 333:333 + 420] = tpl
    r = find_template(canvas, tpl, scales=(1.0,))
    exp = (333, 77, 420, 240)
    if not r["ok"]:
        print("  [selftest] FAIL template not found, score=%.3f" % r["score"])
        return False
    got = r["rect"]
    dev = max(abs(got[i] - exp[i]) for i in range(4))
    ok = dev == 0
    print("  [selftest] %s: expected %s got %s score=%.3f" %
          ("OK" if ok else "FAIL", exp, got, r["score"]))
    return ok
