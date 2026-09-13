# -*- coding: utf-8 -*-
"""
engine.capture — E1：双截图采集（页面/锚/部件）+ 窗口上下文 + 跨帧静态性复验。

职责边界（v0.31 §7.3 +《M1_引擎设计清单》§2/E1）：
  - capture_page(hwnd) → PageSpec（context/image/size/rect_in_screen/anchors/scale_range/capture_meta）
  - capture_anchor / verify_static：静态锚人工框选后的跨帧复验（dt≥2s，同源 ≥0.85）
  - 窗口上下文确定性采集（进程名/标题/类名，WinAPI，无 LLM）

本模块的几何/纯函数部分可离线单测；涉及真实窗口的函数惰性导入 win32/mss，
在无桌面环境调用时抛 EngineError(driver_error)。
坐标一律物理像素、进程须先 init_dpi_aware()（见 run/CLI 入口）。
"""
from __future__ import annotations

import ctypes
import time
from pathlib import Path

import numpy as np

from engine import matcher
from engine import schema
from engine.errors import EngineError, ERRORS


# ---------------------------------------------------------------- 纯函数（可离线单测）

def crop_rect(bgr, rect_xywh):
    """按 (x, y, w, h) 裁剪；越界自动截断，空尺寸返回 None。"""
    x, y, w, h = [int(v) for v in rect_xywh]
    hh, ww = bgr.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(ww, x + w), min(hh, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return bgr[y0:y1, x0:x1]


def static_score(a, b, thr=0.85) -> float:
    """跨帧同源分（模板自匹配）：两帧同区域图像同源度 ≥thr 视为静态。"""
    return matcher.pixel_sim(a, b, thr=48.0)


def verify_static(frame0_bgr, frame_provider, dt_s=2.0, thr=0.85) -> dict:
    """
    跨帧静态性复验（§7.3：间隔 ≥2s 两帧，模板自匹配 score ≥0.85 才采纳）。
    frame_provider() -> bgr（同一屏幕区域的新一帧）。返回 {ok, score, dt_s, elapsed_ms}
    """
    t0 = time.perf_counter()
    time.sleep(max(0.0, dt_s))
    frame1 = frame_provider()
    score = static_score(frame0_bgr, frame1)
    return {"ok": score >= thr, "score": round(score, 4),
            "dt_s": dt_s, "elapsed_ms": (time.perf_counter() - t0) * 1000}


def page_rect_from_anchor(hit_xywh, anchor_rect_in_page, page_size) -> dict:
    """
    锚命中 → 还原页面屏幕矩形（§7.3 静态锚定位规则）：
      页面原点 = 锚命中左上 − 录制页内偏移 × 缩放；页面尺寸 × 缩放。
    返回 {rect, scale, ok, reason}；页面越出屏幕由调用方按搜索边界裁处。
    """
    ax, ay, aw, ah = [float(v) for v in hit_xywh]
    rx, ry, rw, rh = [float(v) for v in anchor_rect_in_page]
    if rw <= 0 or rh <= 0 or aw <= 0 or ah <= 0:
        return {"ok": False, "reason": "bad_geometry"}
    s = (aw / rw + ah / rh) / 2.0
    if s <= 0 or s > 4.0:
        return {"ok": False, "reason": f"scale_out_of_range:{s:.3f}"}
    ox, oy = ax - rx * s, ay - ry * s
    pw, ph = float(page_size[0]) * s, float(page_size[1]) * s
    if pw <= 0 or ph <= 0:
        return {"ok": False, "reason": "bad_page_size"}
    return {"ok": True, "rect": (int(round(ox)), int(round(oy)),
                                 int(round(pw)), int(round(ph))),
            "scale": round(s, 4)}


def make_page_spec(*, bgr, rect_in_screen, context=None, anchors=None, dpi=None,
                   ts=None, cap_method="screen", visible=True) -> dict:
    """页面图像 → PageSpec（v0.31 全量字段；image/size 必填，rect/anchors 可空）。"""
    h, w = bgr.shape[:2]
    spec = schema.page_spec(
        image_dataurl=matcher.bgr_to_dataurl(bgr), size=(w, h),
        context=context or {}, rect_in_screen=rect_in_screen, anchors=anchors or [],
        capture_meta={"dpi": dpi or 0, "ts": ts or _now_iso(),
                      "cap_method": cap_method, "visible": bool(visible)})
    spec["size"] = [w, h]
    return spec


def crop_anchor_spec(page_bgr, rect_in_page, stable_at=None) -> dict:
    """从页面图裁剪锚区域 → anchor spec（image data-url + 页内矩形 + 时间）。"""
    crop = crop_rect(page_bgr, rect_in_page)
    if crop is None:
        raise ValueError(f"锚区域越出页面: {rect_in_page} vs {page_bgr.shape}")
    return schema.anchor_spec(image_dataurl=matcher.bgr_to_dataurl(crop),
                              rect_in_page=rect_in_page,
                              stable_at=stable_at or _now_iso())


def crop_widget(page_bgr, rect_in_page) -> np.ndarray:
    """从页面图裁剪部件图像（第二次截图）。"""
    crop = crop_rect(page_bgr, rect_in_page)
    if crop is None:
        raise ValueError(f"部件区域越出页面: {rect_in_page} vs {page_bgr.shape}")
    return crop


# ---------------------------------------------------------------- 实时窗口（惰性 win32）

def init_dpi_aware() -> None:
    """进程级 DPI 感知（per-monitor v2 → system）。取窗口/屏幕坐标前必须调用一次。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()


def _w32():
    import win32api  # noqa: F401  (探测可用性)
    import win32con  # noqa: F401
    import win32gui  # noqa: F401
    return win32gui


def grab_screen(rect=None) -> np.ndarray:
    """截取屏幕（整虚拟屏或 rect=(x,y,w,h)），BGR。失败抛 EngineError(driver_error)。"""
    import mss
    try:
        with mss.mss() as s:
            if rect is None:
                mon = s.monitors[0]
                img = np.asarray(s.grab(mon))[:, :, :3]
            else:
                x, y, w, h = [int(v) for v in rect]
                if w <= 0 or h <= 0:
                    raise ValueError(f"bad rect {rect}")
                img = np.asarray(s.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3]
        return np.ascontiguousarray(img)
    except EngineError:
        raise
    except Exception as e:
        raise EngineError("driver_error", ERRORS["driver_error"], {"detail": repr(e)})


def window_rect(hwnd):
    import win32gui
    return tuple(int(v) for v in win32gui.GetWindowRect(hwnd))


def root_window(hwnd):
    """归一化到顶层窗口（WindowFromPoint 可能返回子窗口，标题为空会误导判断）。"""
    import win32con
    import win32gui
    try:
        r = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        return int(r or hwnd)
    except Exception:
        return int(hwnd)


def window_title(hwnd) -> str:
    import win32gui
    return win32gui.GetWindowText(hwnd) or ""


def window_class(hwnd) -> str:
    import win32gui
    return win32gui.GetClassName(hwnd) or ""


def virtual_screen_rect():
    """虚拟桌面矩形 (x, y, w, h)，用于排除最小化（落在 -25600 之类）的窗口。"""
    import win32api
    import win32con
    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return int(x), int(y), int(w), int(h)


def is_iconic(hwnd) -> bool:
    import win32gui
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


_SHELL_CLASS = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                "Windows.UI.Core.CoreWindow", "MultitaskingViewFrame",
                "ForegroundStaging", "XamlExplorerHostIslandWindow"}


def top_windows(min_w=80, min_h=60) -> list:
    """可见的普通顶层窗口 [(hwnd, rect, title, class)]（跳过桌面/任务栏/无标题/最小化）。"""
    import win32gui
    out = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            cls = win32gui.GetClassName(hwnd) or ""
            if not title or cls in _SHELL_CLASS:
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if r - l < min_w or b - t < min_h:
                return True
            out.append((hwnd, (int(l), int(t), int(r - l), int(b - t)), title, cls))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def window_for_rect(rect, tie=0.1):
    """框选区域归属的可见窗口 → (hwnd, 覆盖率 0~1)。

    取"占这块区域最多"的窗口；覆盖度接近时取**最上面那个**（用户看到的就是它）。
    框选时如果这块区域被别的窗口盖住（小屏上常见），要先把归属窗口切到前面再截图，
    否则拍到的是盖在它上面的窗口。
    """
    x, y, w, h = [int(v) for v in rect]
    area = max(1, w * h)
    cands = []                      # 按 EnumWindows 的 z 序（上→下）
    for hwnd, (l, t, ww, hh), _title, _cls in top_windows():
        ix = max(0, min(x + w, l + ww) - max(x, l))
        iy = max(0, min(y + h, t + hh) - max(y, t))
        cands.append((int(hwnd), (ix * iy) / area))
    if not cands:
        return 0, 0.0
    best_c = max(c for _h, c in cands)
    for hwnd, c in cands:           # z 序即优先级：最先遇到的就是最上面那个
        if c > 0 and c >= best_c - tie:
            return hwnd, round(c, 3)
    return 0, 0.0


def find_windows_by_title(substr) -> list:
    import win32gui
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and substr in win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


def find_window_for_context(context) -> int:
    """按记下来的窗口上下文找窗口句柄：进程名优先，其次标题，最后类名辅助；找不到返回 0。

    为什么是这个顺序：真实程序（尤其浏览器内核的应用）标题会随当前页面变 —— 哔哩哔哩就是
    现成的例子，录制时记的是首页标题，跑到别的页面标题就变了，所以进程名最稳。
    UWP 类应用的主窗口进程是 ApplicationFrameHost.exe、可能同时存在多个，这时靠标题区分。
    只命中通用类名（Chrome_WidgetWin_1 这类）不算数。
    """
    ctx = context if isinstance(context, dict) else {}
    proc = str(ctx.get("process") or "").strip().lower()
    cls = str(ctx.get("class") or "").strip()
    title = str(ctx.get("title") or "").strip()
    if not (proc or title):
        return 0
    try:
        import win32gui
    except Exception:
        return 0
    cands = []

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                cands.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return 0

    best, best_score = 0, 0
    for hwnd in cands:
        score = 0
        try:
            if proc and process_name_of(hwnd).strip().lower() == proc:
                score += 4
            if cls and window_class(hwnd) == cls:
                score += 1
            t = window_title(hwnd) or ""
            if title and t and (title in t or t in title):
                score += 3
        except Exception:
            continue
        if score > best_score:
            best, best_score = hwnd, score
    return best if best_score >= 4 else 0


def window_state_for_context(context) -> dict:
    """目标窗口"此刻怎么样了"：在不在、是不是最小化、是不是前台、前台是谁、被谁压着。

    为什么要记进定位日志（2026-09-12）：页面定位失败时模板分 0.0 与 0.3 的含义完全不同 ——
    0.0 往往是"屏幕上根本没有那个窗口"，0.3 才是"窗口在、内容变了"。以前日志里没有窗口字段，
    这两种病分不开，也就无法判断该补"把窗口调出来"还是该补"锚点/特征兜底"。
    """
    out = {"found": False, "hwnd": 0, "iconic": False, "foreground": False,
           "fg_process": "", "fg_title": "", "covered_by": ""}
    try:
        hwnd = find_window_for_context(context)
    except Exception:
        hwnd = 0
    fg = {}
    try:
        fg = fg_window_info() or {}
    except Exception:
        pass
    out["fg_process"] = str(fg.get("process") or "")
    out["fg_title"] = str(fg.get("title") or "")
    if not hwnd:
        return out
    out["found"] = True
    out["hwnd"] = int(hwnd)
    try:
        out["iconic"] = bool(is_iconic(hwnd))
    except Exception:
        pass
    out["foreground"] = int(fg.get("hwnd") or 0) == int(hwnd)
    if not out["foreground"]:
        try:
            rect = window_rect(hwnd)
            top, cover = window_for_rect(rect)
            if top and int(top) != int(hwnd) and cover >= 0.3:
                out["covered_by"] = str(window_title(top) or "")
        except Exception:
            pass
    return out


def fg_window_info() -> dict:
    import win32gui
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return {}
    return {"hwnd": hwnd, "rect": window_rect(hwnd), "title": window_title(hwnd),
            "class": window_class(hwnd)}


def window_from_point(x, y) -> int:
    """屏幕物理坐标点 → 顶层窗口句柄（确定性 WinAPI，UI 框选坐标取上下文用）。"""
    import ctypes
    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32 = ctypes.windll.user32
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, 2)      # GA_ROOT
    return int(root or hwnd)


def context_from_point(x, y) -> dict:
    """点所在窗口上下文 {hwnd, process, title, class}（§5.1 第一次截图采集）。"""
    hwnd = window_from_point(x, y)
    if not hwnd:
        return {"hwnd": 0, "process": "", "title": "", "class": ""}
    return {"hwnd": hwnd, "process": process_name_of(hwnd) or "",
            "title": window_title(hwnd), "class": window_class(hwnd)}


# ---------------------------------------------------------------- 权限（UAC）

def self_elevated() -> bool:
    """我们自己是不是以管理员身份在跑。"""
    try:
        import win32api
        import win32con
        import win32security
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False,
                                 win32api.GetCurrentProcessId())
        tok = win32security.OpenProcessToken(h, win32con.TOKEN_QUERY)
        return bool(win32security.GetTokenInformation(tok, win32security.TokenElevation))
    except Exception:
        return False


def elevation_of(hwnd, _query=None) -> str:
    """窗口所属进程的权限：'yes'（管理员）/ 'no'（普通）/ 'unknown'（问不出来）。

    为什么要专门查这个：Windows 的 UIPI 规则下，**普通权限的进程不许向管理员权限的窗口
    发送点击/按键**（钩子也收不到）。用户遇到的表现是"识别失败/点了没反应"，
    完全看不出是权限问题——所以这里要能识别出来、并明确告诉他该怎么做。
    查不到（连进程都打不开）通常本身就是"它比我权限高"的证据，标为 unknown 让上层提示。
    """
    try:
        import win32api
        import win32con
        import win32process
        import win32security
        if _query is not None:
            return _query(hwnd)
        if not hwnd:
            return "unknown"
        _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        if not pid:
            return "unknown"
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
        tok = win32security.OpenProcessToken(h, win32con.TOKEN_QUERY)
        yes = bool(win32security.GetTokenInformation(tok, win32security.TokenElevation))
        return "yes" if yes else "no"
    except Exception:
        return "unknown"


def process_name_of(hwnd) -> str:
    """窗口所属进程 exe 名（确定性 WinAPI，§7.3 注）。"""
    import win32process
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        import win32api
        h = win32api.OpenProcess(0x0400 | 0x0010, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            import win32process as wp
            return Path(wp.GetModuleFileNameEx(h, 0)).name
        finally:
            h.Close()
    except Exception:
        return ""


def dpi_of(hwnd=None) -> int:
    try:
        if hwnd:
            return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        return 96


def bring_to_foreground(hwnd) -> bool:
    """带窗口到前台（M0 实测 workaround：Alt 键解锁前台锁 + TOPMOST）。"""
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
    import win32con
    import win32gui
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:
        pass


def grab_window_content(hwnd):
    """PrintWindow 抓窗口自身内容（被遮挡也可用）；失败返回 None。"""
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
        return arr if arr.std() >= 10 else None
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


def page_of_window(hwnd, min_std=14.0, sim_min=0.40):
    """
    抓目标窗口页面：置前置顶后抓屏，与 PrintWindow 内容做像素同源校验；
    校验不过重试一次；仍不可见退回 PrintWindow 内容并标记 visible=False。
    返回 (rect, bgr, method, visible)（M0 m0_trial.page_of_window 迁移）。
    """
    try:
        bring_to_foreground(hwnd)
        time.sleep(0.55)
        rect = window_rect(hwnd)
        if rect[2] > 0 and rect[3] > 0:
            bgr = grab_screen(rect)
            ref = grab_window_content(hwnd)
            if ref is not None and ref.std() >= min_std:
                sim = matcher.pixel_sim(bgr, ref)
                if sim < sim_min:
                    bring_to_foreground(hwnd)
                    time.sleep(0.6)
                    rect = window_rect(hwnd)
                    bgr = grab_screen(rect)
                    sim = matcher.pixel_sim(bgr, ref)
                if sim < sim_min:
                    return (rect, ref, "printwindow", False)
            return (rect, bgr, "screen", True)
        alt = grab_window_content(hwnd)
        if alt is not None and alt.std() >= min_std:
            return (window_rect(hwnd), alt, "printwindow", False)
    except Exception as e:
        raise EngineError("driver_error", f"窗口 {hwnd} 页面采集失败: {e}")
    raise EngineError("driver_error", f"无法取得窗口 {hwnd} 内容")


def capture_page(hwnd) -> dict:
    """
    采集操作页面 → PageSpec（§5.1 第一次截图：页面上下文 + 页面截图与尺寸）。
    返回 dict {spec, rect, bgr, method, visible}（bgr 供部件/锚采集复用）。
    """
    rect, bgr, method, visible = page_of_window(hwnd)
    ctx = {"process": process_name_of(hwnd) or "",
           "title": window_title(hwnd), "class": window_class(hwnd)}
    spec = make_page_spec(bgr=bgr, rect_in_screen=list(rect), context=ctx,
                          dpi=dpi_of(hwnd), cap_method=method, visible=visible)
    return {"spec": spec, "rect": tuple(int(v) for v in rect),
            "bgr": bgr, "method": method, "visible": visible}


def capture_anchor(hwnd, rect_in_page, page_rect, dt_s=2.0, thr=0.85) -> dict:
    """
    采集静态锚（§7.3）：页面内人工框选区域 rect_in_page → 连续两帧复验静态性。
    返回 {spec|None, ok, score, reason}：score ≥thr 才采纳并给 spec。
    """
    x0, y0 = page_rect[0] + rect_in_page[0], page_rect[1] + rect_in_page[1]
    frame0 = grab_screen((x0, y0, rect_in_page[2], rect_in_page[3]))
    if frame0 is None or frame0.size == 0:
        return {"ok": False, "reason": "empty_frame", "spec": None, "score": 0.0}
    ver = verify_static(frame0,
                        lambda: grab_screen((x0, y0, rect_in_page[2], rect_in_page[3])),
                        dt_s=dt_s, thr=thr)
    if not ver["ok"]:
        return {"ok": False, "reason": "not_static", "spec": None,
                "score": ver["score"]}
    spec = schema.anchor_spec(image_dataurl=matcher.bgr_to_dataurl(frame0),
                              rect_in_page=list(rect_in_page))
    return {"ok": True, "reason": "", "spec": spec, "score": ver["score"]}


def capture_widget(page_bgr, wrect_in_page, text="", ocr=False):
    """第二次截图产物：裁剪 + （可选）OCR 提取文字。返回 {image_dataurl, rect_in_page, center_in_page, text}"""
    crop = crop_widget(page_bgr, wrect_in_page)
    x, y, w, h = wrect_in_page
    center = [x + w // 2, y + h // 2]
    return {"image_dataurl": matcher.bgr_to_dataurl(crop),
            "rect_in_page": list(wrect_in_page), "center_in_page": center,
            "text": text}


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="milliseconds")
