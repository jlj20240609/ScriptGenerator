# -*- coding: utf-8 -*-
"""
engine.locator — E2：页面定位 + 页内部件三级定位。

规格：《M1_引擎设计清单》§2/E2、§3 状态机；《项目分析文档》v0.31 §7.3。
约束铁律：部件搜索/点击永不越出已锁定页面范围（负例回归在 engine/tests）。

locate_page：整窗模板 0.8~1.25×（位置无关）→ 失败走静态锚（页面原点 = 锚 − 录制偏移）。
locate_widget：页内三级 ① UI 树（无提供者直接跳过，不算失败）→ ② 部件相似度
（文字 OCR 条带优先 + 模板，按 match 偏好融合）→ ③ 页面内坐标（几何兜底，仅当
② 明确失败；exists 语义下 ③ 不参与——“坐标仍在 ≠ 看到”）。
"""
from __future__ import annotations

import time

from engine import matcher
from engine.errors import EngineError, ERRORS

# 阈值默认值（M0 实测校准：页面 0.72、模板 0.70、文本 0.75）
PAGE_SCORE_MIN = 0.72
PAGE_SIM_MIN = 0.82          # 页面命中后的像素同源确认（thr=16；假阳性防护，波次1 定案）
ANCHOR_SCORE_MIN = 0.75
TPL_SCORE_MIN = 0.70
TEXT_SIM_MIN = 0.75

# 定位日志 method 词汇（清单 §4）
M_PAGE_TPL = "page_tpl"
M_ANCHOR = "anchor"
M_UIA = "uia"
M_OCR_TEXT = "ocr_text"
M_TPL = "tpl"
M_PAGE_COORD = "page_coord"


class LocConfig:
    def __init__(self, page_score_min=PAGE_SCORE_MIN, page_sim_min=PAGE_SIM_MIN,
                 anchor_score_min=ANCHOR_SCORE_MIN,
                 tpl_score_min=TPL_SCORE_MIN, text_sim_min=TEXT_SIM_MIN,
                 full_page_ocr=False):
        self.page_score_min = page_score_min
        self.page_sim_min = page_sim_min
        self.anchor_score_min = anchor_score_min
        self.tpl_score_min = tpl_score_min
        self.text_sim_min = text_sim_min
        self.full_page_ocr = full_page_ocr


# ---------------------------------------------------------------- 页面定位

def locate_page(screen_bgr, page_spec, cfg=None, prev_hint=None):
    """
    页面定位主路径：整窗模板 0.8~1.25×（页面被移动/缩放均可）→ 失败走静态锚。
    prev_hint: 上一帧页面矩形 (x,y,w,h)。命中路径会先做同矩形快速复验（1 尺度 + 同源），
    通过则以 page_tpl 记 reused=True（多步连续运行免重复全屏多尺度扫描）。
    返回 {ok, rect, method, confidence, scale, elapsed_ms, reused?, detail}
    """
    cfg = cfg or LocConfig()
    t0 = time.perf_counter()
    detail = {}
    tpl = matcher.dataurl_to_bgr(page_spec["image"])
    h_screen, w_screen = screen_bgr.shape[:2]
    size = page_spec.get("size") or [tpl.shape[1], tpl.shape[0]]

    def _confirm_same_source(hit_rect, tpl_img, min_sim):
        """整窗模板命中后的像素同源复验：候选区与录制模板内容必须同源
        （波次1 实测：大片白底/低纹理 UI 会让跨页模板 CCOEFF 高分假阳性；
        同源复验把 0.72+ 的异页匹配挡下，改版页则自然退到锚/校验入口）。"""
        x, y, w, h = [int(v) for v in hit_rect]
        if w < 8 or h < 8 or x < 0 or y < 0 or x + w > w_screen or y + h > h_screen:
            return 0.0
        crop = screen_bgr[y:y + h, x:x + w]
        ref = cv2_resize_to(tpl_img, (w, h))
        return matcher.pixel_sim(crop, ref, size=(240, 150), thr=16.0)

    # 0) 上一帧同矩形快速复验（省一次多尺度全屏扫描）
    if prev_hint is not None and prev_hint[2] > 8 and prev_hint[3] > 8:
        px, py, pw, ph = [int(v) for v in prev_hint]
        if px >= 0 and py >= 0 and px + pw <= w_screen and py + ph <= h_screen:
            tpl1 = cv2_resize_to(tpl, (pw, ph))
            r0 = matcher.find_template(screen_bgr, tpl1, scales=(1.0,),
                                       score_thr=cfg.page_score_min)
            if r0["ok"] and abs(r0["rect"][0] - px) <= 6 and abs(r0["rect"][1] - py) <= 6:
                sim = _confirm_same_source(r0["rect"], tpl1, cfg.page_sim_min)
                if sim >= cfg.page_sim_min:
                    return {"ok": True, "rect": r0["rect"], "method": M_PAGE_TPL,
                            "confidence": round(r0["score"], 4), "scale": r0["scale"],
                            "elapsed_ms": r0["elapsed_ms"], "reused": True,
                            "sim": round(sim, 4), "detail": {}}

    # 1) 整窗/整页模板多尺度 + 同源确认
    r1 = matcher.find_template(screen_bgr, tpl, score_thr=cfg.page_score_min)
    detail["page_tpl"] = {"score": round(r1["best_score"], 4) if r1["rect"] is None
                          else round(r1["score"], 4),
                          "elapsed_ms": round(r1["elapsed_ms"], 1)}
    if r1["ok"]:
        sim = _confirm_same_source(r1["rect"], tpl, cfg.page_sim_min)
        detail["page_tpl"]["sim"] = round(sim, 4)
        if sim >= cfg.page_sim_min:
            return {"ok": True, "rect": r1["rect"], "method": M_PAGE_TPL,
                    "confidence": round(r1["score"], 4), "scale": r1["scale"],
                    "elapsed_ms": r1["elapsed_ms"], "reused": False, "sim": round(sim, 4),
                    "detail": detail}

    # 2) 静态锚（动态页/整窗失配时；§7.3 静态锚规格）
    anchors = page_spec.get("anchors") or []
    for a in anchors:
        a_tpl = matcher.dataurl_to_bgr(a["image"])
        ra = matcher.find_template(screen_bgr, a_tpl, score_thr=cfg.anchor_score_min)
        if not ra["ok"]:
            detail.setdefault("anchors", []).append(
                {"score": round(ra["best_score"], 4), "rect_in_page": a.get("rect_in_page")})
            continue
        hit = ra["rect"]
        geo = page_rect_from_anchor_geo(hit, a["rect_in_page"], size)
        if not geo["ok"] or not rect_inside(geo["rect"], (0, 0, w_screen, h_screen), pad=4):
            detail.setdefault("anchors", []).append(
                {"score": round(ra["score"], 4), "reason": geo.get("reason", "out_of_screen")})
            continue
        return {"ok": True, "rect": geo["rect"], "method": M_ANCHOR,
                "confidence": round(ra["score"], 4), "scale": geo["scale"],
                "elapsed_ms": ra["elapsed_ms"], "reused": False,
                "detail": {**detail, "anchor": a.get("rect_in_page")}}
    return {"ok": False, "rect": None, "method": M_PAGE_TPL, "confidence": 0.0,
            "scale": 1.0, "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "reused": False, "detail": detail}


def cv2_resize_to(bgr, size_wh):
    import cv2
    return cv2.resize(bgr, tuple(size_wh), interpolation=cv2.INTER_AREA)


def page_rect_from_anchor_geo(hit_xywh, anchor_rect_in_page, page_size):
    """薄封装：锚命中 → 页面屏幕矩形（核心几何在 capture.page_rect_from_anchor）。"""
    from engine.capture import page_rect_from_anchor
    return page_rect_from_anchor(hit_xywh, anchor_rect_in_page, page_size)


def rect_inside(rect, outer_xywh, pad=0) -> bool:
    x, y, w, h = rect
    ox, oy, ow, oh = outer_xywh
    return (x >= ox - pad and y >= oy - pad and x + w <= ox + ow + pad
            and y + h <= oy + oh + pad)


def _in_page(page_rect, abs_xywh) -> bool:
    """绝对坐标盒是否（含 pad 修正）落在页面内 —— 越界即弃。"""
    return rect_inside(abs_xywh, page_rect, pad=4)


# ---------------------------------------------------------------- 部件定位（页内）

def locate_widget(page_live_bgr, page_rect, target, cfg=None, page_scale=1.0,
                  exists=False, uia_provider=None, screen_bgr=None):
    """
    在已锁定页面(page_rect)内定位部件。page_live_bgr 应恰好是页面区域图像
    （可由外部从整屏裁剪），这样内部坐标即页内坐标，返回绝对屏幕盒。

    target: widget target dict（text/image/uia/rect_in_page/...）
    exists=True 时语义为“部件是否出现”：③ 页面内坐标不参与判定。
    uia_provider: 可选 callable(text) -> [{name, rect, center, type, automation_id}, ...]
                  （真实 UIA 采集在后续波次接 live 窗口；空 = 跳过①级，不算失败）
    返回 {ok, level(1|2|3|None), method, box(abs xywh), center(abs), confidence,
          matched_text?, elapsed_ms, detail}
    """
    cfg = cfg or LocConfig()
    t0 = time.perf_counter()
    detail = {}
    text = (target.get("text") or "").strip() if isinstance(target.get("text"), str) else ""
    img_data = target.get("image")
    rect_in_page = target.get("rect_in_page")
    match_pref = target.get("match", "auto")

    def _finish(**kw):
        return {"ok": kw.get("ok", False), "level": kw.get("level"),
                "method": kw.get("method"), "box": kw.get("box"),
                "center": kw.get("center"), "confidence": kw.get("confidence", 0.0),
                "matched_text": kw.get("matched_text"),
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "detail": detail, **kw.get("extra", {})}

    # ① UI 树（无提供者/无文字 → 跳过本级，不算失败；v0.31 §5.1）
    l1 = None
    if uia_provider is not None and text:
        try:
            hits = uia_provider(text) or []
        except Exception as e:
            detail["l1"] = {"ok": False, "reason": f"provider_error:{e}"}
            hits = []
        in_hits = [h for h in hits if rect_inside(h["rect"], page_rect, pad=2)]
        if in_hits:
            # 同名多实例消歧：选离“录点预测中心”最近的（M0 链内消歧；Edge DOM
            # 树坐标偏差 ~140px 已有实测 → 用录点距离判断坐标系可信度）
            anchor_xy = None
            if rect_in_page is not None:
                s = page_scale or 1.0
                anchor_xy = (int((rect_in_page[0] + rect_in_page[2] / 2) * s),
                             int((rect_in_page[1] + rect_in_page[3] / 2) * s))
            best = None
            best_d = None
            for hh in in_hits:
                if anchor_xy is None:
                    cand = hh
                    d = 0
                else:
                    c = hh["center"]
                    d = max(abs(c[0] - (page_rect[0] + anchor_xy[0])),
                            abs(c[1] - (page_rect[1] + anchor_xy[1])))
                    cand = hh
                if best is None or d < best_d:
                    best, best_d = cand, d
            if best is not None:
                detail["l1"] = {"ok": True, "n_hits": len(hits),
                                "in_page": len(in_hits), "name": best.get("name"),
                                "disambig_d": (None if best_d is None
                                               else round(best_d, 1))}
                return _finish(ok=True, level=1, method=M_UIA, box=best["rect"],
                               center=(best["rect"][0] + best["rect"][2] // 2,
                                       best["rect"][1] + best["rect"][3] // 2),
                               confidence=1.0, extra={"name": best.get("name")})
        else:
            detail["l1"] = {"ok": False, "n_hits": len(hits),
                            "reason": "no_hit_in_page" if hits else "no_hit"}
    else:
        detail["l1"] = {"ok": False, "reason": "skip_no_provider_or_text"}

    # 预测位置（③ 几何锚点：条带搜索中心；exists 语义下同样以录点/页心为锚做带搜索）
    ph, pw = page_live_bgr.shape[:2]
    text_search_points = []
    if rect_in_page is not None:
        rx, ry, rw, rh = [int(v) for v in rect_in_page]
        s = page_scale or 1.0
        text_search_points.append(((int((rx + rw / 2) * s), int((ry + rh / 2) * s)),
                                   (rw, rh)))
    text_search_points.append(((pw // 2, ph // 2), None))

    # 条带尺寸策略（波次1 实测定案，2026-09-09）：
    # 固定高条带会把页面其他行/框线的 OCR det 盒成批卷进来（600×194 实测 4–6s/次）。
    # 有录制框时先用“框相关 + 行内收紧”小带（≤ ~260×160，实测 <0.5s），
    # 再放宽横向（仍行内收紧）；仅无框/兜底才用整页中心大带（慢但稀有）。
    def _bands_for(rect_wh, is_recorded_anchor):
        if is_recorded_anchor:
            rw, rh = rect_wh
            yield (min(max(160, rw // 2 + 150), pw // 2),
                   min(max(90, rh // 2 + 70), ph // 2))          # 带1：框紧邻（行内）
            yield (min(720, pw // 2),
                   min(max(130, rh + 190), ph // 2))             # 带2：横向放宽（仍行内）
        yield (min(650, pw // 2), min(260, ph // 2))              # 带3：整页兜底（慢）
        yield (min(320, pw // 2), min(120, ph // 2))              # 带4：中心小带

    # ②a 文字条带（默认/文字优先主信号）
    l2_text = {"ok": False}
    if text:
        needle = matcher.text_needle_short(text)
        f = None
        used_off = None
        for (cx0, cy0), rect_wh in text_search_points:
            small_first = rect_wh is not None
            bands = _bands_for(rect_wh, small_first)
            for half_w, half_h in bands:
                lx0 = max(0, cx0 - half_w)
                ly0 = max(0, cy0 - half_h)
                sx = min(pw - lx0, 2 * half_w)
                sy = min(ph - ly0, 2 * half_h)
                if sx < 40 or sy < 24:
                    continue
                strip = page_live_bgr[ly0:ly0 + sy, lx0:lx0 + sx]
                f = matcher.find_text_ocr(strip, needle, thr=cfg.text_sim_min)
                used_off = (lx0, ly0)
                if f["ok"]:
                    break
            if f and f["ok"]:
                break
        if f and f["ok"]:
            bx, by, bw, bh = f["box"]
            abs_box = (page_rect[0] + used_off[0] + bx, page_rect[1] + used_off[1] + by, bw, bh)
            if _in_page(page_rect, abs_box):
                l2_text = {"ok": True, "box": abs_box, "center": (abs_box[0] + bw // 2,
                                                                  abs_box[1] + bh // 2),
                           "confidence": round(f["score"], 3),
                           "matched_text": f["matched_text"],
                           "elapsed_ms": round(f["elapsed_ms"], 1),
                           "upsample": f.get("upsample", 0)}
            else:
                detail["l2_ocr_out_of_page"] = abs_box
        else:
            l2_text = {"ok": False,
                       "elapsed_ms": round((f or {}).get("elapsed_ms", 0), 1)}
    detail["l2_ocr"] = {k: v for k, v in l2_text.items() if k != "box"}

    # ②b 部件模板（兜底 / image_first 主信号）
    l2_tpl = {"ok": False}
    if img_data:
        w_tpl = matcher.dataurl_to_bgr(img_data)
        # 页面已锁定 → 部件模板只需按页面实际缩放匹配（波次1 实测：5 档全图扫描在
        # 本机偶发 20× 慢窗口，单档 + 文字主信号组合稳健且预算内；见 matcher 模块注）
        s = round(page_scale or 1.0, 4)
        scales = (s,)
        r = matcher.find_template(page_live_bgr, w_tpl, scales=scales,
                                  score_thr=cfg.tpl_score_min)
        detail["l2_tpl_elapsed"] = round(r["elapsed_ms"], 1)
        detail["l2_tpl_best"] = round(r["best_score"], 4)
        if r["ok"]:
            bx, by, bw, bh = r["rect"]
            abs_box = (page_rect[0] + bx, page_rect[1] + by, bw, bh)
            if _in_page(page_rect, abs_box):
                l2_tpl = {"ok": True, "box": abs_box,
                          "center": (abs_box[0] + bw // 2, abs_box[1] + bh // 2),
                          "confidence": round(r["score"], 3),
                          "elapsed_ms": round(r["elapsed_ms"], 1)}
            else:
                detail["l2_tpl_out_of_page"] = abs_box
    detail["l2_tpl"] = {"ok": l2_tpl["ok"], "confidence": l2_tpl.get("confidence", 0.0)}

    # ② 融合/偏好选择
    l2_chosen = None
    text_first = match_pref in ("auto", "text_first")
    if text_first:
        cands = (l2_text, l2_tpl)
    else:
        cands = (l2_tpl, l2_text)
    for c in cands:
        if c["ok"]:
            l2_chosen = c
            break
    if l2_chosen is not None:
        method = M_OCR_TEXT if l2_chosen.get("matched_text") else M_TPL
        conf = l2_chosen["confidence"]
        # 双信号同点 → 融合加分（都在场且中心 ≤30px）
        other = l2_tpl if l2_chosen is l2_text else l2_text
        if other["ok"] and l2_chosen["box"] is not None and other.get("box"):
            d = max(abs(l2_chosen["center"][0] - other["center"][0]),
                    abs(l2_chosen["center"][1] - other["center"][1]))
            if d <= 30:
                conf = round(min(1.0, 1 - (1 - conf) * (1 - other["confidence"] * 0.5)), 4)
                detail["fusion"] = True
        return _finish(ok=True, level=2, method=method, box=l2_chosen["box"],
                       center=l2_chosen["center"], confidence=conf,
                       matched_text=l2_chosen.get("matched_text"))

    # ③ 页面内坐标（几何兜底；exists 语义不参与）
    if not exists and rect_in_page is not None:
        rx, ry, rw, rh = [int(v) for v in rect_in_page]
        s = page_scale or 1.0
        abs_box = (page_rect[0] + int(round(rx * s)), page_rect[1] + int(round(ry * s)),
                   max(2, int(round(rw * s))), max(2, int(round(rh * s))))
        if _in_page(page_rect, abs_box):
            return _finish(ok=True, level=3, method=M_PAGE_COORD, box=abs_box,
                           center=(abs_box[0] + abs_box[2] // 2,
                                   abs_box[1] + abs_box[3] // 2),
                           confidence=0.5, extra={"fallback": True})
        detail["l3_out_of_page"] = abs_box
    return _finish(ok=False, level=None, method=None, box=None, center=None)


def locate_widget_on_screen(screen_bgr, page_rect, target, cfg=None, page_scale=1.0,
                            exists=False, uia_provider=None):
    """整屏图 + 页面矩形 → 裁剪页内图再走 locate_widget（坐标自动对齐）。"""
    x, y, w, h = [int(v) for v in page_rect]
    hh, ww = screen_bgr.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(ww, x + w), min(hh, y + h)
    if x1 <= x0 or y1 <= y0:
        raise EngineError("page_not_found", ERRORS["page_not_found"],
                          {"reason": "page_rect_out_of_screen"})
    page_live = screen_bgr[y0:y1, x0:x1]
    res = locate_widget(page_live, (x0, y0, x1 - x0, y1 - y0), target, cfg=cfg,
                        page_scale=page_scale, exists=exists, uia_provider=uia_provider,
                        screen_bgr=screen_bgr)
    return res
