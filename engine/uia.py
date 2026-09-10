# -*- coding: utf-8 -*-
"""
engine.uia — ①级 UI 树定位（Windows UI Automation，真实窗口）。

迁移自 M0（m0lib.uia_walk_find_text），波次2 接入 locator 的 uia_provider 槽：
  - uia_walk_find_text(hwnd, text) -> {ok, hits[...], nodes, elapsed_ms}
    hits 元素 {name, rect(屏幕绝对), center, type, automation_id, sim}
  - make_provider(hwnd) -> provider(text)->hits（供 locator.locate_widget(..., uia_provider=…)）

约束（v0.31 §5.1/§7.3）：UI 树为空/不可用 → 调用方（locator）直接跳过本级不算失败；
命中必须落在已锁定页面范围内（locator 过滤）+ 同名多实例按“离录点最近”消歧。
"""
from __future__ import annotations

import time

from engine import matcher


def uia_walk_find_text(hwnd, text, max_nodes=9000, max_ms=4000.0) -> dict:
    """
    在 hwnd 的 UI 树内找 Name 与 text 相似(>=0.8) 的控件。
    返回 {ok, hits:[{name, rect, center, type, automation_id, sim}, ...(≤6, 树序)],
          nodes, elapsed_ms}
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
        if depth > 40 or time.perf_counter() > deadline or visited >= max_nodes \
                or len(hits) >= 6:
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
                sim = matcher.text_similar(name, target_text)
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
                                     "center": (rect[0] + rect[2] // 2,
                                                rect[1] + rect[3] // 2),
                                     "type": type(c).__name__, "automation_id": aid,
                                     "sim": round(sim, 3)})
                        continue
            walk(c, depth + 1)

    walk(root, 0)
    if not hits:
        return {"ok": False, "hits": [], "nodes": visited,
                "elapsed_ms": (time.perf_counter() - t0) * 1000,
                "reason": "timeout" if time.perf_counter() > deadline else "not_found"}
    return {"ok": True, "hits": hits, "nodes": visited,
            "elapsed_ms": (time.perf_counter() - t0) * 1000}


def make_provider(hwnd):
    """hwnd → uia_provider(text)->hits（供 locator；hwnd 失效时返回空列表）。"""
    import win32gui

    def provider(text):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return []
        try:
            u = uia_walk_find_text(hwnd, text)
            return u.get("hits", []) if u.get("ok") else []
        except Exception:
            return []
    return provider


def hit_test(x, y) -> dict:
    """屏幕物理坐标点 → UIA 命中控件身份（§5.1 第二次截图的 UI 树采集）。"""
    import uiautomation as auto
    try:
        e = auto.ControlFromPoint(int(x), int(y))
    except Exception:
        return {}
    if e is None:
        return {}
    try:
        r = e.BoundingRectangle
        rect = [int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)]
    except Exception:
        rect = None
    try:
        name = e.Name or ""
    except Exception:
        name = ""
    try:
        aid = e.AutomationId or ""
    except Exception:
        aid = ""
    return {"name": name, "automation_id": aid, "type": type(e).__name__,
            "rect": rect}
