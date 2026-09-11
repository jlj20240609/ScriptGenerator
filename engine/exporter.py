# -*- coding: utf-8 -*-
"""
engine.exporter — 脚本 → Skill + Tools 确定性导出器（M4-WP1，分析文档 §8）。

本质是**函数拼装 + 加修饰**（§8.1）：
  脚本的步骤序列  → 一条函数调用链（**导出时**拼好，模式 A，§8.3）
  写死的字面量    → skill 入参（§8.4）
  步骤引用的部件  → 内嵌的 WIDGETS 注册表（§8.7）
  原子函数        → @tool 修饰；拼好的链 → @skill 修饰（§8.1）

两条必须说清楚的边界（照抄 §8.2 的警告，因为它决定了产物怎么用）：
  1. **导出物不是自包含代码**：skill/tools 里只有"调用编排 + 部件数据"；
     识别（mss 截图 / RapidOCR / OpenCV 相似度 / UIA）与输入（SendInput）仍在
     **本机引擎**上执行。所以 screen.py 里的函数是"转发到本机引擎"，不是自己实现。
  2. 运行时每次动作前仍要"看屏幕找部件"（本地三级定位）。**云端不参与坐标**——
     这条是 M0 就定下的架构边界，导出功能不放松。

为什么这块先做且必须纯逻辑：它是 M4 的地基（目录适配、AI 转译、向导都建在它上面），
而且"拼得对不对、引用全不全"完全可以离线判定——留在真机上试是浪费时间。
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from engine import schema

# 原子操作 → 引擎侧函数名（§8.2 的 Tools 表）
ACTION_TOOLS = {
    "click": "click",
    "dblclick": "dblclick",
    "type": "type_text",
    "wait": "wait",
    "notify": "notify",
    "hotkey": "hotkey",
    "stop": "stop",
}

# 导出时静态确定的 TOOLS 清单里，除步骤用到的之外**恒定**要有的两个（§8.2/§8.5）：
#   find_target：每次动作前"看屏幕找部件"
#   verify_result：结果判定器（错误的自主恢复全靠它）
BASE_TOOLS = ("find_target", "verify_result")

# 会按"部件名"调用的工具：引用完整性校验就查它们的第一参数（见 verify_product）
WIDGET_FUNCS = ("find_target", "click", "dblclick", "type_text", "verify_result")

# screen.py 里的工具函数源码片段。做成表是为了能按"目标 Agent 已有这些工具"逐个跳过
# （§8.6 求差集：已有就复用、不覆盖），而不是每次重写一大段模板。
_TOOL_SRC = {
    "find_target": '''
def find_target(name):
    """在操作页面内找部件（本地三级定位），返回 {"exists", "box", "method", "confidence"}。"""
    return _call("widget.locate", {"target": widget(name)})
''',
    "click": '''
def click(name):
    """点一下某个部件（先本地定位再点）。"""
    return _call("input.click", {"target": widget(name)})
''',
    "dblclick": '''
def dblclick(name):
    """点两下某个部件。"""
    return _call("input.click", {"target": widget(name), "dbl": True})
''',
    "type_text": '''
def type_text(name, text):
    """在部件处输入文字（Unicode 直发，绕开输入法）。"""
    return _call("input.type", {"target": widget(name), "text": text})
''',
    "wait": '''
def wait(seconds):
    """等待若干秒。"""
    import time as _t
    _t.sleep(float(seconds))
    return {"ok": True}
''',
    "hotkey": '''
def hotkey(keys):
    """按组合键。"""
    return _call("input.hotkey", {"keys": keys})
''',
    "notify": '''
def notify(message):
    """弹白话提示并暂停，等用户处理。"""
    return _call("ui.notify", {"message": message})
''',
    "verify_result": '''
def verify_result(name, on_fail="notify"):
    """结果判定（本地 D2 + 云端 D3）：成功 / 失败(类型) / 未知。"""
    return _call("outcome.verify", {"target": widget(name), "on_fail": on_fail})
''',
    "screenshot": '''
def screenshot(path=None):
    """截当前屏幕（可交给多模态模型）。"""
    return _call("page.capture", {"save": path} if path else {})
''',
    "run_script": '''
def run_script(task):
    """把脚本交给本机引擎运行。"""
    return _call("script.run", {"script": task})
''',
}

# 被跳过（复用目标 Agent 已有实现）时留下的转发壳
_REUSE_STUB = '''
def {name}(*a, **kw):
    """这个工具目标 Agent 已经注册了，导出时**跳过了实现**（§8.6 求差集）。

    这里不重新实现，转发给对方的实现——覆盖别人已装好的工具是不可接受的。
    若语义不一致，请在导出选项里关掉「复用已有工具」。
    """
    return _agent_tool("{name}")(*a, **kw)
'''

_AGENT_HOOK = '''

_AGENT_TOOLS = {}


def _agent_tool(name):
    fn = _AGENT_TOOLS.get(name)
    if fn is None:
        raise RuntimeError(
            "工具 %s 用的是目标 Agent 里已有的实现，但启动时没有注册。"
            "请调用 tools.screen.bind_agent_tools({'%s': 你的函数})，"
            "或重新导出并关掉「复用已有工具」。" % (name, name))
    return fn


def bind_agent_tools(mapping):
    """把目标 Agent 已有的同名工具接进来（§8.6 复用）。"""
    _AGENT_TOOLS.update(mapping or {})
    return _AGENT_TOOLS
'''

EXPORT_VERSION = "1"

# 可参数化的字面量：脚本里写死的值 → skill 入参（§8.4）
PARAM_RULES = (
    ("type", "text"),          # 输入的文字
    ("wait", "seconds"),       # 等待时长
)


# --------------------------------------------------------------------- 小工具

def slug(name: str, fallback: str = "script") -> str:
    """脚本名 → 合法 Python 标识符（skill 名/函数名/文件名用）。

    中文名**保留**：Python 3 的标识符与模块名都允许 Unicode，而本产品的脚本名多半
    就是中文（"自动登录"）。早先按 ASCII 白名单过滤，结果所有中文名都退化成 "script"——
    导出的 skill 全叫同一个名字，等于没名字。
    """
    s = re.sub(r"[^\w]+", "_", str(name or ""), flags=re.UNICODE).strip("_")
    if not s:
        s = fallback
    if s[0].isdigit():
        s = "s_" + s
    return s[:48]


def _img_key(target: dict) -> str:
    """部件图像的指纹（去重合并用；base64 很长，不能直接当 key）。"""
    img = target.get("image") or ""
    return hashlib.sha1(img.encode("utf-8")).hexdigest()[:12] if img else ""


def _widget_key(target: dict) -> tuple:
    """两个 target 是不是"同一个部件"：看页面上下文 + 文字 + 图像指纹 + 坐标。"""
    page = target.get("page") or {}
    ctx = page.get("context") or {}
    r = target.get("rect_in_page") or []
    return (ctx.get("title", ""), ctx.get("class", ""), (target.get("text") or "").strip(),
            _img_key(target), tuple(r))


def _iter_targets(st: dict):
    """产出步骤里所有 target（含条件/循环/预期结果里的）。"""
    if not isinstance(st, dict):
        return
    if st.get("type") == "action":
        if st.get("target"):
            yield st["target"]
        eo = st.get("expected_outcome") or {}
        if eo.get("target"):
            yield eo["target"]
    elif st.get("type") == "condition":
        cond = st.get("condition") or {}
        if cond.get("target"):
            yield cond["target"]
    elif st.get("type") == "loop":
        lp = st.get("loop") or {}
        if lp.get("target"):
            yield lp["target"]


def walk_steps(steps):
    """深度遍历所有步骤（含条件分支与循环体）。"""
    for st in steps or []:
        if not isinstance(st, dict):
            continue
        yield st
        if st.get("type") == "condition":
            yield from walk_steps(st.get("then") or [])
            yield from walk_steps(st.get("else") or [])
        elif st.get("type") == "loop":
            yield from walk_steps(st.get("body") or [])


# --------------------------------------------------------------------- 部件注册表

def collect_widgets(sg: dict, names=None) -> dict:
    """收集脚本用到的全部部件 → WIDGETS 注册表（全局去重合并，§8.7）。

    names: 可选 {widget_key: 名字}，允许调用方（界面/AI）指定更好懂的名字。
    返回 {名字: {page, uia, text, images, coord, match, semantic, nearby}}
    """
    reg = {}
    by_key = {}
    for st in walk_steps(sg.get("steps")):
        for t in _iter_targets(st):
            key = _widget_key(t)
            if key in by_key:
                continue
            page = t.get("page") or {}
            ctx = page.get("context") or {}
            entry = {}
            if ctx:
                entry["page"] = {k: ctx.get(k, "") for k in ("process", "title", "class")
                                 if ctx.get(k)}
            if t.get("uia"):
                entry["uia"] = t["uia"]
            if t.get("text"):
                entry["text"] = t["text"]
            if t.get("image"):
                entry["images"] = [t["image"]]
            if t.get("rect_in_page"):
                x, y, w, h = [int(v) for v in t["rect_in_page"]]
                entry["coord"] = {"x": x, "y": y, "w": w, "h": h}
            if t.get("match") and t["match"] != "auto":
                entry["match"] = t["match"]
            if t.get("semantic"):
                entry["semantic"] = t["semantic"]
            if t.get("nearby"):
                entry["nearby"] = t["nearby"]
            name = (names or {}).get(key) or _name_of(t, reg)
            reg[name] = entry
            by_key[key] = name
    return reg


def _name_of(target: dict, reg: dict) -> str:
    """给部件起个名字：优先用它自己的文字（§8.7 的"与脚本 target 同名引用"）。

    重名时加序号——名字必须唯一，否则 skill 里引用会指错部件。
    """
    base = (target.get("text") or "").strip() or "部件"
    base = re.sub(r"\s+", "", base)[:20] or "部件"
    if base not in reg:
        return base
    i = 2
    while f"{base}_{i}" in reg:
        i += 1
    return f"{base}_{i}"


# --------------------------------------------------------------------- 参数化

def collect_params(sg: dict, names=None) -> dict:
    """把写死的字面量识别为**可参数化点**（§8.4）。

    names: 可选 {(step_id, 字段): 入参名}，让界面/AI 把 p_1 改成 username 这种好名字。
    返回 {入参名: {default, step, field, where}}；按脚本出现顺序稳定编号。
    """
    out = {}
    seq = 0
    for st in walk_steps(sg.get("steps")):
        if st.get("type") != "action":
            continue
        act = st.get("action")
        params = st.get("params") or {}
        for a, field in PARAM_RULES:
            if act != a or field not in params:
                continue
            seq += 1
            sid = str(st.get("id") or f"step{seq}")
            name = (names or {}).get((sid, field)) or f"p_{seq}"
            where = ((st.get("target") or {}).get("text") or "").strip() or act
            out[name] = {"default": params[field], "step": sid, "field": field,
                         "action": act, "where": where}
    return out


def apply_params(sg: dict, values: dict, spec: dict) -> dict:
    """按参数表把值填回脚本（skill 的 build_task 就靠它）。

    改了脚本的副本，不动原脚本（导出的产物是快照，§8.3）。
    """
    out = json.loads(json.dumps(sg))

    def walk(steps):
        for st in steps:
            if st.get("type") == "action":
                for name, meta in spec.items():
                    if meta["step"] != st.get("id") or meta["field"] not in ("text", "seconds"):
                        continue
                    if name in values:
                        st.setdefault("params", {})[meta["field"]] = values[name]
            elif st.get("type") == "condition":
                walk(st.get("then") or [])
                walk(st.get("else") or [])
            elif st.get("type") == "loop":
                walk(st.get("body") or [])
    walk(out.get("steps") or [])
    return out


# --------------------------------------------------------------------- 拼装（步骤 → 调用链）

def _call_of(st: dict, widgets: dict) -> str:
    """一个步骤 → 一行（或一块）函数调用源码。"""
    act = st.get("action")
    fn = ACTION_TOOLS.get(act, act)
    p = st.get("params") or {}
    tname = _widget_ref(st.get("target") or {}, widgets)
    if act == "type":
        return f'    type_text({tname}, {_lit(p.get("text", ""))})  # 输入文字'
    if act == "wait":
        return f'    wait({_lit(p.get("seconds", 1))})  # 等一下'
    if act == "notify":
        return f'    notify({_lit(p.get("message", ""))})  # 提示我（会暂停等用户处理）'
    if act == "hotkey":
        return f'    hotkey({_lit(p.get("keys", ""))})  # 按快捷键'
    if act == "stop":
        return "    stop()  # 停止"
    if act == "dblclick":
        return f'    dblclick({tname})  # 点两下'
    return f'    click({tname})  # 点一下'


def _widget_ref(target: dict, widgets: dict) -> str:
    """target → 源码里的部件名引用（字符串字面量，指向 WIDGETS 的键）。"""
    if not target:
        return "None"
    key = _widget_key(target)
    for name, entry in widgets.items():
        if _entry_matches_key(entry, key):
            return _lit(name)
    # 没进注册表（理论上不该发生）→ 明确报出来而不是悄悄编一个名字
    return _lit("<未注册部件>")


def _entry_matches_key(entry: dict, key: tuple) -> bool:
    page = entry.get("page") or {}
    coord = entry.get("coord") or {}
    got = (page.get("title", ""), page.get("class", ""), entry.get("text", ""),
           _img_key({"image": (entry.get("images") or [""])[0]}),
           (coord.get("x"), coord.get("y"), coord.get("w"), coord.get("h")) if coord else ())
    want = (key[0], key[1], key[2], key[3], tuple(key[4]))
    return got == want


def _lit(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def render_chain(sg: dict, widgets: dict, indent: int = 1) -> str:
    """整条调用链的源码（条件/循环翻译成 if/for/while）。"""
    pad = "    " * indent
    lines = []
    for st in sg.get("steps") or []:
        typ = st.get("type")
        if typ == "action":
            lines.append(pad + _call_of(st, widgets).lstrip())
            eo = st.get("expected_outcome") or {}
            if eo.get("target"):
                otn = _widget_ref(eo["target"], widgets)
                of = (eo.get("on_fail") or {}).get("strategy", "notify")
                lines.append(f"{pad}# 做完后应该看到 {otn}")
                lines.append(f"{pad}verify_result({otn}, on_fail={_lit(of)})")
        elif typ == "condition":
            cond = st.get("condition") or {}
            ctn = _widget_ref(cond.get("target") or {}, widgets)
            neg = "" if cond.get("exists", True) else "not "
            lines.append(f"{pad}if {neg}find_target({ctn})[\"exists\"]:  # 如果{'没' if neg else ''}看到")
            lines.append(render_chain({"steps": st.get("then") or []}, widgets, indent + 1)
                         or pad + "    pass")
            els = st.get("else") or []
            if els:
                lines.append(f"{pad}else:")
                lines.append(render_chain({"steps": els}, widgets, indent + 1)
                             or pad + "    pass")
        elif typ == "loop":
            lp = st.get("loop") or {}
            mode = lp.get("mode")
            if mode == "count":
                lines.append(f"{pad}for _i in range({int(lp.get('count', 1))}):  # 重复 N 次")
            elif mode == "until":
                ltn = _widget_ref(lp.get("target") or {}, widgets)
                neg = "" if lp.get("exists", True) else "not "
                lines.append(f"{pad}while {neg}find_target({ltn})[\"exists\"]:  # 一直重复，直到看到")
            else:
                lines.append(f"{pad}while True:  # 一直重复（靠 stop 或人工停止）")
            lines.append(render_chain({"steps": st.get("body") or []}, widgets, indent + 1)
                         or pad + "    pass")
    return "\n".join(lines)


# --------------------------------------------------------------------- 渲染产物

def describe(sg: dict) -> str:
    """从步骤自动总结一句 description（§8.2 的 skill 三要素之一）。"""
    names = []
    for st in sg.get("steps") or []:
        typ = st.get("type")
        if typ == "action":
            act = st.get("action")
            tw = ((st.get("target") or {}).get("text") or "").strip()
            label = {"click": "点一下", "dblclick": "点两下", "type": "输入文字",
                     "wait": "等一下", "notify": "提示我", "hotkey": "按快捷键",
                     "stop": "停止"}.get(act, str(act))
            names.append(f"{label}「{tw}」" if tw else label)
        elif typ == "condition":
            names.append("看一下条件")
        elif typ == "loop":
            names.append("重复若干次")
    head = "、".join(names[:6])
    return (f"{sg.get('name') or '脚本'}：{head}" + ("…" if len(names) > 6 else ""))[:180]


def render_skill(sg: dict, widgets: dict, params: dict, engine_entry: str,
                 opts=None) -> str:
    """渲染 skills/<name>.py。"""
    opts = opts or {}
    name = slug(sg.get("name"), "script")
    tools = sorted({ACTION_TOOLS.get(st.get("action"), "") for st in walk_steps(sg.get("steps"))
                    if st.get("type") == "action"} | set(BASE_TOOLS) - {""})
    # hotkey/stop 之类没有对应工具的收尾动作不进 TOOLS 清单
    tools = [t for t in tools if t]
    plist = ", ".join(f"{k}={_lit(v['default'])}" for k, v in params.items()) or ""
    values = ", ".join(f"{_lit(k)}: {k}" for k in params)
    args = ", ".join(f"{k}={k}" for k in params)
    param_doc = "\n".join(
        f"#   {k}: {v['where']}（{v['action']}；默认值 {v['default']!r}）"
        for k, v in params.items()) or "#   （这个脚本没有可改的参数）"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return f'''# -*- coding: utf-8 -*-
"""由「脚本构建器」导出的 Skill —— **单向导出**，请勿手工改动后回写脚本。

来源脚本：{sg.get("name") or "未命名脚本"}（targets_rev={sg.get("targets_rev", 0)}）
导出时间：{stamp}
导出格式版本：v{EXPORT_VERSION}

⚠️ 这个文件**不是自包含代码**：它只含"调用编排"，识别（截图/OCR/相似度/UI 树）
与输入（键鼠模拟）都在**本机引擎**上执行——Agent 与本产品同机时，tools/screen.py
会经由 {engine_entry} 调用引擎。运行时每次动作前仍会"看屏幕找部件"（本地三级定位），
**云端 AI 不参与坐标**。

改脚本后请**重新导出**（导出不反向同步）。
"""

SKILL_NAME = {_lit(name)}
SKILL_DESCRIPTION = {_lit(describe(sg))}
SYSTEM_PROMPT = """你是这个自动化脚本的执行助手。
用户把「{sg.get("name") or "未命名脚本"}」这件事做成了脚本，你负责调用它。
需要参数时按用户给的值调用；用户没给就用默认值。
跑完看 verify_result 的结论：失败要如实告诉用户是哪一类失败，不要假装成功。"""

# 这个 skill 用到的工具（**导出时静态确定**，§8.3）
TOOLS = {json.dumps(tools, ensure_ascii=False)}

# 可参数化点（脚本里写死的值，导出时提升为入参，§8.4）：
{param_doc}

from tools.screen import fill as _fill, run_script   # noqa: E402


def build_task({plist}):
    """把参数填进脚本，返回**可直接交给本机引擎运行**的脚本对象。

    这是 Agent 侧的入口：它不自己执行，而是产出脚本 JSON 交给引擎
    （识别与输入都在本机引擎里，见文件头的说明）。
    """
    return _fill({{{values}}})


def run({plist}):
    """按参数运行脚本（把 build_task 的结果交给本机引擎）。"""
    return run_script(build_task({args}))
'''


def render_tools(widgets: dict, sg: dict, engine_entry: str, opts=None) -> dict:
    """渲染 tools/screen.py 与 tools/registry.py，返回 {相对路径: 源码}。

    opts.skip_tools：目标 Agent **已有的同名工具**（§8.6 求差集：已有就复用、不重复写）。
    被跳过的工具在产物里只留一行说明，不重新定义——覆盖别人的实现是不可接受的。
    """
    opts = opts or {}
    skip = {str(t) for t in (opts.get("skip_tools") or [])} & set(_TOOL_SRC)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    src = sg  # 快照：导出的脚本原文（build_task 的填充基础）
    # 组装工具函数：跳过的留转发壳，其余的给完整实现
    tool_defs = "".join(
        (_REUSE_STUB.format(name=n) if n in skip else _TOOL_SRC[n])
        for n in _TOOL_SRC)
    reuse_block = _AGENT_HOOK if skip else ""
    reuse_note = ("\n# 以下工具目标 Agent 已有，导出时跳过实现、转发给它："
                  f"{'、'.join(sorted(skip))}\n" if skip else "")
    screen = f'''# -*- coding: utf-8 -*-
"""由「脚本构建器」导出的 Tools —— 原子操作 + 部件数据（单向导出，勿回写）。

来源脚本：{sg.get("name") or "未命名脚本"}　导出时间：{stamp}
⚠️ **不是自包含实现**：这里只有"调用编排 + 部件数据（WIDGETS）"；
真正的识别（mss 截图 / RapidOCR / OpenCV / UIA）与输入（SendInput）在**本机引擎**里，
本文件通过 {engine_entry} 调用它。运行时每次动作前仍会"看屏幕找部件"。
"""
import json
import subprocess
import sys

ENGINE_ENTRY = {_lit(engine_entry)}

# 本脚本用到的部件（导出时内嵌；同名部件已全局去重，§8.7）
WIDGETS = json.loads(r"""{json.dumps(widgets, ensure_ascii=False)}""")
{reuse_note}

# 导出时的脚本快照（build_task 在它基础上填参数）
SCRIPT_SNAPSHOT = json.loads(r"""{json.dumps(src, ensure_ascii=False)}""")


def _call(method, params):
    """把一次调用转给本机引擎（JSON-Lines IPC，一行一请求）。"""
    req = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}})
    p = subprocess.run([sys.executable, ENGINE_ENTRY], input=req + "\\n",
                       capture_output=True, text=True, encoding="utf-8")
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 1:
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message") or "引擎调用失败")
            return msg.get("result") or {{}}
    raise RuntimeError("引擎没有回应：%s" % (p.stderr or "")[:200])


def widget(name):
    """按名字取部件数据。"""
    if name not in WIDGETS:
        raise KeyError("没有这个部件：%s" % name)
    return WIDGETS[name]

{reuse_block}{tool_defs}


def _fill(values):
    """把参数值填回脚本快照（skill 的 build_task 用它）。"""
    import copy
    out = copy.deepcopy(SCRIPT_SNAPSHOT)
    for name, val in (values or {{}}).items():
        for key, meta in (PARAM_SPEC or {{}}).items():
            if key == name:
                _set(out, meta["step"], meta["field"], val)
    return out


PARAM_SPEC = json.loads(r"""{json.dumps(opts.get("param_spec") or {}, ensure_ascii=False)}""")


def _set(task, step_id, field, value):
    def walk(steps):
        for st in steps:
            if st.get("id") == step_id:
                st.setdefault("params", {{}})[field] = value
            elif st.get("type") == "condition":
                walk(st.get("then") or []); walk(st.get("else") or [])
            elif st.get("type") == "loop":
                walk(st.get("body") or [])
    walk(task.get("steps") or [])


def fill(values):
    """按参数名填脚本（skill 用）。"""
    return _fill(values)
'''
    registry = f'''# -*- coding: utf-8 -*-
"""Python 函数 → LLM tool schema（导出产物的一部分，单向导出）。

来源脚本：{sg.get("name") or "未命名脚本"}　导出时间：{stamp}

工具清单是**导出时静态确定**的（§8.3）：{json.dumps(sorted(set(BASE_TOOLS)), ensure_ascii=False)}
每个工具的实现都在本机引擎里，这里只给 Agent 一份可注册的 schema。
"""

TOOL_SCHEMAS = [
    {{"type": "function", "function": {{
        "name": "find_target",
        "description": "在指定操作页面内定位部件，返回点击点（本地三级降级：UI 树 → 部件相似度 → 页面内坐标）",
        "parameters": {{"type": "object", "properties": {{"name": {{"type": "string", "description": "部件名（见 screen.WIDGETS）"}}}}, "required": ["name"]}},
    }}}},
    {{"type": "function", "function": {{
        "name": "click",
        "description": "点击某个部件（会先定位再点）",
        "parameters": {{"type": "object", "properties": {{"name": {{"type": "string"}}}}, "required": ["name"]}},
    }}}},
    {{"type": "function", "function": {{
        "name": "type_text",
        "description": "在某个部件处输入文字",
        "parameters": {{"type": "object", "properties": {{"name": {{"type": "string"}}, "text": {{"type": "string"}}}}, "required": ["name", "text"]}},
    }}}},
    {{"type": "function", "function": {{
        "name": "wait",
        "description": "等待若干秒",
        "parameters": {{"type": "object", "properties": {{"seconds": {{"type": "number"}}}}, "required": ["seconds"]}},
    }}}},
    {{"type": "function", "function": {{
        "name": "notify",
        "description": "弹出白话提示并暂停，等待用户处理（脚本需要用户介入时调用）",
        "parameters": {{"type": "object", "properties": {{"message": {{"type": "string"}}}}, "required": ["message"]}},
    }}}},
    {{"type": "function", "function": {{
        "name": "verify_result",
        "description": "判断操作结果：成功 / 失败(类型) / 未知",
        "parameters": {{"type": "object", "properties": {{"name": {{"type": "string"}}}}, "required": ["name"]}},
    }}}},
]


def schemas():
    return TOOL_SCHEMAS
'''
    return {"tools/screen.py": screen, "tools/registry.py": registry}


# --------------------------------------------------------------------- 引用完整性

def _widget_refs(src: str):
    """从源码里找出"按部件名调用"的地方 → [(部件名, 行号)]。

    为什么要用 AST 而不是字符串搜索：**AI 转译（出口 ②）的产物可能引用一个不存在的
    部件**，那正是 DoD 要求拦下来的东西；字符串搜索分不清"引用"和"注释/文档里提到的名字"，
    会既漏报又误报。
    """
    import ast
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out                      # 语法错由编译检查另报
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fn not in WIDGET_FUNCS or not node.args:
            continue
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append((a.value, getattr(node, "lineno", 0)))
    return out


def verify_product(files: dict, sg: dict, widgets: dict, params: dict) -> list:
    """引用完整性校验（DoD 要求）：产物引用的部件必须都有数据，且**能编译**。

    为什么必须做：AI 转译可能引用一个不存在的部件，产物一跑就崩；在导出前拦下来，
    用户看到的是"这里有问题"，而不是运行时莫名其妙的报错。
    """
    problems = []
    for path, src in files.items():
        if not path.endswith(".py"):
            continue
        try:
            compile(src, path, "exec")
        except SyntaxError as e:
            problems.append(f"{path} 无法编译：第 {e.lineno} 行 {e.msg}")
            continue
        for name, lineno in _widget_refs(src):
            if name not in widgets:
                problems.append(f"{path} 第 {lineno} 行引用了不存在的部件「{name}」")
    # 每个部件必须至少有一种识别信号（否则运行时无从下手）
    for name, entry in widgets.items():
        if not any(k in entry for k in ("uia", "text", "images", "coord")):
            problems.append(f"部件「{name}」没有任何识别信号（UI 树/文字/图像/坐标全空）")
    return problems


# --------------------------------------------------------------------- 总入口

def export_plan(sg: dict, opts=None) -> dict:
    """脚本 → 导出计划（不落盘）。返回 {files, widgets, params, notes}。

    这是导出器对外的唯一入口：目录适配（WP2）、界面向导（WP3）、AI 转译（WP4）
    都基于它的产物工作。
    """
    opts = opts or {}
    problems = schema.validate(sg)
    if problems:
        return {"ok": False, "files": {}, "widgets": {}, "params": {},
                "problems": [f"脚本本身不合法：{p}" for p in problems[:5]]}
    widgets = collect_widgets(sg, names=opts.get("widget_names"))
    params = collect_params(sg, names=opts.get("param_names"))
    engine_entry = opts.get("engine_entry") or "engine/ipc.py"
    # 目标 Agent 已有的同名工具 → 求差集（§8.6）：能复用的不重复写、不覆盖
    reuse = {str(t) for t in (opts.get("existing_tools") or [])} if \
        opts.get("reuse_existing", True) else set()
    reuse &= set(_TOOL_SRC)
    files = render_tools(widgets, sg, engine_entry,
                         opts={"param_spec": {k: {kk: vv for kk, vv in v.items()
                                                  if kk in ("step", "field")}
                                              for k, v in params.items()},
                               "skip_tools": sorted(reuse)})
    files[f"skills/{slug(sg.get('name'), 'script')}.py"] = render_skill(
        sg, widgets, params, engine_entry, opts)
    files["skills/__init__.py"] = ""
    files["tools/__init__.py"] = ""
    issues = verify_product(files, sg, widgets, params)
    notes = [f"部件 {len(widgets)} 个", f"可参数化点 {len(params)} 个",
             "识别与输入在本机引擎执行（导出物不是自包含代码）"]
    if reuse:
        notes.append("复用了目标 Agent 已有的工具：" + "、".join(sorted(reuse)))
    return {"ok": not issues, "files": files, "widgets": widgets, "params": params,
            "problems": issues, "reused_tools": sorted(reuse),
            "generated_tools": sorted(set(_TOOL_SRC) - reuse), "notes": notes}


def export_to_dir(sg: dict, out_dir, opts=None) -> dict:
    """直接落盘到某个目录（通用格式；Agent 目录适配见 engine/agent_dir.py）。"""
    from pathlib import Path
    res = export_plan(sg, opts)
    if not res["files"]:
        return res
    root = Path(out_dir)
    written = []
    for rel, src in res["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        written.append(str(p))
    res["written"] = written
    return res
