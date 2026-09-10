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
# 同源确认的"勉强可用"档（用户审查要求折中）。定在 0.80 而不是更低，是实测定的：
#   同页改动后同源仍在 0.93+（0.82 并不严）；而异页能到 0.772、纯白页 0.884 ——
#   线压到 0.72 会把异页放行。故折中档 = 0.80，且额外要求整窗模板分数 ≥ 0.85（纵深防御）。
PAGE_SIM_SOFT = 0.80
PAGE_SOFT_TPL_MIN = 0.85
# 命中区域的灰度标准差下限：低于它说明是一片纯色/空白（页面崩了或还在加载），
# 这种区域 CCOEFF 会给假高分（实测纯白页同源分数也有 0.883），必须直接判不命中。
PAGE_MIN_STD = 6.0
ANCHOR_SCORE_MIN = 0.75
TPL_SCORE_MIN = 0.70
TEXT_SIM_MIN = 0.75
# 环带模板（只比外圈边框/底色，中心内容不参与）：解决"占位提示被填入的数据顶替"
RING_SCORE_MIN = 0.60

# 定位日志 method 词汇（清单 §4）
M_PAGE_TPL = "page_tpl"
M_ANCHOR = "anchor"
M_UIA = "uia"
M_OCR_TEXT = "ocr_text"
M_TPL = "tpl"
M_TPL_RING = "tpl_ring"
M_PAGE_COORD = "page_coord"


class LocConfig:
    def __init__(self, page_score_min=PAGE_SCORE_MIN, page_sim_min=PAGE_SIM_MIN,
                 anchor_score_min=ANCHOR_SCORE_MIN,
                 tpl_score_min=TPL_SCORE_MIN, text_sim_min=TEXT_SIM_MIN,
                 ring_score_min=RING_SCORE_MIN, page_sim_soft=PAGE_SIM_SOFT,
                 page_soft_tpl_min=PAGE_SOFT_TPL_MIN,
                 full_page_ocr=False):
        self.page_score_min = page_score_min
        self.page_sim_min = page_sim_min
        self.page_sim_soft = page_sim_soft
        self.page_soft_tpl_min = page_soft_tpl_min
        self.anchor_score_min = anchor_score_min
        self.tpl_score_min = tpl_score_min
        self.text_sim_min = text_sim_min
        self.ring_score_min = ring_score_min
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

    def _low_texture(hit_rect):
        """命中区域是不是"一片纯色/空白"（页面崩了/还在加载时不许当命中）。

        只看**中心 80%**：候选区域常把页面与背景的交界带进来，边界会把标准差拉高，
        于是"整屏纯白"也能混过纹理检查（实测）。
        """
        x, y, w, h = [int(v) for v in hit_rect]
        if w < 8 or h < 8 or x < 0 or y < 0 or x + w > w_screen or y + h > h_screen:
            return True
        mx, my = int(w * 0.1), int(h * 0.1)
        core = screen_bgr[y + my:y + h - my, x + mx:x + w - mx]
        return matcher.gray_std(core) < PAGE_MIN_STD

    # 0) 上一帧同矩形快速复验（省一次多尺度全屏扫描）
    if prev_hint is not None and prev_hint[2] > 8 and prev_hint[3] > 8:
        px, py, pw, ph = [int(v) for v in prev_hint]
        if px >= 0 and py >= 0 and px + pw <= w_screen and py + ph <= h_screen:
            tpl1 = cv2_resize_to(tpl, (pw, ph))
            r0 = matcher.find_template(screen_bgr, tpl1, scales=(1.0,),
                                       score_thr=cfg.page_score_min)
            if (r0["ok"] and abs(r0["rect"][0] - px) <= 6 and abs(r0["rect"][1] - py) <= 6
                    and not _low_texture(r0["rect"])):
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
    if r1["ok"] and not _low_texture(r1["rect"]):
        sim = _confirm_same_source(r1["rect"], tpl, cfg.page_sim_min)
        detail["page_tpl"]["sim"] = round(sim, 4)
        # 同源确认三档（用户审查折中）：≥0.82 算稳；0.80~0.82 且整窗模板分数 ≥0.85
        # 时勉强可用 → 采纳但记警告、置信度打折；否则丢弃走锚/校准。
        sl = getattr(cfg, "page_sim_soft", PAGE_SIM_SOFT)
        tl = getattr(cfg, "page_soft_tpl_min", PAGE_SOFT_TPL_MIN)
        if sim >= cfg.page_sim_min:
            verdict = "ok"
        elif sim >= sl and r1["score"] >= tl:
            verdict = "warn"
        else:
            verdict = "no"
        detail["page_tpl"]["verdict"] = verdict
        if verdict != "no":
            conf = round(r1["score"] * (0.9 if verdict == "warn" else 1.0), 4)
            return {"ok": True, "rect": r1["rect"], "method": M_PAGE_TPL,
                    "confidence": conf, "scale": r1["scale"], "reused": False,
                    "sim": round(sim, 4), "soft": verdict == "warn",
                    "elapsed_ms": r1["elapsed_ms"], "detail": detail}

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

def _nearby_ok(target, page_bgr, cand):
    """相对锚点/邻居文字校验：候选位置附近是否还能找到录制时记下的邻里文字。

    用途（用户审查提出）：同页出现多个相同文字时（同名列、重复按钮、两个一样的占位
    提示），用"邻居对不对得上"来消歧。返回 True / False / None（无线索，不参与判定）。
    """
    items = [x for x in (target.get("nearby") or []) if (x.get("text") or "").strip()]
    if not items:
        return None
    ph_, pw_ = page_bgr.shape[:2]
    cx = cand["box"][0] + cand["box"][2] // 2
    cy = cand["box"][1] + cand["box"][3] // 2
    checked = hits = 0
    for item in items[:2]:                    # 最多验两条，控制开销
        off = item.get("offset") or [0, 0]
        rect = item.get("rect_in_page") or [0, 0, 160, 28]
        ex, ey = cx + int(off[0]), cy + int(off[1])
        half_w = max(int(rect[2]) // 2 + 40, 90)
        half_h = max(int(rect[3]) // 2 + 20, 40)
        x0, y0 = max(0, ex - half_w), max(0, ey - half_h)
        sx, sy = min(pw_ - x0, 2 * half_w), min(ph_ - y0, 2 * half_h)
        if sx < 24 or sy < 16:
            continue
        strip = page_bgr[y0:y0 + sy, x0:x0 + sx]
        checked += 1
        r = matcher.find_text_ocr(strip, matcher.text_needle_short(item["text"]), thr=0.72)
        if r["ok"]:
            hits += 1
    if checked == 0:
        return None
    return hits > 0


def _path_match(rec_path, got_path) -> int:
    """记录路径与候选路径的**后缀**匹配层数（0 = 完全对不上）。

    M2：同名控件消歧用。取后缀是因为页面结构可能加深/变浅，但"最近的几层祖先"
    通常稳定（录制时记的就是最近 3 层）。
    """
    n = 0
    for a, b in zip(reversed(list(rec_path or [])), reversed(list(got_path or []))):
        if a and b and str(a) == str(b):
            n += 1
        else:
            break
    return n


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
        px_w, px_h = page_rect[2], page_rect[3]
        # 整页容器/窗口根（name 常为页面标题、盒≈整页）会误配——盒过大直接弃用
        in_hits = [h for h in hits
                   if rect_inside(h["rect"], page_rect, pad=2)
                   and h["rect"][2] < px_w * 0.7 and h["rect"][3] < px_h * 0.7]
        if in_hits:
            # 同名多实例消歧（M2 改进）：**先看树路径**，再看离"录点预测中心"的距离。
            # 路径对得上就认它 —— M1 时代只按距离，遇到 Edge DOM 那类坐标偏差
            # （实测 ~140px）就只好弃用本级，有了路径即使坐标飘了也能认准。
            anchor_xy = None
            if rect_in_page is not None:
                s = page_scale or 1.0
                anchor_xy = (int((rect_in_page[0] + rect_in_page[2] / 2) * s),
                             int((rect_in_page[1] + rect_in_page[3] / 2) * s))
            rec_path = ((target.get("uia") or {}).get("path") or [])
            rec_aid = ((target.get("uia") or {}).get("automation_id") or "").strip()
            best = None
            best_d = None
            best_pm = 0
            best_aid = False
            for hh in in_hits:
                if anchor_xy is None:
                    d = 0
                else:
                    c = hh["center"]
                    d = max(abs(c[0] - (page_rect[0] + anchor_xy[0])),
                            abs(c[1] - (page_rect[1] + anchor_xy[1])))
                pm = _path_match(rec_path, hh.get("path") or [])
                aid_ok = bool(rec_aid) and rec_aid == (hh.get("automation_id") or "").strip()
                key = (0 if (pm > 0 or aid_ok) else 1, -pm, 0 if aid_ok else 1, d)
                if best is None or key < (0 if (best_pm > 0 or best_aid) else 1,
                                          -best_pm, 0 if best_aid else 1, best_d):
                    best, best_d, best_pm, best_aid = hh, d, pm, aid_ok
            ident_ok = best_pm > 0 or best_aid          # 身份对得上（路径或控件 ID）
            too_far = (best_d is not None and best_d > 60) and not ident_ok
            if best is not None and not too_far:
                detail["l1"] = {"ok": True, "n_hits": len(hits),
                                "in_page": len(in_hits), "name": best.get("name"),
                                "path_match": best_pm, "aid_match": best_aid,
                                "disambig_d": (None if best_d is None
                                               else round(best_d, 1))}
                return _finish(ok=True, level=1, method=M_UIA, box=best["rect"],
                               center=(best["rect"][0] + best["rect"][2] // 2,
                                       best["rect"][1] + best["rect"][3] // 2),
                               confidence=1.0, extra={"name": best.get("name")})
            detail["l1"] = {"ok": False, "n_hits": len(hits),
                            "in_page": len(in_hits),
                            "reason": "coord_unreliable" if too_far else "no_hit",
                            "best_d": (None if best_d is None else round(best_d, 1))}
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
    if rect_in_page is None:
        # 无录点时兜底多看两处：页面下半区（贴近页底的提示文字，如登录失败的
        # "密码错误"）与页面上部（顶栏/导航文字，如"推荐""首页"）——
        # 只搜页心 ±260px 时会漏掉这两类（M1 波次3 演示、M2 案例生成各踩过一次）。
        text_search_points.append(((pw // 2, int(ph * 0.78)), None))
        text_search_points.append(((pw // 2, int(ph * 0.22)), None))

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
    # 收集命中候选再挑选：同页常出现相同文字（同名列、重复按钮、两个一样的占位提示），
    # 只取"第一个命中"会挑错（用户审查提出）。挑选规则见下方 far/near 分类。
    text_cands = []                      # [{box(页内), score, matched_text, dist, nearby_ok}]
    text_elapsed = 0.0
    if text:
        needle = matcher.text_needle_short(text)
        for (cx0, cy0), rect_wh in text_search_points:
            bands = _bands_for(rect_wh, rect_wh is not None)
            got_here = False
            for half_w, half_h in bands:
                lx0 = max(0, cx0 - half_w)
                ly0 = max(0, cy0 - half_h)
                sx = min(pw - lx0, 2 * half_w)
                sy = min(ph - ly0, 2 * half_h)
                if sx < 40 or sy < 24:
                    continue
                strip = page_live_bgr[ly0:ly0 + sy, lx0:lx0 + sx]
                hits = matcher.find_text_all_ocr(strip, needle, thr=cfg.text_sim_min)
                text_elapsed += float(hits[0]["elapsed_ms"]) if hits else 0.0
                for h in hits:
                    bx, by, bw, bh = h["box"]
                    box = (lx0 + bx, ly0 + by, bw, bh)
                    if _in_page(page_rect, (page_rect[0] + box[0], page_rect[1] + box[1],
                                            bw, bh)):
                        text_cands.append({"box": box, "score": h["score"],
                                           "matched_text": h["matched_text"],
                                           "upsample": h.get("upsample", 0)})
                    else:
                        detail.setdefault("l2_ocr_out_of_page", []).append(box)
                if hits:
                    got_here = True
                    break                    # 该搜索点已命中 → 不再扩大条带（省时间）
            if got_here:
                break                        # 近处搜索点命中即停；没有才继续兜底点

    # 挑候选：离录点近的优先；太远的降级为"兜底候选"（用户审查：位置相近才采纳）
    anchor_xy = (text_search_points[0][0] if rect_in_page is not None
                 else (pw // 2, ph // 2))
    far_limit = 160
    if img_data:
        w_tpl0 = matcher.dataurl_to_bgr(img_data)
        if w_tpl0 is not None and getattr(w_tpl0, "size", 0):
            far_limit = max(2 * w_tpl0.shape[1], 2 * w_tpl0.shape[0], 160)
    near_c, far_c = [], []
    for c in text_cands:
        cx_, cy_ = c["box"][0] + c["box"][2] // 2, c["box"][1] + c["box"][3] // 2
        c["dist"] = max(abs(cx_ - anchor_xy[0]), abs(cy_ - anchor_xy[1]))
        c["nearby_ok"] = _nearby_ok(target, page_live_bgr, c) if target.get("nearby") else None
        (far_c if c["dist"] > far_limit else near_c).append(c)
    # 邻居对得上的最优先；其次离录点近；最后分数高
    near_c.sort(key=lambda c: (0 if c.get("nearby_ok") else 1, c["dist"], -c["score"]))
    far_c.sort(key=lambda c: (-c["score"], c["dist"]))

    def _as_l2(c):
        if not c:
            return {"ok": False}
        bx, by, bw, bh = c["box"]
        abs_box = (page_rect[0] + bx, page_rect[1] + by, bw, bh)
        return {"ok": True, "box": abs_box,
                "center": (abs_box[0] + bw // 2, abs_box[1] + bh // 2),
                "confidence": c["score"], "matched_text": c["matched_text"],
                "elapsed_ms": round(text_elapsed, 1), "upsample": c.get("upsample", 0),
                "dist": c["dist"], "nearby_ok": c.get("nearby_ok")}

    l2_text = _as_l2(near_c[0] if near_c else None)
    l2_text_far = _as_l2(far_c[0] if far_c else None)
    # 候选留痕（用户审查要求）：默认记下前 3 个候选的盒、分数、距录点距离、邻居是否对上，
    # 便于事后审查"为什么挑了这个/为什么没找到"。
    detail["l2_ocr"] = {"ok": l2_text["ok"], "confidence": l2_text.get("confidence", 0.0),
                        "cands": len(text_cands), "near": len(near_c), "far": len(far_c),
                        "dist": l2_text.get("dist"), "nearby_ok": l2_text.get("nearby_ok"),
                        "top3": [[list(c["box"]), c["score"], c.get("dist"),
                                  c.get("nearby_ok")] for c in (near_c + far_c)[:3]],
                        "elapsed_ms": round(text_elapsed, 1)}
    if l2_text_far["ok"] and not l2_text["ok"]:
        detail["l2_ocr_far_only"] = {"dist": l2_text_far.get("dist"),
                                     "text": l2_text_far.get("matched_text")}

    # ②b 部件模板（兜底 / image_first 主信号）
    l2_tpl = {"ok": False}
    if img_data:
        w_tpl = matcher.dataurl_to_bgr(img_data)
        # 页面已锁定 → 部件模板只需按页面实际缩放匹配（波次1 实测：5 档全图扫描在
        # 本机偶发 20× 慢窗口，单档 + 文字主信号组合稳健且预算内；见 matcher 模块注）
        s = round(page_scale or 1.0, 4)
        scales = (s,)
        # 只在"录点附近"搜（与 ②a 文字路径的条带同一思路）：页面锁定后部件相对页面
        # 是稳定的；全页搜会错配到同页另一个外观相同的控件上（实测：两个一样的输入框，
        # 录下面那个却定位到上面那个）。确实重排了 → 交给 ③+安全闸 与校准流程。
        area = None
        if rect_in_page is not None:
            rx, ry, rw, rh = [int(v) for v in rect_in_page]
            cx0, cy0 = int((rx + rw / 2) * s), int((ry + rh / 2) * s)
            th0, tw0 = w_tpl.shape[:2]
            half_w, half_h = max(int(tw0 * s * 0.6), 80), max(int(th0 * s * 1.2), 48)
            lx0, ly0 = max(0, cx0 - half_w), max(0, cy0 - half_h)
            sx, sy = min(pw - lx0, 2 * half_w), min(ph - ly0, 2 * half_h)
            if sx >= int(tw0 * s) + 4 and sy >= int(th0 * s) + 4:
                area = (lx0, ly0, sx, sy)
        r = matcher.find_template(page_live_bgr, w_tpl, scales=scales,
                                  score_thr=cfg.tpl_score_min, search=area)
        detail["l2_tpl_area"] = area
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

    # ②c 环带模板（"认边框不认内容"）：整块失配时再试一次。
    # 场景：录制时输入框是空的（内侧是灰色占位提示），跑过一次后框里填了数据 ——
    # 文字没了、整块图案也对不上，只剩坐标；用外圈边框定位则不受"框里写了什么"影响。
    l2_ring = {"ok": False}
    if img_data and not l2_tpl["ok"]:
        w_tpl = matcher.dataurl_to_bgr(img_data)
        if w_tpl is not None and getattr(w_tpl, "size", 0):
            s = round(page_scale or 1.0, 4)
            th, tw = w_tpl.shape[:2]
            tw_s, th_s = int(round(tw * s)), int(round(th * s))
            if rect_in_page is not None:
                rx, ry, rw, rh = [int(v) for v in rect_in_page]
                cx0, cy0 = int((rx + rw / 2) * s), int((ry + rh / 2) * s)
            else:
                cx0, cy0 = pw // 2, ph // 2
            half_w = max(int(tw_s * 0.6), 80)
            half_h = max(int(th_s * 1.2), 48)
            lx0 = max(0, cx0 - half_w)
            ly0 = max(0, cy0 - half_h)
            sx = min(pw - lx0, 2 * half_w)
            sy = min(ph - ly0, 2 * half_h)
            if sx >= tw_s + 4 and sy >= th_s + 4:
                rr = matcher.find_template_ring(page_live_bgr, w_tpl, scale=s,
                                                score_thr=cfg.ring_score_min,
                                                search=(lx0, ly0, sx, sy))
                detail["l2_ring_score"] = round(rr.get("best_score", -1.0), 4)
                detail["l2_ring_elapsed"] = round(rr.get("elapsed_ms", 0.0), 1)
                if rr["ok"]:
                    bx, by, bw, bh = rr["rect"]
                    abs_box = (page_rect[0] + bx, page_rect[1] + by, bw, bh)
                    if _in_page(page_rect, abs_box):
                        l2_ring = {"ok": True, "box": abs_box,
                                   "center": (abs_box[0] + bw // 2, abs_box[1] + bh // 2),
                                   "confidence": rr["score"],
                                   "elapsed_ms": rr["elapsed_ms"]}
    detail["l2_ring"] = {"ok": l2_ring["ok"], "confidence": l2_ring.get("confidence", 0.0)}

    # ② 融合/偏好选择
    l2_chosen = None
    text_first = match_pref in ("auto", "text_first")
    # 顺序里"远文字"排在最后：文字虽然命中了，但位置离录点太远时不优先采纳
    # （用户审查：位置相近才采纳）；实在没有别的证据时它仍是兜底。
    if text_first:
        cands = (l2_text, l2_tpl, l2_ring, l2_text_far)
    else:
        cands = (l2_tpl, l2_ring, l2_text, l2_text_far)
    if exists:
        # "如果看到"要的是强证据：环带只看外圈边框、远文字可能匹配到别处同名文字，
        # 都不足以说明"这个东西还在"（与 ③ 页内坐标同一口径）。
        cands = tuple(c for c in cands if c is not l2_ring and c is not l2_text_far)
    for c in cands:
        if c["ok"]:
            l2_chosen = c
            break
    if l2_chosen is not None:
        if l2_chosen.get("matched_text"):
            method = M_OCR_TEXT
        elif l2_chosen is l2_ring:
            method = M_TPL_RING
        else:
            method = M_TPL
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
