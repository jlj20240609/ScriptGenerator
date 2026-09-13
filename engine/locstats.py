"""engine.locstats — 定位质量统计：把定位日志聚合成"识别率"报告。

为什么需要它：引擎每次定位都写 engine_loc.jsonl（或界面的 m1_ui_loc.jsonl），但一直没人
聚合过 —— 于是"识别能力到底怎么样"只能靠感觉。这个模块只做**离线解析**：不抓屏、不点击、
不改任何脚本，把日志变成可比较的数字。

口径（三个数字先说清楚，避免各说各话）：
  一次命中率  = locate_widget 里 ok=true 的比例。
  层级占比    = 成功定位里各层占多少（l1=UI 树，l2=部件相似度：文字/模板/环带，l3=页面内坐标兜底）。
  盲点率      = 成功定位里"靠第 3 层兜底"的比例 —— 这一层没认出目标，只是按录下来的位置点，
                数字越高越危险（位置变了就会点空）。

用法（代码里）：
    from engine import locstats
    rep = locstats.analyze(locstats.load_rows(path))
    print(locstats.render_text(rep))
"""
from __future__ import annotations

import json
from pathlib import Path

# 定位方式 → 层级
L1_METHODS = ("uia",)
L2_METHODS = ("ocr_text", "tpl", "tpl_ring")
L3_METHODS = ("page_coord",)
# 坐标固化（2026-09-14）：按上次校准记下的位置点 —— 单独一档，好跟"识别出来的"分开看
LEARNED_METHODS = ("learned",)
# 页面定位方式（不参与部件三级口径）
PAGE_METHODS = ("page_tpl", "anchor", "feature")
# 过程性失败原因（引擎 report / 定位日志里的 reason）
FAIL_REASONS = ("window_not_found", "page_not_found", "widget_not_found",
                "click_guard_failed", "stop_requested", "outcome_failed")


def level_of(method: str) -> str:
    """定位方式 → 层级字符串（l1/l2/l3/mem）；不认识的返回空串。"""
    m = (method or "").strip()
    if m in L1_METHODS:
        return "l1"
    if m in L2_METHODS:
        return "l2"
    if m in L3_METHODS:
        return "l3"
    if m in LEARNED_METHODS:
        return "mem"
    return ""


def load_rows(path) -> list:
    """读 JSONL 定位日志；坏行跳过（日志是追加写的，偶尔会有半行）。"""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def _field(row: dict, key, default=None):
    """先取顶层，再取 extra 里的（两种写法历史都出现过）。"""
    if key in row and row[key] is not None:
        return row[key]
    extra = row.get("extra")
    if isinstance(extra, dict) and extra.get(key) is not None:
        return extra[key]
    return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(a, b) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def _quantile(xs, q: float):
    """朴素分位数（样本少也能用；不做插值，取最近秩）。"""
    vals = sorted(x for x in xs if x is not None)
    if not vals:
        return None
    idx = int(round(q * (len(vals) - 1)))
    return round(vals[max(0, min(idx, len(vals) - 1))], 1)


def _why_of(row) -> dict:
    """取 2️⃣ 写进日志的失败原因（可能没有）。"""
    w = _field(row, "why")
    return w if isinstance(w, dict) else {}


def _count_reasons(rows) -> dict:
    """聚合失败原因：既看 why.reason，也看行自己的 reason（window_not_found 这类）。"""
    cnt = {}
    for r in rows:
        reason = _field(r, "reason") or _why_of(r).get("reason")
        if reason:
            cnt[str(reason)] = cnt.get(str(reason), 0) + 1
    return dict(sorted(cnt.items(), key=lambda kv: -kv[1]))


def _step_summary(rows_by_event: dict) -> dict:
    page, widget, guard, exists, fails = (rows_by_event.get(k, []) for k in
                                          ("page", "widget", "guard", "exists", "fails"))
    page_ok = [bool(_field(r, "page_ok", _field(r, "ok"))) for r in page]
    w_ok = [bool(_field(r, "ok")) for r in widget]
    levels = {}
    for r, ok in zip(widget, w_ok):
        if not ok:
            continue
        lv = level_of(str(_field(r, "method") or "")) or "其它"
        levels[lv] = levels.get(lv, 0) + 1
    blind = levels.get("l3", 0)
    first = w_ok[0] if w_ok else None
    return {
        "page": {
            "attempts": len(page), "ok": sum(page_ok),
            "methods": _count_by(page, "method"),
            "p50_ms": _quantile([_num(_field(r, "elapsed_ms")) for r in page], 0.5),
            "reasons": _count_reasons(page),
        },
        "widget": {
            "attempts": len(widget), "ok": sum(w_ok),
            "levels": dict(sorted(levels.items())),
            "first_ok": first,
            "blind": blind,
            "conf_p50": _quantile([_num(_field(r, "confidence")) for r in widget], 0.5),
            "p50_ms": _quantile([_num(_field(r, "elapsed_ms")) for r in widget], 0.5),
            "reasons": _count_reasons(widget),
        },
        "guard": {
            "attempts": len(guard),
            "ok": sum(1 for r in guard if bool(_field(r, "ok"))),
            "methods": _count_by(guard, "method"),
            "p50_score": _quantile([_num(_field(r, "confidence")) for r in guard], 0.5),
        },
        "exists": {"attempts": len(exists),
                   "ok": sum(1 for r in exists if bool(_field(r, "found", _field(r, "ok"))))},
        "fails": _count_reasons(fails),
    }


def _count_by(rows, key) -> dict:
    cnt = {}
    for r in rows:
        v = _field(r, key)
        if v is None:
            continue
        v = str(v)
        cnt[v] = cnt.get(v, 0) + 1
    return dict(sorted(cnt.items(), key=lambda kv: -kv[1]))


def _win_bucket(win) -> str:
    """把窗口状态归成一类 —— 用来回答"页面为什么认不出来"。

    0.0（屏幕上根本没有它）和 0.3（窗口在、内容变了）是两种病：前者要"把窗口调出来"，
    后者才轮到锚点/特征/PrintWindow。没有这个分档，就没法决定往哪一头投人力。
    """
    if not isinstance(win, dict) or not win:
        return "没记录（旧日志）"
    if not win.get("found"):
        return "窗口不在屏幕上（没开或被关了）"
    if win.get("iconic"):
        return "窗口在，但被最小化"
    if win.get("covered_by"):
        return "窗口在，但被别的窗口压着"
    if not win.get("foreground"):
        return "窗口在，但不是前台"
    return "窗口正常在前台（属于内容变了）"


def analyze(rows) -> dict:
    """把定位日志行聚合成报告字典（结构见模块 docstring 与 test_locstats）。"""
    by_step = {}
    for r in rows:
        sid = str(r.get("step_id") or "?")
        ev = str(r.get("event") or "")
        bucket = {"locate_page": "page", "locate_widget": "widget", "click_guard": "guard",
                  "exists_check": "exists", "procedural_fail": "fails",
                  "raise_window": "fails"}.get(ev, "other")
        by_step.setdefault(sid, {}).setdefault(bucket, []).append(r)

    steps = {sid: _step_summary(buckets) for sid, buckets in sorted(by_step.items())}

    all_widget = [r for b in by_step.values() for r in b.get("widget", [])]
    all_page = [r for b in by_step.values() for r in b.get("page", [])]
    all_guard = [r for b in by_step.values() for r in b.get("guard", [])]
    all_fails = [r for b in by_step.values() for r in b.get("fails", [])]
    w_ok = [bool(_field(r, "ok")) for r in all_widget]
    levels = {}
    for r, ok in zip(all_widget, w_ok):
        if ok:
            lv = level_of(str(_field(r, "method") or "")) or "其它"
            levels[lv] = levels.get(lv, 0) + 1
    page_ok = [bool(_field(r, "page_ok", _field(r, "ok"))) for r in all_page]
    steps_with_widget = [s for s in steps.values() if s["widget"]["attempts"]]
    first_ok = [s["widget"]["first_ok"] for s in steps_with_widget
                if s["widget"]["first_ok"] is not None]

    # 页面认不出来时的窗口状态分档
    win_fail = {}
    for r in all_page:
        if bool(_field(r, "page_ok", _field(r, "ok"))):
            continue
        b = _win_bucket(_field(r, "win"))
        win_fail[b] = win_fail.get(b, 0) + 1

    overall = {
        "steps": len(steps),
        "widget": {
            "attempts": len(all_widget), "ok": sum(w_ok),
            "hit_rate": _pct(sum(w_ok), len(all_widget)),
            "levels": dict(sorted(levels.items())),
            "blind": levels.get("l3", 0),
            "blind_rate": _pct(levels.get("l3", 0), sum(w_ok)),
            "first_try_rate": _pct(sum(1 for x in first_ok if x), len(first_ok)),
            "p50_ms": _quantile([_num(_field(r, "elapsed_ms")) for r in all_widget], 0.5),
            "p90_ms": _quantile([_num(_field(r, "elapsed_ms")) for r in all_widget], 0.9),
        },
        "page": {
            "attempts": len(all_page), "ok": sum(page_ok),
            "hit_rate": _pct(sum(page_ok), len(all_page)),
            "methods": _count_by(all_page, "method"),
            "p50_ms": _quantile([_num(_field(r, "elapsed_ms")) for r in all_page], 0.5),
        },
        "guard": {
            "attempts": len(all_guard),
            "ok": sum(1 for r in all_guard if bool(_field(r, "ok"))),
            "pass_rate": _pct(sum(1 for r in all_guard if bool(_field(r, "ok"))), len(all_guard)),
        },
        "fail_reasons": _count_reasons(all_fails) or _count_reasons(
            [r for b in by_step.values() for r in b.get("widget", []) if not _field(r, "ok")]),
        "why_reasons": _why_all(by_step),
        "page_fail_win": win_fail,
    }
    ts = [r.get("ts") for r in rows if r.get("ts")]
    return {"rows": len(rows), "time_range": [ts[0], ts[-1]] if ts else [],
            "steps": steps, "overall": overall}


def _why_all(by_step) -> dict:
    """聚合 2️⃣ 落盘的 why.reason（哪些机制在拒候选）。"""
    cnt = {}
    for buckets in by_step.values():
        for key in ("widget", "page", "exists"):
            for r in buckets.get(key, []):
                reason = _why_of(r).get("reason")
                if reason:
                    cnt[str(reason)] = cnt.get(str(reason), 0) + 1
    return dict(sorted(cnt.items(), key=lambda kv: -kv[1]))


def summarize_detail(detail, cand_max: int = 3) -> dict:
    """把定位器返回的 detail 压成一小段"为什么"（写进定位日志，供统计与界面用）。

    只保留判断原因必需的字段：各层 ok/分数/命中数、候选 top3（本身就紧凑）、被哪道门槛拒了。
    **绝不带图片数据** —— 定位日志是追加写的，塞进图片会迅速膨胀。
    返回里的 reason 是一句可聚合的代号，note 是给人看的一句话。
    """
    d = detail if isinstance(detail, dict) else {}
    out: dict = {"tried": [], "rejects": []}

    def _keep_layer(name, layer, keys):
        if not isinstance(layer, dict):
            return
        keep = {}
        for k in keys:
            if k in layer and layer[k] is not None:
                keep[k] = layer[k]
        if keep:
            out[name] = keep
        out["tried"].append(name)

    _keep_layer("l1", d.get("l1"), ("ok", "n_hits", "reason"))
    ocr = d.get("l2_ocr")
    if isinstance(ocr, dict):
        keep = {k: ocr[k] for k in ("ok", "confidence") if ocr.get(k) is not None}
        top3 = ocr.get("top3")
        if isinstance(top3, list) and top3:
            keep["cands"] = len(top3)
            keep["top3"] = top3[:cand_max]
        if isinstance(ocr.get("nearby_rejected"), dict):
            keep["nearby_rejected"] = ocr["nearby_rejected"]
        out["l2_ocr"] = keep
        out["tried"].append("l2_ocr")
    _keep_layer("l2_tpl", d.get("l2_tpl"), ("ok", "confidence"))
    if d.get("l2_tpl_best") is not None:
        out.setdefault("l2_tpl", {})["best"] = d["l2_tpl_best"]
    _keep_layer("l2_ring", d.get("l2_ring"), ("ok", "confidence"))
    if d.get("l2_ring_score") is not None:
        out.setdefault("l2_ring", {})["best"] = d["l2_ring_score"]
    _keep_layer("l3", d.get("l3"), ("verified", "verify_method", "nearby_ok"))
    _keep_layer("page_tpl", d.get("page_tpl"), ("score", "sim", "verdict"))
    if isinstance(d.get("anchor_consensus"), dict):
        out["anchor_consensus"] = d["anchor_consensus"]
        out["tried"].append("anchor")
    if isinstance(d.get("page_feature"), dict):
        out["page_feature"] = {k: v for k, v in list(d["page_feature"].items())[:6]}

    for key in ("l2_ocr_nearby_rejected", "l2_tpl_nearby_rejected", "l2_ring_nearby_rejected"):
        if d.get(key):
            out["rejects"].append(key)
    for key in ("l2_tpl_out_of_page", "l2_ring_out_of_page", "l2_ocr_out_of_page",
                "l3_out_of_page"):
        if d.get(key):
            out["rejects"].append(key)

    reason, note = _why_code(d, out)
    out["reason"] = reason
    out["note"] = note
    return out


def _why_code(d: dict, out: dict):
    """从 detail 归纳"首要原因"：给统计聚合用，也给人一句话。"""
    if out["rejects"] and any("nearby" in x for x in out["rejects"]):
        return ("nearby_rejected",
                "候选被“旁边的文字”这道门槛拒了：位置看着像，但旁边的字对不上")
    if any("out_of_page" in x for x in out["rejects"]):
        return ("out_of_page", "候选落在了页面框外面，被丢掉了")
    ocr = out.get("l2_ocr") or {}
    if ocr and not ocr.get("ok") and not ocr.get("cands"):
        return ("no_text_found", "这一页上没认出那个词（OCR 没有候选）")
    for name in ("l2_tpl", "l2_ring"):
        layer = out.get(name) or {}
        if layer and not layer.get("ok") and layer.get("best") is not None:
            return ("low_score", f"{name} 最像的只有 {layer['best']} 分，没到门槛")
    pt = out.get("page_tpl") or {}
    if pt and pt.get("score") is not None and not pt.get("verdict"):
        return ("page_template_miss", f"整页模板最高只有 {pt['score']} 分")
    if out.get("anchor_consensus"):
        ac = out["anchor_consensus"]
        if ac.get("tried") and not ac.get("ok"):
            return ("anchor_no_consensus", "锚点没形成共识（改动的区域太多）")
    l1 = out.get("l1") or {}
    if l1 and not l1.get("ok") and l1.get("reason"):
        return ("no_uia", f"UI 树里没有这个东西（{l1['reason']}）")
    if l1 and not l1.get("ok"):
        return ("no_uia", "UI 树里没有这个东西")
    return ("", "")


def _span_hours(rep: dict):
    """日志首尾时间跨度（小时）；解析不了返回 None。"""
    tr = rep.get("time_range") or []
    if len(tr) != 2:
        return None
    try:
        import datetime as dt
        a = dt.datetime.fromisoformat(str(tr[0]))
        b = dt.datetime.fromisoformat(str(tr[1]))
        return (b - a).total_seconds() / 3600.0
    except Exception:
        return None


def render_text(rep: dict) -> str:
    """控制台/界面用的中文报告。"""
    o = rep.get("overall", {})
    w, p, g = o.get("widget", {}), o.get("page", {}), o.get("guard", {})
    L = []
    L.append(f"定位日志：{rep.get('rows', 0)} 行"
             + (f"（{rep['time_range'][0]} → {rep['time_range'][1]}）" if rep.get("time_range") else ""))
    span = _span_hours(rep)
    if span is not None and span > 2:
        L.append(f"⚠ 这段日志跨了约 {span:.1f} 小时，很可能混了多次运行 —— 用 --tail 或 --since "
                 f"圈定一次运行再看，数字才对得上（否则早期的跑批会把大盘拉偏）。")
    L.append("")
    L.append(f"一次命中率（部件）：{w.get('hit_rate')}%  ({w.get('ok')}/{w.get('attempts')})")
    L.append(f"页面定位成功率    ：{p.get('hit_rate')}%  ({p.get('ok')}/{p.get('attempts')})")
    L.append(f"点击安全闸通过率  ：{g.get('pass_rate')}%  ({g.get('ok')}/{g.get('attempts')})")
    lv = w.get("levels") or {}
    total_ok = sum(lv.values())
    if total_ok:
        share = "、".join(f"{k} {_pct(v, total_ok)}%" for k, v in sorted(lv.items()))
        L.append(f"成功定位的层级占比：{share}")
    L.append(f"盲点率（靠第 3 层按位置点）：{w.get('blind_rate')}%  —— 越高越危险")
    L.append(f"各步首次就命中        ：{w.get('first_try_rate')}%")
    L.append(f"部件定位耗时 p50/p90  ：{w.get('p50_ms')} / {w.get('p90_ms')} ms")
    L.append(f"页面定位耗时 p50      ：{p.get('p50_ms')} ms")
    if o.get("fail_reasons"):
        L.append("")
        L.append("失败原因分布：")
        for k, v in o["fail_reasons"].items():
            L.append(f"  · {k}：{v} 次")
    if o.get("why_reasons"):
        L.append("")
        L.append("候选被谁拦下（why.reason）：")
        for k, v in o["why_reasons"].items():
            L.append(f"  · {k}：{v} 次")
    if o.get("page_fail_win"):
        L.append("")
        L.append("页面认不出来时的窗口状态（决定该修哪一头）：")
        for k, v in o["page_fail_win"].items():
            L.append(f"  · {k}：{v} 次")
    L.append("")
    L.append("按步骤：")
    for sid, s in rep.get("steps", {}).items():
        ww, pp, gg = s["widget"], s["page"], s["guard"]
        bits = [f"部件 {ww['ok']}/{ww['attempts']}"]
        if ww.get("levels"):
            bits.append("层级 " + "/".join(f"{k}:{v}" for k, v in ww["levels"].items()))
        if ww.get("blind"):
            bits.append(f"盲点 {ww['blind']}")
        if ww.get("p50_ms") is not None:
            bits.append(f"p50 {ww['p50_ms']}ms")
        bits.append(f"页面 {pp['ok']}/{pp['attempts']}")
        if gg["attempts"]:
            bits.append(f"闸门 {gg['ok']}/{gg['attempts']}")
        L.append(f"  {sid}: " + "，".join(bits))
        if s.get("fails"):
            L.append("      失败：" + "，".join(f"{k}×{v}" for k, v in s["fails"].items()))
    return "\n".join(L)


def render_markdown(rep: dict) -> str:
    """留档用的 Markdown（与 render_text 同口径）。"""
    o = rep.get("overall", {})
    w, p = o.get("widget", {}), o.get("page", {})
    L = ["# 定位质量统计", "",
         f"- 日志行数：{rep.get('rows', 0)}",
         f"- 时间范围：{rep.get('time_range')[0] if rep.get('time_range') else '-'} → "
         f"{rep.get('time_range')[1] if rep.get('time_range') else '-'}",
         "",
         "| 指标 | 数值 |", "| --- | --- |",
         f"| 一次命中率（部件） | {w.get('hit_rate')}% ({w.get('ok')}/{w.get('attempts')}) |",
         f"| 页面定位成功率 | {p.get('hit_rate')}% ({p.get('ok')}/{p.get('attempts')}) |",
         f"| 盲点率（第 3 层兜底） | {w.get('blind_rate')}% |",
         f"| 各步首次命中率 | {w.get('first_try_rate')}% |",
         f"| 部件定位耗时 p50/p90 | {w.get('p50_ms')} / {w.get('p90_ms')} ms |",
         f"| 页面定位耗时 p50 | {p.get('p50_ms')} ms |", "",
         "## 按步骤", "",
         "| 步骤 | 部件 ok/尝试 | 层级 | 盲点 | p50 ms | 页面 ok/尝试 | 失败原因 |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for sid, s in rep.get("steps", {}).items():
        ww, pp = s["widget"], s["page"]
        L.append(f"| `{sid}` | {ww['ok']}/{ww['attempts']} | "
                 + "/".join(f"{k}:{v}" for k, v in (ww.get("levels") or {}).items())
                 + f" | {ww.get('blind', 0)} | {ww.get('p50_ms')} | {pp['ok']}/{pp['attempts']} | "
                 + "，".join(f"{k}×{v}" for k, v in (s.get("fails") or {}).items()) + " |")
    if o.get("fail_reasons"):
        L += ["", "## 失败原因分布", ""] + [f"- {k}：{v} 次" for k, v in o["fail_reasons"].items()]
    if o.get("why_reasons"):
        L += ["", "## 候选被谁拦下（why.reason）", ""] + \
             [f"- {k}：{v} 次" for k, v in o["why_reasons"].items()]
    if o.get("page_fail_win"):
        L += ["", "## 页面认不出来时的窗口状态", ""] + \
             [f"- {k}：{v} 次" for k, v in o["page_fail_win"].items()]
    return "\n".join(L) + "\n"
