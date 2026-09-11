# -*- coding: utf-8 -*-
"""
engine.autofill — 按部件文字补齐目标（M4-WP5 配套）。

为什么需要它：一句话生成的步骤只带"部件文字"（云端**不许**给坐标，这是 M0 起就定的
边界）。要让它真能跑，还差"这些文字在屏幕上是哪一块"。这一步由**本地**完成：

  用户框一次操作页面 → 在本机对这张页面截图做 OCR → 按文字找到部件 → 生成
  图片/页内矩形/中心点（与手动"截图目标"产出的目标**完全同构**）

于是整条链是：**云端出意图（文字）→ 本地出坐标**。云端始终没有机会碰坐标。
找不到的部件如实列出来（不猜、不留一个跑不通的目标）。
"""
from __future__ import annotations

from engine import capture
from engine import matcher
from engine import schema


def _targets(st):
    if st.get("target"):
        yield st["target"]
    eo = st.get("expected_outcome") or {}
    if eo.get("target"):
        yield eo["target"]
    cond = st.get("condition") or {}
    if cond.get("target"):
        yield cond["target"]
    lp = st.get("loop") or {}
    if lp.get("target"):
        yield lp["target"]


def walk(steps):
    for st in steps or []:
        yield st
        if st.get("type") == "condition":
            yield from walk(st.get("then") or [])
            yield from walk(st.get("else") or [])
        elif st.get("type") == "loop":
            yield from walk(st.get("body") or [])


def needs_fill(target: dict) -> bool:
    """这个目标还缺"在屏幕上是哪一块"吗？"""
    if not isinstance(target, dict) or not (target.get("text") or "").strip():
        return False
    return not (target.get("image") or target.get("rect_in_page") or target.get("uia"))


def autofill(sg: dict, page: dict, opts=None) -> dict:
    """按文字补齐目标。page = capture.capture_page(hwnd) 的结果。

    返回 {ok, script, filled, missing, notes}：filled 是补好的部件文字，
    missing 是页面上没找到的（如实报出来，交给用户手动框）。
    """
    opts = dict(opts or {})
    # 注意：page["bgr"] 是 numpy 数组，不能直接当布尔用（会抛"truth value is ambiguous"）
    if not page or page.get("bgr") is None:
        return {"ok": False, "script": sg, "filled": [], "missing": [],
                "notes": ["请先框一次操作页面（点「截图目标」），我才能按文字找位置"]}
    page_bgr = page["bgr"]
    page_rect = page.get("rect") or (0, 0, 0, 0)
    spec = page.get("spec")
    ocr = matcher.ocr_run(page_bgr)
    if not ocr.get("ok"):
        return {"ok": False, "script": sg, "filled": [], "missing": [],
                "notes": ["页面看不太清（OCR 没成功），先确认目标窗口没有被遮挡、也没锁屏"]}
    tokens = list(zip(ocr["txts"], ocr["boxes"], ocr["scores"]))

    # 深拷贝：补的是副本，用户不满意可以整份丢掉
    import json
    out = json.loads(json.dumps(sg))
    filled, missing = [], []
    page_used = False
    for st in walk(out.get("steps")):
        for t in _targets(st):
            if not needs_fill(t):
                continue
            want = t["text"].strip()
            hit = _find(tokens, want)
            if not hit:
                missing.append(want)
                continue
            box = _pad_clamp(hit, page_bgr.shape, int(opts.get("pad", 6)))
            got = capture.capture_widget(page_bgr, list(box), text=want)
            if not page_used and spec is not None:
                t["page"] = spec          # 页面只挂一次，其余靠执行器继承
                page_used = True
            t.update({"image": got["image_dataurl"], "text": want,
                      "rect_in_page": got["rect_in_page"],
                      "center_in_page": got["center_in_page"]})
            filled.append(want)
    problems = schema.validate(out)
    notes = []
    if filled:
        notes.append(f"按文字找到并补好了 {len(filled)} 个部件：" + "、".join(filled))
    if missing:
        notes.append("页面上没找到这些文字（请手动框一下，或改个更准的说法）："
                     + "、".join(missing))
    if page_rect and filled:
        notes.append(f"坐标全部来自你框的那张页面截图（{page_rect}）本机算的，"
                     "云端没有参与")
    return {"ok": not problems and bool(filled), "script": out, "filled": filled,
            "missing": missing, "notes": notes, "problems": problems}


def _find(tokens, want: str):
    """在 OCR 结果里找这块文字：先精确/近似整串，再退到"短前缀"。"""
    best, best_sim = None, 0.0
    for txt, box, _sc in tokens:
        sim = matcher.text_similar(str(txt), want)
        if sim > best_sim:
            best, best_sim = box, sim
    if best is not None and best_sim >= 0.75:
        return best
    short = matcher.text_needle_short(want, 2)
    if len(short) >= 2 and short != want:
        best, best_sim = None, 0.0
        for txt, box, _sc in tokens:
            sim = matcher.text_similar(str(txt), short)
            if sim > best_sim:
                best, best_sim = box, sim
        if best is not None and best_sim >= 0.75:
            return best
    return None


def _pad_clamp(box, shape, pad):
    x, y, w, h = [int(v) for v in box]
    h_, w_ = int(shape[0]), int(shape[1])
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w_, x + w + pad), min(h_, y + h + pad)
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def pending_texts(sg: dict) -> list:
    """脚本里还缺坐标的部件文字（界面用来提示"还差几步没框"）。"""
    out = []
    for st in walk(sg.get("steps")):
        for t in _targets(st):
            if needs_fill(t) and t["text"].strip() not in out:
                out.append(t["text"].strip())
    return out
