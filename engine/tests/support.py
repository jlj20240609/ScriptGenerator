# -*- coding: utf-8 -*-
"""
engine.tests.support — 合成屏场景与替身驱动（离线，不触真实桌面）。

页面 = PIL 画的中文 UI 假页（白底深字，高对比，OCR 稳定）；
场景 = 若干“页面图像 + 屏幕摆放位置/缩放”，由 StateDriver 组合为整屏；
替换驱动（FakeDriver）与假人类（FakeHuman）用于解释器流程测试。
"""
from __future__ import annotations

import copy

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


def font(size=22):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def text_w(text, size=22):
    try:
        return int(np.ceil(font(size).getlength(text)))
    except Exception:
        return size * len(text)


def draw_text(img, x, y, text, size=22, fill=(20, 20, 20)):
    d = ImageDraw.Draw(img)
    d.text((x, y), text, font=font(size), fill=fill)
    return (x, y, text_w(text, size), int(size * 1.35))


def mk_canvas(w, h, bg):
    """建 BGR 画布。"""
    arr = np.full((h, w, 3), bg, np.uint8)
    return arr


def pil_to_bgr(pil_img):
    return np.asarray(pil_img)[:, :, ::-1].copy()


def make_page(label="", w=1000, h=640, bg=(250, 251, 253), header=(60, 90, 140),
              header_text="", deco=None):
    """
    画一页 UI：可选 header 色带 + 标题文字 + 自定义 deco(labels 绘制回调)。
    deco(draw: ImageDraw, page: Image) -> None 用于加按钮/色块。
    """
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    if header_text:
        d.rectangle((0, 0, w, 46), fill=header)
        draw_text(img, 18, 10, header_text, size=22, fill=(255, 255, 255))
    if deco:
        deco(d, img)
    if label:
        draw_text(img, 18, 60, label, size=20, fill=(120, 120, 120))
    return pil_to_bgr(img)


def paste_scale(page_bgr, screen, x, y, scale=1.0):
    """把 page 按 scale 摆进 screen（BGR canvas），返回实际占用矩形。"""
    import cv2
    h, w = page_bgr.shape[:2]
    if scale == 1.0:
        pw, ph = w, h
        small = page_bgr
    else:
        pw, ph = int(w * scale), int(h * scale)
        small = cv2.resize(page_bgr, (pw, ph), interpolation=cv2.INTER_AREA)
    x, y = int(x), int(y)
    screen[y:y + ph, x:x + pw] = small
    return (x, y, pw, ph)


def feed_topbar(w=1100):
    """动态页固定顶栏（浅底深字导航词），供 FeedScene 使用；返回 (顶栏 bgr, boxes)。"""
    top = Image.new("RGB", (w, 46), (226, 232, 240))
    d = ImageDraw.Draw(top)
    boxes = {}
    draw_text(top, 14, 11, "M1 动态工作台", size=18, fill=(40, 60, 90))
    x = 190
    for word in ("直播", "推荐", "热门", "影视"):
        boxes[word] = draw_text(top, x, 11, word, size=20, fill=(25, 40, 70))
        x += 110
    boxes["_clock"] = draw_text(top, w - 110, 13, "21:30:00", size=16,
                                fill=(120, 120, 120))
    return pil_to_bgr(top), boxes


def make_feed_frame(seed, w=1100, h=760):
    """动态页主区：每帧随机色块布局（模拟滚动 feed/内容区持续变化）。"""
    rng = np.random.default_rng(seed)
    frame = np.full((h, w, 3), (15, 18, 28), np.uint8)
    for _ in range(rng.integers(12, 20)):
        bw = int(rng.integers(60, 300))
        bh = int(rng.integers(50, 150))
        x = int(rng.integers(0, w - bw))
        y = int(rng.integers(60, h - bh))
        c = tuple(int(v) for v in rng.integers(30, 230, 3))
        frame[y:y + bh, x:x + bw] = c
    return frame


class FeedScene:
    """强动态页：固定顶栏 + 主区每帧变化（bili feed 型合成，帧间主区必失配）。"""

    def __init__(self, w=1100, h=760, top_h=46, frame0_seed=0):
        self.w, self.h, self.top_h = w, h, top_h
        top, self.boxes = feed_topbar(w)
        self.top = top
        self.frame0_seed = frame0_seed
        self.n = 0

    def screen(self):
        """合成整屏：顶栏（恒定）+ 主区（随帧号变化）。"""
        feed = make_feed_frame(self.frame0_seed + self.n, self.w, self.h - self.top_h)
        canvas = np.full((self.h, self.w, 3), (10, 10, 14), np.uint8)
        canvas[0:self.top_h, :, :] = self.top
        canvas[self.top_h:, :, :] = feed
        return np.ascontiguousarray(canvas)

    def provider(self):
        return self.screen()

    def next_frame(self):
        self.n += 1
        return self.screen()

    def widget_target(self, word="热门", page_spec=None):
        """顶栏导航词 widget（页面内坐标）。"""
        from engine.tests.support import widget_target as _wt
        if page_spec is None:
            import engine.schema as _sc
            import engine.matcher as _m
            page_spec = _sc.page_spec(image_dataurl=_m.bgr_to_dataurl(self.screen()),
                                      size=(self.w, self.h))
        bx = self.boxes[word]
        # 裁词框（多扩 6px 抗描边）
        x, y, w, h = bx
        x, y = max(0, x - 6), max(0, y - 6)
        w, h = w + 12, h + 12
        frame = self.screen()
        crop = frame[y:y + h, x:x + w]
        import engine.schema as _sc
        import engine.matcher as _m
        return {"page": page_spec, "image": _m.bgr_to_dataurl(crop),
                "text": word, "match": "auto",
                "rect_in_page": [x, y, w, h],
                "center_in_page": [x + w // 2, y + h // 2]}


def widget_rect_of(text, x, y, size=22):
    """按文字位置估算部件框（目标矩形用于采集/记录）。"""
    return [x - 4, y - 4, text_w(text, size) + 8, int(size * 1.35) + 8]


def page_spec_of(bgr, rect=None, context=None):
    """页面 BGR + 采集屏幕矩形 → PageSpec dict（含内嵌 image data-url）。"""
    import engine.matcher as m
    import engine.schema as sc
    h, w = bgr.shape[:2]
    return sc.page_spec(image_dataurl=m.bgr_to_dataurl(bgr), size=(w, h),
                        context=context or {}, rect_in_screen=rect)


def widget_target(page_bgr, page_spec, box_xywh, text="", match="auto",
                  semantic="", include_image=True, include_text=True,
                  box_as_rect=None):
    """
    从页面图中造部件 Target：按框裁剪为 image data-url + 记录页内坐标。
    box_xywh: 页内 (x,y,w,h)（通常来自页面构建器的 boxes 返回值）。
    """
    import engine.matcher as m
    import engine.schema as sc
    x, y, w, h = [int(v) for v in box_xywh]
    crop = page_bgr[y:y + h, x:x + w]
    t = {"page": page_spec}
    if include_image:
        t["image"] = m.bgr_to_dataurl(crop)
    if include_text and text:
        t["text"] = text
    if semantic:
        t["semantic"] = semantic
    if match != "auto":
        t["match"] = match
    rect = box_as_rect if box_as_rect is not None else [x, y, w, h]
    t["rect_in_page"] = [int(v) for v in rect]
    t["center_in_page"] = [x + w // 2, y + h // 2]
    return t


def action_step(sid, act, target=None, params=None, expected_outcome=None):
    st = {"id": sid, "type": "action", "action": act}
    if target is not None:
        st["target"] = target
    if params:
        st["params"] = params
    if expected_outcome is not None:
        st["expected_outcome"] = expected_outcome
    return st


def condition_step(sid, target, exists=True, then=None, else_=None):
    return {"id": sid, "type": "condition",
            "condition": {"target": target, "exists": bool(exists)},
            "then": then or [], "else": else_ or []}


def loop_step(sid, loop, body=None):
    return {"id": sid, "type": "loop", "loop": loop, "body": body or []}


def script(name, steps):
    import engine.schema as sc
    sg = sc.new_script(name)
    sg["steps"] = steps
    return sg


def _b64(bgr):
    import engine.matcher as m
    return m.bgr_to_dataurl(bgr)


# ---------------------------------------------------------------- 演示页（登录/首页/改版页）

def login_page():
    """登录页：用户名/密码输入框(灰框) + 登录按钮(浅底深字)。返回 (bgr, boxes dict)"""
    boxes = {}

    def deco(d, img):
        d.rectangle((80, 130, 560, 620), fill=(255, 255, 255), outline=(210, 215, 225))
        boxes["user_label"] = draw_text(img, 120, 200, "用户名", size=22)
        d.rectangle((120, 235, 520, 295), fill=(255, 255, 255), outline=(150, 155, 165))
        boxes["user_box"] = (120, 235, 400, 60)
        boxes["pwd_label"] = draw_text(img, 120, 330, "密码", size=22)
        d.rectangle((120, 365, 520, 425), fill=(255, 255, 255), outline=(150, 155, 165))
        boxes["pwd_box"] = (120, 365, 400, 60)
        d.rectangle((120, 460, 380, 524), fill=(228, 233, 242), outline=(120, 130, 150))
        boxes["login_btn"] = draw_text(img, 216, 477, "登录", size=24, fill=(30, 60, 130))
        d.text((120, 560), "M1 演示：登录后进入首页", font=font(16), fill=(170, 170, 170))

    return make_page(header_text="自动登录 · 演示页", deco=deco), boxes


def home_page():
    """首页（登录成功后）：浅灰侧栏 + 深色菜单文字 + 欢迎语。"""
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 46, 240, 640), fill=(226, 232, 240))
        boxes["menu"] = draw_text(img, 44, 130, "库存查询", size=24, fill=(30, 40, 60))
        d.rectangle((280, 90, 940, 500), fill=(240, 252, 240))
        boxes["welcome"] = draw_text(img, 320, 150, "登录成功", size=30, fill=(20, 110, 50))
        draw_text(img, 320, 220, "欢迎使用进销存管理台", size=22)

    return make_page(bg=(246, 249, 244), header_text="进销存管理台", header=(30, 120, 70),
                     deco=deco), boxes


def home_page_v2():
    """改版首页：侧栏中棕（模板必失配）+ 菜单改名“库存中心”。"""
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 46, 240, 640), fill=(163, 142, 118))
        boxes["menu"] = draw_text(img, 44, 150, "库存中心", size=24, fill=(45, 30, 12))
        d.rectangle((280, 90, 940, 500), fill=(252, 247, 240))
        boxes["welcome"] = draw_text(img, 320, 170, "登录成功", size=28, fill=(150, 100, 30))
        draw_text(img, 320, 240, "欢迎使用新版管理台", size=22)

    return make_page(bg=(252, 250, 245), header_text="进销存管理台 V2", header=(150, 110, 30),
                     deco=deco), boxes


def erp_v1_page():
    """ERP v1（录制基准页）：浅蓝灰底 + 顶栏 + 左菜单“库存查询”（不同配色/布局族）。"""
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 0, 1000, 52), fill=(45, 60, 90))
        draw_text(img, 18, 12, "进销存管理台", size=22, fill=(255, 255, 255))
        d.rectangle((0, 52, 190, 640), fill=(205, 214, 228))
        boxes["menu"] = draw_text(img, 34, 190, "库存查询", size=24, fill=(30, 45, 70))
        d.rectangle((230, 90, 960, 190), fill=(255, 255, 255), outline=(200, 205, 214))
        draw_text(img, 250, 115, "功能工具栏", size=20, fill=(60, 60, 60))
        d.rectangle((230, 210, 960, 600), fill=(255, 255, 255), outline=(210, 214, 220))
        draw_text(img, 250, 230, "数据表格区域", size=20, fill=(90, 90, 90))

    return make_page(bg=(238, 240, 246), deco=deco), boxes


def erp_v2_renamed_page():
    """ERP 彻底改名页：菜单与表格内容不含旧词前缀（用于“校准失败保留旧值”场景）。"""
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 0, 1000, 52), fill=(45, 60, 90))
        draw_text(img, 18, 12, "进销存管理台", size=22, fill=(255, 255, 255))
        d.rectangle((0, 52, 190, 640), fill=(205, 214, 228))
        d.rectangle((0, 186, 6, 234), fill=(60, 90, 140))
        d.rectangle((6, 186, 190, 234), fill=(180, 200, 226))
        boxes["menu"] = draw_text(img, 34, 196, "出库记录", size=24, fill=(20, 40, 70))
        d.rectangle((230, 90, 960, 190), fill=(255, 255, 255), outline=(200, 205, 214))
        draw_text(img, 250, 115, "功能工具栏", size=20, fill=(60, 60, 60))
        d.rectangle((230, 210, 960, 600), fill=(255, 255, 255), outline=(210, 214, 220))
        draw_text(img, 250, 230, "数据表格区域", size=20, fill=(90, 90, 90))
        draw_text(img, 250, 268, "出库台账 行", size=20, fill=(120, 60, 60))

    return make_page(bg=(238, 240, 246), deco=deco), boxes


def erp_v2_page():
    """ERP v2（改版现场）：暖色底/深顶栏 + 菜单改名“库存中心”+ 布局微移（模板显著失配）。"""
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 0, 1000, 52), fill=(120, 80, 30))
        draw_text(img, 18, 12, "进销存管理台 V2", size=22, fill=(255, 255, 255))
        d.rectangle((0, 52, 190, 640), fill=(176, 150, 118))
        boxes["menu"] = draw_text(img, 34, 205, "库存中心", size=24, fill=(50, 32, 10))
        d.rectangle((230, 90, 960, 190), fill=(255, 250, 240), outline=(210, 190, 160))
        draw_text(img, 250, 118, "功能工具栏", size=20, fill=(80, 70, 50))
        d.rectangle((230, 212, 960, 600), fill=(255, 250, 240), outline=(215, 198, 170))
        draw_text(img, 250, 232, "数据表格区域（新版）", size=20, fill=(110, 96, 70))

    return make_page(bg=(248, 243, 232), deco=deco), boxes


def erp_v2_small_page():
    """
    ERP 小改版页（真实 ERP v1→v2 形态）：配色/布局几乎不变（整窗模板仍命中），
    仅“库存查询”菜单项改名“库存中心”并换了行内高亮样式（旧部件文字+模板双失配）。
    """
    boxes = {}

    def deco(d, img):
        d.rectangle((0, 0, 1000, 52), fill=(45, 60, 90))
        draw_text(img, 18, 12, "进销存管理台", size=22, fill=(255, 255, 255))
        d.rectangle((0, 52, 190, 640), fill=(205, 214, 228))
        # 菜单行高亮样式（与 v1 明显不同 → 旧 crop 模板失配）
        d.rectangle((0, 186, 6, 234), fill=(60, 90, 140))
        d.rectangle((6, 186, 190, 234), fill=(180, 200, 226))
        boxes["menu"] = draw_text(img, 34, 196, "库存中心", size=24, fill=(20, 40, 70))
        d.rectangle((230, 90, 960, 190), fill=(255, 255, 255), outline=(200, 205, 214))
        draw_text(img, 250, 115, "功能工具栏", size=20, fill=(60, 60, 60))
        d.rectangle((230, 210, 960, 600), fill=(255, 255, 255), outline=(210, 214, 220))
        draw_text(img, 250, 230, "数据表格区域", size=20, fill=(90, 90, 90))
        draw_text(img, 250, 268, "库存中心 台账行", size=20, fill=(120, 60, 60))

    return make_page(bg=(238, 240, 246), deco=deco), boxes


def scene_of(page_bgr, x, y, scale=1.0, canvas_w=1400, canvas_h=1000, bg=(24, 24, 28)):
    """整屏：灰底 + 一页摆到 (x,y)。返回 (canvas, page_rect)。"""
    screen = mk_canvas(canvas_w, canvas_h, bg)
    rect = paste_scale(page_bgr, screen, x, y, scale)
    return screen, rect


# ---------------------------------------------------------------- 替身驱动

class FakeHuman:
    """记录交互；可按队列自动作答。"""

    def __init__(self, answers=None):
        self.notified = []
        self.not_found_calls = []
        self.outcome_calls = []
        self.answers = answers or []        # 顺序弹出；缺省 = continue
        self.on_not_found = None            # 可选回调：收到 L1 提示时可切换场景

    def _pop(self):
        if self.answers:
            return self.answers.pop(0)
        return "continue"

    def notify(self, message):
        self.notified.append(message)

    def prompt_not_found(self, message, target_text):
        self.not_found_calls.append(message)
        if self.on_not_found:
            self.on_not_found(message)
        return self._pop()

    def prompt_outcome_fail(self, message):
        self.outcome_calls.append(message)
        return self._pop()


class FakeDriver:
    """合成屏驱动：screen_provider 出整屏 bgr；click 记录并可触发场景切换。"""

    def __init__(self, screen_provider, on_click=None, dpi=120):
        self.provider = screen_provider
        self.on_click = on_click
        self.clicks = []          # (x, y, dbl)
        self.typed = []
        self.hotkeys = []
        self.slept = 0.0
        self.sleeps = []
        self.dpi = dpi

    def grab_screen(self):
        bgr = self.provider()
        h, w = bgr.shape[:2]
        return np.ascontiguousarray(bgr), {"width": w, "height": h, "dpi": self.dpi}

    def grab_rect(self, rect):
        bgr, _ = self.grab_screen()
        x, y, w, h = [int(v) for v in rect]
        hh, ww = bgr.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(ww, x + w), min(hh, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return bgr[y0:y1, x0:x1]

    def click(self, x, y, dbl=False):
        self.clicks.append((int(x), int(y), bool(dbl)))
        if self.on_click:
            self.on_click(int(x), int(y), bool(dbl))

    def type_text(self, text):
        self.typed.append(text)

    def clear_text(self):
        """清空输入框（`输入文字` 动作用前会调）：记次数，供离线用例观察。"""
        self.cleared = getattr(self, "cleared", 0) + 1

    def hotkey(self, keys):
        self.hotkeys.append(keys)

    def sleep(self, seconds):
        self.slept += float(seconds)
        self.sleeps.append(float(seconds))

    def raise_if_needed(self):
        return None


class StatefulScene:
    """
    场景状态机：多个具名页面；state 决定整屏内容与可点区域。
    hit_zones: {zone_name: {x,y,w,h, state, to}} 或 测试自定义 transform(state, x, y)。
    """

    def __init__(self, pages, placement, canvas=(1400, 1000), bg=(24, 24, 28),
                 initial="page1"):
        # pages: {name: (bgr, scale 参考)}, placement: {name: (x, y, scale)}
        self.pages = pages
        self.placement = placement
        self.canvas = canvas
        self.bg = bg
        self.state = initial
        self.zones = {}           # name -> {x,y,w,h(页内), to}
        self.clicks = []
        self.extra_transform = None

    def add_zone(self, name, page_rect_in_page, to):
        self.zones[name] = {"rect": page_rect_in_page, "to": to}

    def screen(self):
        bgr_name = self.state
        if isinstance(bgr_name, tuple):       # (name, page_offset_x, page_offset_y, scale)
            name, ox, oy, sc = bgr_name
        else:
            name, ox, oy, sc = bgr_name, *self.placement[bgr_name]
        page = self.pages[name]
        if isinstance(page, tuple):
            page = page[0]
        screen = mk_canvas(self.canvas[0], self.canvas[1], self.bg)
        rect = paste_scale(page, screen, ox, oy, sc)
        return screen, rect

    def provider(self):
        return self.screen()[0]

    def on_click(self, x, y, dbl=False):
        self.clicks.append((x, y, dbl))
        if self.extra_transform:
            self.state = self.extra_transform(self.state, x, y) or self.state
            return
        # 页内命中 zone → 转场
        screen, rect = self.screen()
        cur = self.state
        if isinstance(cur, tuple):
            cur = cur[0]
        bx, by, bw, bh = rect
        for zname, z in self.zones.items():
            rx, ry, rw, rh = z["rect"]
            cx = bx + rx + rw / 2
            cy = by + ry + rh / 2
            if abs(x - cx) <= max(30, rw) and abs(y - cy) <= max(30, rh):
                self.state = z["to"]
                return
