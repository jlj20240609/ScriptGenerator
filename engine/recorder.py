# -*- coding: utf-8 -*-
"""
engine.recorder — 操作录制（M3-WP1）。

分两层，故意分开：

  第一层（纯逻辑，无屏无钩子）：**事件流 → 积木**。
    录制的可用性几乎全在这一步——打字必须聚合成一块、修饰键不能凭空生块、
    无感停顿要变成「等一下」。做成纯函数就能离线钉住，不必反复真机录。

  第二层（Recorder）：挂钩子收事件、跟着抓页面的图、把积木变成步骤。
    钩子和抓屏都靠注入，所以第二层的骨架同样能离线验证。

事件格式（钩子负责填准，录制器只消费）：
  {"t": 秒, "kind": "click",  "x":, "y":, "button": "left", "double": bool}
  {"t": 秒, "kind": "key",    "key": "a"|"enter"|"ctrl_l", "mods": ["ctrl"], "at": (x,y)|None}
  {"t": 秒, "kind": "scroll", "x":, "y":, "dx":, "dy":}
其中 `mods` 必须是钩子在按下时刻的**真实**修饰键状态——录制器不猜：
光按下 Ctrl 什么都记不下来，但 Ctrl+S 会记成快捷键而不是「输入 s」。
"""
from __future__ import annotations

import re
import time

# --------------------------------------------------------------------- 积木种类

B_CLICK = "click"
B_DBLCLICK = "dblclick"
B_TYPE = "type"
B_HOTKEY = "hotkey"
B_WAIT = "wait"
B_UNSUPPORTED = "unsupported"        # 记不下来又不该丢的操作（滚轮、单独的功能键）

EV_CLICK = "click"
EV_KEY = "key"
EV_SCROLL = "scroll"

# --------------------------------------------------------------------- 判定阈值

DOUBLE_CLICK_PX = 6                  # 两次点击的位移不超过这个数才算「同一点」
DOUBLE_CLICK_S = 0.35                # 两次点击的最长间隔
TYPE_GAP_S = 0.9                     # 连续输入的字符间隔；超过就切成两块
BLOCK_GAP_S = 1.2                    # 操作之间的停顿；超过就插一块「等一下」

# --------------------------------------------------------------------- 键名表

MODIFIER_KEYS = {
    "ctrl", "ctrl_l", "ctrl_r", "control", "control_l", "control_r",
    "alt", "alt_l", "alt_r", "alt_gr", "option",
    "shift", "shift_l", "shift_r",
    "cmd", "cmd_l", "cmd_r", "win", "win_l", "win_r",
    "super", "super_l", "super_r", "meta", "meta_l", "meta_r",
}

MOD_ALIAS = {
    "control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "option": "alt",
    "shift_l": "shift", "shift_r": "shift",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win",
    "win_l": "win", "win_r": "win",
    "super": "win", "super_l": "win", "super_r": "win",
    "meta": "win", "meta_l": "win", "meta_r": "win",
}
MOD_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}

FUNC_KEYS = {
    "enter", "return", "tab", "esc", "escape", "backspace", "delete", "del",
    "insert", "ins", "home", "end", "page_up", "pageup", "page_down", "pagedown",
    "up", "down", "left", "right", "caps_lock", "num_lock", "scroll_lock",
    "print_screen", "prtsc", "pause", "break", "menu", "apps",
}

# 键名 → 真正落进输入框的字符（"space" 不处理就会把 "hello world" 录成 "helloworld"）
CHAR_ALIAS = {"space": " "}

_FUNC_KEY_RE = re.compile(r"f([1-9]|1[0-9]|2[0-4])")


def classify_key(key) -> str:
    """键名分类：char / func / modifier / unknown。"""
    k = str(key or "").strip().lower()
    if not k:
        return "unknown"
    if k in MODIFIER_KEYS:
        return "modifier"
    if k in CHAR_ALIAS:
        return "char"
    if k in FUNC_KEYS:
        return "func"
    if len(k) == 1:
        return "char"
    if _FUNC_KEY_RE.fullmatch(k):
        return "func"
    return "unknown"


def _norm_mod(m) -> str:
    m = str(m or "").strip().lower()
    return MOD_ALIAS.get(m, m)


# --------------------------------------------------------------------- 事件 → 积木


def blocks_from_events(events, double_click_s: float = DOUBLE_CLICK_S,
                       type_gap_s: float = TYPE_GAP_S,
                       block_gap_s: float = BLOCK_GAP_S) -> list:
    """把钩子事件流切成积木。纯函数：给定同样的事件，结果完全一样。"""
    blocks: list = []
    cur_type = None                  # 未收口的「输入文字」块
    last_click_at = None
    last_click_block = None
    last_click_t = None
    last_t = None

    def flush():
        nonlocal cur_type
        if cur_type is not None:
            b = {k: v for k, v in cur_type.items() if not k.startswith("_")}
            if b["params"].get("text"):
                blocks.append(b)
            cur_type = None

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        if kind not in (EV_CLICK, EV_KEY, EV_SCROLL):
            continue                      # 鼠标移动之类的高频事件直接忽略
        t = float(ev.get("t") or 0.0)

        # ---- 先处理停顿：无论下一条是什么，长间隔都要先收口并插「等一下」
        if last_t is not None and (t - last_t) > block_gap_s:
            flush()
            blocks.append({"action": B_WAIT, "at": None,
                           "params": {"seconds": round(t - last_t, 2)}})
        last_t = t

        if kind == EV_CLICK:
            flush()
            x, y = int(ev.get("x") or 0), int(ev.get("y") or 0)
            merged = (blocks and blocks[-1] is last_click_block
                      and blocks[-1]["action"] == B_CLICK
                      and last_click_t is not None
                      and (t - last_click_t) <= double_click_s
                      and abs(x - last_click_at[0]) <= DOUBLE_CLICK_PX
                      and abs(y - last_click_at[1]) <= DOUBLE_CLICK_PX)
            if merged:
                blocks[-1] = {"action": B_DBLCLICK, "at": (x, y),
                              "params": dict(blocks[-1]["params"])}
                last_click_block = blocks[-1]
            else:
                blocks.append({"action": B_CLICK, "at": (x, y),
                               "params": {"button": str(ev.get("button") or "left")}})
                last_click_block = blocks[-1]
            last_click_at = (x, y)
            last_click_t = t
            continue

        if kind == EV_SCROLL:
            flush()
            x, y = int(ev.get("x") or 0), int(ev.get("y") or 0)
            blocks.append({"action": B_UNSUPPORTED, "at": (x, y),
                           "params": {"kind": "scroll", "dx": ev.get("dx"),
                                      "dy": ev.get("dy"),
                                      "why": "滚轮在现有动作里没有对应项"}})
            continue

        # ---- 按键
        k = str(ev.get("key") or "").strip().lower()
        cls = classify_key(k)
        if cls == "modifier":
            continue                      # 光按修饰键不生成任何积木
        mods = sorted({_norm_mod(m) for m in (ev.get("mods") or []) if m},
                      key=lambda m: (MOD_ORDER.get(m, 9), m))
        ev_at = ev.get("at")
        at = tuple(ev_at) if ev_at else last_click_at

        if mods:
            flush()
            blocks.append({"action": B_HOTKEY, "at": at,
                           "params": {"keys": "+".join(list(mods) + [k])}})
            continue

        if cls == "char":
            if cur_type is not None and (t - cur_type["_last_t"]) > type_gap_s:
                flush()
            if cur_type is None:
                cur_type = {"action": B_TYPE, "at": at, "params": {"text": ""},
                            "_last_t": t}
            cur_type["params"]["text"] += CHAR_ALIAS.get(k, k)
            cur_type["_last_t"] = t
            continue

        # 功能键 / 认不出的键：现有动作里没有对应项，如实留痕而不是硬塞
        flush()
        why = ("按键「%s」在现有动作里没有对应项" % k) if k else "认不出的按键"
        blocks.append({"action": B_UNSUPPORTED, "at": at,
                       "params": {"kind": "key", "key": k, "why": why}})

    flush()
    return blocks


# --------------------------------------------------------------------- 白话描述


def describe(b: dict) -> str:
    """把一个积木说成人话——录制界面直接给用户看这句。"""
    a = b.get("action")
    p = b.get("params") or {}
    if a == B_CLICK:
        btn = p.get("button") or "left"
        return "点一下" if btn == "left" else f"点一下（{btn} 键）"
    if a == B_DBLCLICK:
        return "点两下"
    if a == B_TYPE:
        return "输入文字「%s」" % (p.get("text") or "")
    if a == B_HOTKEY:
        return "按快捷键 %s" % (p.get("keys") or "")
    if a == B_WAIT:
        return "等一下 %s 秒" % (p.get("seconds"),)
    if a == B_UNSUPPORTED:
        k = p.get("kind")
        if k == "scroll":
            dy = p.get("dy")
            if dy is None:
                return "滚轮（暂不支持，仅留痕）"
            return "滚轮%s（暂不支持，仅留痕）" % ("向下" if float(dy) < 0 else "向上")
        if k == "key":
            return "按键「%s」（暂不支持，仅留痕）" % (p.get("key") or "?")
        return "这一步暂不支持（仅留痕）"
    return "未知操作"


def summarize(blocks) -> dict:
    """统计：总共几步、能用几步、几步只留痕。"""
    blocks = list(blocks or [])
    counts: dict = {}
    for b in blocks:
        counts[b.get("action")] = counts.get(b.get("action"), 0) + 1
    unsupported = counts.get(B_UNSUPPORTED, 0)
    return {
        "total": len(blocks),
        "usable": len(blocks) - unsupported,
        "unsupported": unsupported,
        "waits": counts.get(B_WAIT, 0),
        "clicks": counts.get(B_CLICK, 0),
        "dblclicks": counts.get(B_DBLCLICK, 0),
        "types": counts.get(B_TYPE, 0),
        "hotkeys": counts.get(B_HOTKEY, 0),
        "texts": [b["params"].get("text", "") for b in blocks if b.get("action") == B_TYPE],
        "lines": [describe(b) for b in blocks],
    }


# --------------------------------------------------------------------- 录制器


class Recorder:
    """操作录制器：挂全局钩子收事件 → 切积木 → 抓双截图 → 出步骤。

    参数（都可注入，默认 None 表示这段能力本次不可用）：
      hooker   : 全局钩子后端，需有 start(cb)/stop()（真机见 PynputHooker）
      grabr    : 抓屏，签名 grabr() -> bgr（默认 engine.capture.grab_screen）
      page_of  : (x, y) -> (page_rect, page_bgr, spec)：把屏幕点变成「操作页面」
      widget_of: (x, y, page_rect, page_bgr, spec) -> target：把点变成部件（OCR 反查）
      clock    : 取时间（测试可注入假时钟）

    一个关键性能设计：**抓帧在事件发生时立刻做，OCR 反查留到停止之后批量做**。
    录制中每步都 OCR 会卡到不可用（单次 0.5s 级），而抓一帧只要几十毫秒；
    所以录制时只存帧，停下后再统一反查「点的是什么」。
    """

    def __init__(self, hooker=None, grabr=None, page_of=None, widget_of=None,
                 clock=time.time):
        self.hooker = hooker
        self.grabr = grabr
        self.page_of = page_of
        self.widget_of = widget_of
        self.clock = clock
        self.events: list = []
        self.frames: dict = {}           # (x, y) -> 帧（录制时立刻抓，避免卡顿）
        self._t0 = 0.0
        self._running = False

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> dict:
        if self._running:
            return {"ok": False, "note": "已经在录制了"}
        if self.hooker is None:
            return {"ok": False, "note": "没有可用的按键/鼠标监听（钩子未注入）"}
        self.events = []
        self.frames = {}
        self._t0 = self.clock()
        self._running = True
        try:
            self.hooker.start(self._on_event)
        except Exception as e:
            self._running = False
            return {"ok": False, "note": f"监听启动失败：{e!r}"}
        return {"ok": True, "note": "开始录制"}

    def stop(self) -> dict:
        if not self._running:
            return {"ok": False, "note": "当前没有在录制"}
        try:
            if self.hooker is not None:
                self.hooker.stop()
        except Exception:
            pass
        self._running = False
        blocks = blocks_from_events(self.events)
        return {"ok": True, "blocks": blocks, "summary": summarize(blocks),
                "elapsed_s": round(self.clock() - self._t0, 1),
                "frames": len(self.frames)}

    @property
    def running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------- 收事件

    def _on_event(self, ev: dict) -> None:
        """钩子回调：记事件 + **立刻**抓一帧（只在点击/滚轮处抓，不逐字抓）。"""
        if not self._running:
            return
        e = dict(ev or {})
        e["t"] = round(self.clock() - self._t0, 3)
        self.events.append(e)
        if e.get("kind") in (EV_CLICK, EV_SCROLL) and self.grabr is not None:
            self._grab_frame(int(e.get("x") or 0), int(e.get("y") or 0))

    def _grab_frame(self, x: int, y: int) -> None:
        key = (x, y)
        if key in self.frames:
            return
        try:
            self.frames[key] = self.grabr()
        except Exception:
            pass

    # ---------------------------------------------------------------- 出步骤

    def steps(self, page_provider=None) -> dict:
        """积木 → schema 步骤（录制停止后调用）。

        page_provider: (x, y) -> (page_rect, page_bgr, spec)，给每个动作找「操作页面」；
          不传就用构造时注入的 page_of。取不到页面/部件的块会跳过并列在 notes 里，
          绝不生成一个假装能用的步骤。
        """
        page_provider = page_provider or self.page_of
        if self._running:
            res = self.stop()
        else:
            res = {"ok": True, "blocks": blocks_from_events(self.events),
                   "summary": summarize(blocks_from_events(self.events))}
        blocks = res.get("blocks") or []
        steps, skipped, notes = [], [], []
        counter = 0
        for b in blocks:
            act = b.get("action")
            if act == B_UNSUPPORTED:
                skipped.append(b)
                notes.append(f"{describe(b)}：{b.get('params', {}).get('why') or '不支持'}")
                continue
            if act == B_WAIT:
                counter += 1
                steps.append({"id": f"r{counter}", "type": "action", "action": "wait",
                              "params": {"seconds": b["params"].get("seconds", 1)}})
                continue
            if act == B_HOTKEY:
                counter += 1
                steps.append({"id": f"r{counter}", "type": "action", "action": "hotkey",
                              "params": {"keys": b["params"].get("keys", "")}})
                continue
            # 点击/双击/输入：必须有部件，否则这一步没意义
            target = None
            at = b.get("at")
            if at and self.widget_of is not None and page_provider is not None:
                try:
                    target = self._widget_at(int(at[0]), int(at[1]), page_provider)
                except Exception as e:
                    notes.append(f"{describe(b)}：部件没认出来（{e!r}）")
            if target is None:
                skipped.append(b)
                notes.append(f"{describe(b)}：没拿到页面/部件，已跳过")
                continue
            counter += 1
            st = {"id": f"r{counter}", "type": "action", "action": act,
                  "params": {}, "target": target}
            if act == B_TYPE:
                st["params"]["text"] = b["params"].get("text", "")
            steps.append(st)
        return {"ok": True, "steps": steps, "skipped": skipped, "notes": notes,
                "summary": res.get("summary")}

    def _widget_at(self, x: int, y: int, page_provider):
        """把屏幕点变成部件 target：先取「操作页面」，再在页面里反查点的是什么。"""
        page = page_provider(x, y)
        if not page:
            return None
        rect, bgr, spec = page
        return self.widget_of(x, y, rect, bgr, spec)
