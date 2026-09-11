# -*- coding: utf-8 -*-
"""
engine.recorder_live — 录制器的真机接线（M3-WP1）。

`engine/recorder.py` 是纯的、可注入的；真正必须跑在真机上的只有三件事，
全部集中在这个模块里，换成假的就能离线测：

  1. 全局键鼠钩子（pynput）
  2. 点击处属于哪个窗口（廉价：WindowFromPoint）
  3. 点到的到底是什么部件（贵：OCR 反查 → 出 schema 目标）

三条容易踩的坑，都在这里处理掉：

  * **坐标必须是物理像素**。引擎别处都以物理像素为准（1920×1200 而不是 1536×960），
    所以挂钩子前先 init_dpi_aware()，否则录到的坐标会被系统按 DPI 虚化，对不上。
  * **按键释放绝不能上报**。录制器无法区分按下/松开，把松开也发过去，
    「输入 demo」会变成「输入 demodemo」。
  * **Ctrl+S 这类组合键，pynput 给的是控制字符**（'\\x13'），不是 's'。
    不还原的话快捷键会记成一个不可读的怪字符。

已知限制（如实记下，不假装支持）：中文输入法组合出来的字，钩子只看得到原始按键，
录到的是拼音字母而不是汉字。这条在 M3 收口时向用户明说。
"""
from __future__ import annotations

import threading

from engine import capture
from engine import matcher
from engine import recorder as R
from engine import schema

# --------------------------------------------------------------------- 键名映射


def key_name(key) -> str:
    """pynput 的按键对象 → 本引擎的键名（'a' / 'enter' / 'ctrl_l' / 'f5'）。"""
    try:
        from pynput import keyboard
    except Exception:
        return ""
    if isinstance(key, keyboard.Key):
        return str(getattr(key, "name", "") or "")
    ch = getattr(key, "char", None)
    if ch:
        return str(ch)
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else ""


def unctrl(name: str, mods) -> str:
    """Ctrl 组合键的控制字符还原成字母：'\\x13' + ctrl → 's'。"""
    if "ctrl" not in (mods or ()) or len(name) != 1:
        return name
    o = ord(name)
    if o == 0:
        return "@"
    if 1 <= o <= 26:
        return chr(o + 0x60)
    if 27 <= o <= 31:
        return "[" + chr(o + 0x40) + "]"
    return name


# --------------------------------------------------------------------- 全局钩子


class PynputHooker:
    """全局键鼠监听 → 本引擎的事件格式（见 recorder 模块头）。

    只上报**按下**：松开事件录制器分不清，发过去会把输入内容翻倍。
    修饰键状态在这里维护，随每个按键事件一起给出真实组合（录制器不猜）。

    stop_combo：停止热键（如 "ctrl+alt+q"）。按下它会把 stopped 事件置位，
    **并且这个按键本身不上报**——否则停止动作自己会被录成一个垃圾积木。
    """

    def __init__(self, stop_combo: str = ""):
        self._ml = None
        self._kl = None
        self._cb = None
        self._mods: set = set()
        self._lock = threading.Lock()
        self.stopped = threading.Event()
        self._stop_mods, self._stop_key = self._parse_combo(stop_combo)

    @staticmethod
    def _parse_combo(combo: str):
        parts = [p.strip().lower() for p in str(combo or "").split("+") if p.strip()]
        if not parts:
            return frozenset(), ""
        mods = frozenset(R._norm_mod(p) for p in parts[:-1] if R.classify_key(p) == "modifier")
        return mods, parts[-1]

    # ---- 生命周期

    def start(self, cb) -> None:
        capture.init_dpi_aware()          # 坐标口径：物理像素
        from pynput import keyboard, mouse
        self._cb = cb
        with self._lock:
            self._mods = set()
        self.stopped.clear()
        self._ml = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._kl = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        for listener in (self._ml, self._kl):
            listener.daemon = True
            listener.start()

    def stop(self) -> None:
        for listener in (self._ml, self._kl):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                pass
        self._ml = self._kl = None
        with self._lock:
            self._mods = set()

    @property
    def alive(self) -> bool:
        return any(l is not None and getattr(l, "running", False)
                   for l in (self._ml, self._kl))

    # ---- 上报

    def _emit(self, ev: dict) -> None:
        cb = self._cb
        if cb is None:
            return
        try:
            cb(ev)
        except Exception:
            pass                          # 录制回调出错绝不能让钩子线程崩掉

    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return
        self._emit({"kind": R.EV_CLICK, "x": int(x), "y": int(y),
                    "button": str(getattr(button, "name", button) or "left"),
                    "double": False})

    def _on_scroll(self, x, y, dx, dy) -> None:
        self._emit({"kind": R.EV_SCROLL, "x": int(x), "y": int(y),
                    "dx": int(dx), "dy": int(dy)})

    def _on_press(self, key) -> None:
        name = key_name(key)
        if not name:
            return
        with self._lock:
            mods = sorted(self._mods)
            if R.classify_key(name) == "modifier":
                self._mods.add(R._norm_mod(name))
        if R.classify_key(name) == "modifier":
            self._emit({"kind": R.EV_KEY, "key": name, "mods": mods, "at": None})
            return
        fixed = unctrl(name, mods)
        if self._stop_key and fixed == self._stop_key and frozenset(mods) == self._stop_mods:
            self.stopped.set()            # 停止热键本身不上报，免得录成一个垃圾积木
            return
        self._emit({"kind": R.EV_KEY, "key": fixed, "mods": mods, "at": None})

    def _on_release(self, key) -> None:
        name = key_name(key)
        if not name or R.classify_key(name) != "modifier":
            return                        # 普通键的松开一个字都不许上报
        with self._lock:
            self._mods.discard(R._norm_mod(name))


# --------------------------------------------------------------------- 页面/部件


class LivePages:
    """把「点击处」变成 schema 里那个部件目标。

    页面图优先取**点击那一刻**存下的帧：录制停止后再抓，页面早已翻页
    （登录表单点完就没了），反查出来的会是完全无关的部件。
    """

    def __init__(self, ocr=True, pad_px=6, hit_radius_px=70, row_px=420,
                 max_nearby=6, page_on_first_only=True):
        self.ocr = ocr
        self.pad_px = pad_px
        self.hit_radius_px = hit_radius_px
        self.row_px = row_px
        self.max_nearby = max_nearby
        self.page_on_first_only = page_on_first_only
        self._last_page_key = None
        self._cur_page_key = None
        self._cur_spec = None

    # ---- 窗口（廉价）

    def window_of(self, x: int, y: int):
        try:
            hwnd = capture.window_from_point(int(x), int(y))
            if not hwnd:
                return None
            return {"hwnd": hwnd, "rect": list(capture.window_rect(hwnd))}
        except Exception:
            return None

    def page_key(self, win, rect):
        return (None if not win else win.get("hwnd"), tuple(rect or ()))

    # ---- 页面

    def page_of(self, x: int, y: int, frame=None, win=None):
        """(x, y, 点击那一刻的帧, 点击那一刻的窗口) → (页面矩形, 页面图, PageSpec)。"""
        win = win if isinstance(win, dict) else None
        hwnd = (win or {}).get("hwnd")
        rect = (win or {}).get("rect")
        if not rect:
            if not hwnd:
                hwnd = capture.window_from_point(int(x), int(y))
            if not hwnd:
                return None
            rect = list(capture.window_rect(hwnd))
        rect = [int(v) for v in rect]
        bgr = self._crop_from_frame(frame, rect)
        if bgr is None:
            try:
                bgr = capture.grab_screen(tuple(rect))      # 退回现场抓
            except Exception:
                return None
        if bgr is None or getattr(bgr, "size", 0) == 0:
            return None
        spec = capture.make_page_spec(bgr=bgr, rect_in_screen=rect,
                                      context=self._context(hwnd), dpi=self._dpi(hwnd))
        self._cur_page_key = self.page_key(win, rect)
        self._cur_spec = spec
        return (tuple(rect), bgr, spec)

    def _crop_from_frame(self, frame, rect):
        """全屏帧是**整虚拟屏**（mss monitors[0]），裁剪前要减掉虚拟屏原点。"""
        if frame is None:
            return None
        try:
            vx, vy, vw, vh = capture.virtual_screen_rect()
            h, w = frame.shape[:2]
            if (w, h) != (int(vw), int(vh)):
                return None
            return capture.crop_rect(frame, (rect[0] - int(vx), rect[1] - int(vy),
                                             rect[2], rect[3]))
        except Exception:
            return None

    def _context(self, hwnd) -> dict:
        if not hwnd:
            return {}
        try:
            return {"process": capture.process_name_of(hwnd) or "",
                    "title": capture.window_title(hwnd), "class": capture.window_class(hwnd)}
        except Exception:
            return {}

    def _dpi(self, hwnd):
        try:
            return capture.dpi_of(hwnd) if hwnd else capture.dpi_of()
        except Exception:
            return None

    # ---- 部件

    def widget_of(self, x: int, y: int, page_rect, page_bgr, spec):
        lx, ly = int(x) - int(page_rect[0]), int(y) - int(page_rect[1])
        toks = self._tokens(page_bgr)
        box, text = self._pick(toks, lx, ly, page_bgr.shape)
        if box is None:
            box = self._fallback_box(lx, ly, page_bgr.shape)
            text = ""
        nearby = self._nearby(toks, box, page_bgr.shape)
        try:
            got = capture.capture_widget(page_bgr, list(box), text=text)
        except Exception:
            return None
        kw = {"image_dataurl": got["image_dataurl"], "text": got["text"],
              "rect_in_page": got["rect_in_page"], "center_in_page": got["center_in_page"]}
        if nearby:
            kw["nearby"] = nearby
        if self._should_embed_page(spec):
            kw["page"] = spec
        return schema.widget_target(**kw)

    def _should_embed_page(self, spec) -> bool:
        """页面只在「第一块」和「换页」时内嵌，其余靠执行器继承页面上下文。

        为什么：每步都内嵌整页图会让脚本体积爆掉（一张页面图就是几十 KB 的
        base64）。执行器本身会在目标没有 page 时沿用上一页，这正是它想要的形状。
        """
        if not self.page_on_first_only:
            return True
        if self._cur_page_key != self._last_page_key:
            self._last_page_key = self._cur_page_key
            return True
        return False

    def _tokens(self, bgr) -> list:
        if not self.ocr or bgr is None:
            return []
        try:
            r = matcher.ocr_run(bgr)
        except Exception:
            return []
        if not r.get("ok"):
            return []
        return list(zip(r.get("txts") or [], r.get("boxes") or [], r.get("scores") or []))

    def _pick(self, toks, lx, ly, shape):
        """点到哪个文字上：先找「最小的、包住点击点」的框，再退到最近的框。"""
        inside = None
        for text, (bx, by, bw, bh), _sc in toks:
            if bx <= lx <= bx + bw and by <= ly <= by + bh:
                if inside is None or bw * bh < inside[1]:
                    inside = ((text, (bx, by, bw, bh)), bw * bh)
        if inside is not None:
            text, (bx, by, bw, bh) = inside[0]
            return self._pad_clamp((bx, by, bw, bh), shape), str(text)
        best = None
        for text, (bx, by, bw, bh), _sc in toks:
            cx, cy = bx + bw / 2.0, by + bh / 2.0
            d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            if d <= self.hit_radius_px and (best is None or d < best[0]):
                best = (d, (text, (bx, by, bw, bh)))
        if best is None:
            return None, ""
        text, (bx, by, bw, bh) = best[1]
        return self._pad_clamp((bx, by, bw, bh), shape), str(text)

    def _pad_clamp(self, box, shape):
        x, y, w, h = box
        p = int(self.pad_px)
        x0, y0 = max(0, x - p), max(0, y - p)
        H, W = int(shape[0]), int(shape[1])
        x1, y1 = min(W, x + w + p), min(H, y + h + p)
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _fallback_box(self, lx, ly, shape):
        """认不出文字时：给一个以点击点为中心的框，只靠图像匹配找部件。"""
        w, h = 160, 36
        x0, y0 = max(0, lx - w // 2), max(0, ly - h // 2)
        H, W = int(shape[0]), int(shape[1])
        x1, y1 = min(W, x0 + w), min(H, y0 + h)
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _nearby(self, toks, box, shape):
        """同一行的邻居文字（消歧用）：相对目标中心的偏移。"""
        if not toks:
            return None
        bx, by, bw, bh = box
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        out = []
        for text, (tx, ty, tw, th), _sc in toks:
            if not str(text).strip():
                continue
            if tx >= bx and ty >= by and tx + tw <= bx + bw and ty + th <= by + bh:
                continue                          # 就是目标自己
            ov = min(by + bh, ty + th) - max(by, ty)
            if ov < min(bh, th) * 0.5:
                continue                          # 不在同一行
            tcx, tcy = tx + tw / 2.0, ty + th / 2.0
            if abs(tcx - cx) > self.row_px:
                continue
            out.append({"text": str(text), "rect_in_page": [int(tx), int(ty), int(tw), int(th)],
                        "offset": [int(tcx - cx), int(tcy - cy)]})
        if not out:
            return None
        out.sort(key=lambda n: abs(n["offset"][0]))
        return out[: self.max_nearby]


# --------------------------------------------------------------------- 组装


def build_recorder(ocr=True, hooker=None, **kw) -> R.Recorder:
    """组装一个能在真机上跑的操作录制器（抓帧走后台线程，绝不卡住输入）。"""
    pages = LivePages(ocr=ocr, **kw)
    return R.Recorder(hooker=hooker if hooker is not None else PynputHooker(),
                      grabr=capture.grab_screen, window_of=pages.window_of,
                      page_of=pages.page_of, widget_of=pages.widget_of,
                      grab_async=True)
