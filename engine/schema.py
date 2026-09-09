# -*- coding: utf-8 -*-
"""
engine.schema — E8：.sgscript.json v1.0 读写与校验。

规格权威：《项目分析文档》v0.31 §11 +《M1_引擎设计清单》§4。
本模块是纯逻辑（无 cv2/OCR 依赖）；图像一律 data-url 字符串，校验时不解码像素。

脚本顶层：{version, name?, targets_rev?, steps[]}
step: action / condition / loop（见 §11 示例与 §4.3/§5.2~5.4）
target（部件对象，内部字段见 4.3；完整持久化见 v0.31 §11 注）:
  { page?: PageSpec, image?, text?, match?, semantic?, rect_in_page?, center_in_page?, uia? }
PageSpec（v0.31 全量字段）:
  { context: {process?, title?, class?}, image(必), size[w,h], rect_in_screen?,
    anchors?: [{image, rect_in_page, stable_at?}], scale_range: [lo,hi],
    capture_meta?: {dpi?, ts?, cap_method?, visible?} }
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

SCRIPT_VERSION = "1.0"
DEFAULT_SCALE_RANGE = [0.8, 1.25]
ACTIONS = ("click", "dblclick", "type", "wait", "notify", "hotkey")   # MVP 六动作（§5.2）
MATCH_PREFS = ("auto", "text_first", "image_first")
ON_FAIL_STRATEGIES = ("retry", "notify", "stop", "skip")              # §6.3
NO_TARGET_ACTIONS = ("wait", "notify", "hotkey")
LOOP_MODES = ("count", "until", "forever")
_UUID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_DATAURL_PREFIX = "data:image/png;base64,"


# ---------------------------------------------------------------- 基础工具

def _errs_of(problems):
    from engine.errors import EngineError, ERRORS
    return EngineError("schema_invalid", ERRORS["schema_invalid"], {"errors": problems})


def _is_dataurl(v) -> bool:
    return isinstance(v, str) and v.startswith(_DATAURL_PREFIX)


def _dataurl_len_ok(v) -> bool:
    if not _is_dataurl(v):
        return False
    try:
        base64.b64decode(v[len(_DATAURL_PREFIX):], validate=True)
        return True
    except Exception:
        return False


def _rect_ok(v, label, problems) -> bool:
    if not (isinstance(v, (list, tuple)) and len(v) == 4 and
            all(isinstance(x, int) and not isinstance(x, bool) for x in v)):
        problems.append(f"{label} 需为 4 个整数 [x,y,w,h]")
        return False
    if v[2] <= 0 or v[3] <= 0:
        problems.append(f"{label} 宽高需 >0")
        return False
    return True


def _id_ok(v) -> bool:
    return isinstance(v, str) and bool(_UUID_RE.match(v))


# ---------------------------------------------------------------- 校验

def validate(sg) -> list:
    """返回问题条目列表（空 = 通过）。问题为字符串，不抛异常。"""
    problems = []
    if not isinstance(sg, dict):
        return ["脚本顶层必须是 JSON 对象"]
    if sg.get("version") != SCRIPT_VERSION:
        problems.append(f"version 必须为 \"{SCRIPT_VERSION}\"（当前 {sg.get('version')!r}）")
    steps = sg.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps 必须是非空数组")
        return problems
    ids = set()

    def _check_target(t, where, require_page_image=False):
        if not isinstance(t, dict):
            problems.append(f"{where}: target 必须是对象")
            return
        page = t.get("page")
        if page is not None:
            if not isinstance(page, dict):
                problems.append(f"{where}: target.page 必须是对象")
            else:
                if not _is_dataurl(page.get("image")):
                    problems.append(f"{where}: target.page.image 必须是非空 PNG data-url")
                size = page.get("size")
                if not (isinstance(size, (list, tuple)) and len(size) == 2 and
                        all(isinstance(x, int) and x > 0 for x in size)):
                    problems.append(f"{where}: target.page.size 需为 [宽,高] 正整数")
                sr = page.get("scale_range", DEFAULT_SCALE_RANGE)
                if not (isinstance(sr, (list, tuple)) and len(sr) == 2 and
                        all(isinstance(x, (int, float)) and x > 0 for x in sr) and sr[0] <= sr[1]):
                    problems.append(f"{where}: target.page.scale_range 需为 [lo,hi] 且 0<lo<=hi")
                ctx = page.get("context")
                if ctx is not None and not isinstance(ctx, dict):
                    problems.append(f"{where}: target.page.context 需为对象")
                anchors = page.get("anchors")
                if anchors is not None:
                    if not isinstance(anchors, list):
                        problems.append(f"{where}: target.page.anchors 需为数组")
                    else:
                        for i, a in enumerate(anchors):
                            if not isinstance(a, dict) or not _is_dataurl(a.get("image")):
                                problems.append(f"{where}: anchors[{i}].image 需为 PNG data-url")
                            _rect_ok(a.get("rect_in_page"), f"{where}: anchors[{i}].rect_in_page", problems)
        has_signal = False
        if isinstance(t.get("text"), str) and t["text"].strip():
            has_signal = True
        if _is_dataurl(t.get("image")):
            has_signal = True
        if isinstance(t.get("uia"), dict) and (t["uia"].get("name") or t["uia"].get("automation_id")):
            has_signal = True
        if not has_signal and page is None:
            problems.append(f"{where}: target 至少需要 text/image/uia 之一（或所属 page）")
        match = t.get("match", "auto")
        if match not in MATCH_PREFS:
            problems.append(f"{where}: target.match 需为 {MATCH_PREFS}（当前 {match!r}）")
        if "rect_in_page" in t:
            _rect_ok(t["rect_in_page"], f"{where}: rect_in_page", problems)
        if "center_in_page" in t:
            c = t["center_in_page"]
            if not (isinstance(c, (list, tuple)) and len(c) == 2 and
                    all(isinstance(x, int) for x in c)):
                problems.append(f"{where}: center_in_page 需为 2 整数")

    def _check_steps(arr, where):
        for i, st in enumerate(arr):
            w = f"{where}[{i}]"
            if not isinstance(st, dict):
                problems.append(f"{w}: 步骤必须是对象")
                continue
            sid = st.get("id")
            if not _id_ok(sid):
                problems.append(f"{w}: id 缺失或非法（{sid!r}）")
            elif sid in ids:
                problems.append(f"{w}: id {sid!r} 重复")
            ids.add(sid)
            typ = st.get("type")
            if typ == "action":
                act = st.get("action")
                if act not in ACTIONS:
                    problems.append(f"{w}: action 需为 {ACTIONS} 之一")
                    continue
                params = st.get("params")
                if params is None:
                    params = st.setdefault("params", {})
                if not isinstance(params, dict):
                    problems.append(f"{w}: params 需为对象")
                if act == "type" and not (isinstance(params.get("text"), str) and params["text"] != ""):
                    problems.append(f"{w}: 输入文字步骤的 params.text 不能为空")
                if act == "wait":
                    secs = params.get("seconds")
                    if not (isinstance(secs, (int, float)) and not isinstance(secs, bool) and secs > 0):
                        problems.append(f"{w}: 等待步骤的 params.seconds 需 >0")
                if act == "notify" and not isinstance(params.get("message", ""), str):
                    problems.append(f"{w}: 提示我的 params.message 需为字符串")
                if act == "hotkey" and not (isinstance(params.get("keys"), str) and params["keys"].strip()):
                    problems.append(f"{w}: 快捷键步骤的 params.keys 不能为空")
                if act not in NO_TARGET_ACTIONS:
                    _check_target(st.get("target") or {}, f"{w}.target")
                _check_outcome(st, w)
            elif typ == "condition":
                cond = st.get("condition")
                if not isinstance(cond, dict):
                    problems.append(f"{w}: condition 需为对象")
                else:
                    _check_target(cond.get("target") or {}, f"{w}.condition.target")
                    if "exists" in cond and not isinstance(cond["exists"], bool):
                        problems.append(f"{w}: condition.exists 需为布尔")
                if not isinstance(st.get("then"), list) or not isinstance(st.get("else"), list):
                    problems.append(f"{w}: condition 需含 then/else 数组")
                else:
                    _check_steps(st["then"], f"{w}.then")
                    _check_steps(st["else"], f"{w}.else")
            elif typ == "loop":
                lp = st.get("loop")
                if not isinstance(lp, dict) or lp.get("mode") not in LOOP_MODES:
                    problems.append(f"{w}: loop.mode 需为 {LOOP_MODES} 之一")
                else:
                    mode = lp["mode"]
                    if mode == "count":
                        n = lp.get("count")
                        if not (isinstance(n, int) and not isinstance(n, bool) and n >= 0):
                            problems.append(f"{w}: loop.count 需为非负整数")
                    elif mode == "until":
                        _check_target(lp.get("target") or {}, f"{w}.loop.target")
                        if "exists" in lp and not isinstance(lp["exists"], bool):
                            problems.append(f"{w}: loop.exists 需为布尔")
                    if not isinstance(st.get("body"), list):
                        problems.append(f"{w}: loop 需含 body 数组")
                    else:
                        _check_steps(st["body"], f"{w}.body")
            else:
                problems.append(f"{w}: type 需为 action/condition/loop（当前 {typ!r}）")

    def _check_outcome(st, w):
        eo = st.get("expected_outcome")
        if eo is None:
            return
        if not isinstance(eo, dict):
            problems.append(f"{w}: expected_outcome 需为对象")
            return
        _check_target(eo.get("target") or {}, f"{w}.expected_outcome.target")
        of = eo.get("on_fail")
        if of is not None:
            if not isinstance(of, dict) or of.get("strategy") not in ON_FAIL_STRATEGIES:
                problems.append(f"{w}: on_fail.strategy 需为 {ON_FAIL_STRATEGIES} 之一")
            if not isinstance(of.get("message", ""), str):
                problems.append(f"{w}: on_fail.message 需为字符串")
            rt = of.get("retry")
            if rt is not None:
                if not isinstance(rt, dict):
                    problems.append(f"{w}: on_fail.retry 需为对象")
                else:
                    n = rt.get("times", 3)
                    if not (isinstance(n, int) and not isinstance(n, bool) and 0 < n <= 100):
                        problems.append(f"{w}: on_fail.retry.times 需为 1~100 整数")

    _check_steps(steps, "steps")
    return problems


def check(sg) -> None:
    """校验失败抛 EngineError(schema_invalid)。"""
    problems = validate(sg)
    if problems:
        raise _errs_of(problems)


# ---------------------------------------------------------------- 读写

def load(path) -> dict:
    p = Path(path)
    if not p.exists():
        from engine.errors import EngineError, ERRORS
        raise EngineError("script_not_found", ERRORS["script_not_found"], {"path": str(p)})
    with open(p, encoding="utf-8") as f:
        try:
            sg = json.load(f)
        except json.JSONDecodeError as e:
            from engine.errors import EngineError, ERRORS
            raise EngineError("schema_invalid", f"JSON 解析失败: {e}", {"path": str(p)})
    check(sg)
    return sg


def dump(sg, path=None) -> str:
    text = json.dumps(sg, ensure_ascii=False, indent=2)
    if path is not None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return text


def new_script(name: str = "", targets_rev: int = 0) -> dict:
    return {"version": SCRIPT_VERSION, "name": name, "targets_rev": targets_rev, "steps": []}


# ---------------------------------------------------------------- 构造助手（录制/校验写回用）

def page_spec(*, image_dataurl: str, size, context=None, rect_in_screen=None,
              anchors=None, scale_range=DEFAULT_SCALE_RANGE, capture_meta=None) -> dict:
    spec = {"context": context or {}, "image": image_dataurl, "size": [int(size[0]), int(size[1])],
            "rect_in_screen": [int(x) for x in rect_in_screen] if rect_in_screen else None,
            "anchors": anchors or [], "scale_range": [float(scale_range[0]), float(scale_range[1])],
            "capture_meta": capture_meta or {}}
    # anchors/scale_range 恒保留；其余可选字段空值剔除（写盘更干净）
    return {k: v for k, v in spec.items() if k in ("anchors", "scale_range")
            or v is not None and v != {}}


def anchor_spec(*, image_dataurl: str, rect_in_page, stable_at: str = "") -> dict:
    return {"image": image_dataurl, "rect_in_page": [int(x) for x in rect_in_page],
            "stable_at": stable_at}


def widget_target(*, page=None, image_dataurl=None, text="", match="auto", semantic="",
                  rect_in_page=None, center_in_page=None, uia=None) -> dict:
    t = {}
    if page is not None:
        t["page"] = page
    if image_dataurl:
        t["image"] = image_dataurl
    if text:
        t["text"] = text
    if semantic:
        t["semantic"] = semantic
    if match != "auto":
        t["match"] = match
    if rect_in_page is not None:
        t["rect_in_page"] = [int(x) for x in rect_in_page]
    if center_in_page is not None:
        t["center_in_page"] = [int(x) for x in center_in_page]
    if uia:
        t["uia"] = uia
    return t
