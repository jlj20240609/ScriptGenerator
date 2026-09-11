# -*- coding: utf-8 -*-
"""
engine.ai_generate — 一句话生成脚本（M4-WP5，分析文档 §7.4 #3）。

用户说一句"帮我做一个自动登录"，就得到一份**可编辑的步骤骨架**：每一步做什么、
要找的部件大概叫什么、做完后应该看到什么。

这里最要紧的是**边界**（M0 起就定死的）：云端**不许产出坐标**。所以生成出来的
每一步只带"部件文字/语义"，不带任何坐标或图片；坐标要等用户框一次操作页面之后，
由本机引擎在**用户自己的页面截图**上按文字找出来（见 engine/autofill.py）。
这条链是："云端出意图 → 本地出坐标"，与整体架构一致。

与出口 ② 共用同一条 AI 转译管线（§8.9 明确要求），只是方向相反：
  §8.9② 是"脚本 → 源码"，本模块是"一句话 → 步骤"。
"""
from __future__ import annotations

import json
import re
import time

from engine import schema

GEN_PROMPT = """用户想做一个电脑上的自动化操作，他说：

「{sentence}」

请把它拆成**可执行的步骤**，只输出 JSON（不要解释、不要代码块之外的任何文字）：
{{
  "page": "大概是什么界面（例如 登录页 / 订单列表）",
  "steps": [
    {{"action": "type",  "target": "用户名", "text": "demo_user"}},
    {{"action": "type",  "target": "密码",   "text": "demo_pass"}},
    {{"action": "click", "target": "登录"}},
    {{"action": "expect","target": "登录成功"}},
    {{"action": "wait",  "seconds": 2}},
    {{"action": "hotkey","keys": "ctrl+s"}},
    {{"action": "notify","message": "请检查一下结果"}}
  ],
  "notes": "有哪一步你不确定，用一句话说清楚"
}}

规则：
- action 只能是：click / dblclick / type / wait / notify / hotkey / expect
  （expect 表示"上一步做完后应该看到什么"）；
- **target 只写界面上那个部件的文字**，例如"登录""用户名"；
- **绝对不要给坐标、不要给像素位置、不要编造窗口内容**——找位置是本机的事；
- 写死的值（账号、密码、等待秒数）直接给出，作为默认值；
- 不确定的地方写进 notes，不要猜。
"""

ALLOWED = ("click", "dblclick", "type", "wait", "notify", "hotkey", "expect")


def extract_json(reply: str) -> dict:
    """从模型回复里抠出 JSON（容忍 ```json 包裹与前后夹话）。"""
    raw = str(reply or "").strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)```", raw, re.S)
    text = m.group(1) if m else raw
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"ok": False, "reason": "no_json", "raw": raw[:160]}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as e:
        return {"ok": False, "reason": f"bad_json:{e}", "raw": text[start:start + 160]}
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return {"ok": False, "reason": "shape", "raw": text[start:start + 160]}
    return {"ok": True, "data": data}


def _target_of(text: str) -> dict:
    """只带"部件文字"的目标：**不带页面、不带图、不带坐标**。

    为什么连页面都不带：schema 要求 page 里必须有页面截图，而这一步还没有截图；
    先只留文字信号，等用户框一次页面后由 autofill 补上图片/坐标/页面。
    """
    return {"text": str(text or "").strip()}


def steps_from_ai(data: dict, name: str) -> dict:
    """AI 给的 JSON → schema 合法的步骤骨架（+ 一份"待补齐"清单）。"""
    steps, pending = [], []
    seq = 0

    def sid():
        nonlocal seq
        seq += 1
        return f"g{seq}"

    for raw in data.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        act = str(raw.get("action") or "").strip().lower()
        if act not in ALLOWED:
            continue
        if act == "wait":
            secs = raw.get("seconds", 1)
            try:
                secs = float(secs)
            except (TypeError, ValueError):
                secs = 1.0
            steps.append({"id": sid(), "type": "action", "action": "wait",
                          "params": {"seconds": secs if secs > 0 else 1.0}})
            continue
        if act == "notify":
            steps.append({"id": sid(), "type": "action", "action": "notify",
                          "params": {"message": str(raw.get("message") or "请检查一下")}})
            continue
        if act == "hotkey":
            keys = str(raw.get("keys") or "").strip()
            if not keys:
                continue
            steps.append({"id": sid(), "type": "action", "action": "hotkey",
                          "params": {"keys": keys}})
            continue
        tw = str(raw.get("target") or "").strip()
        if not tw:
            continue
        if act == "expect":
            # "做完后应该看到 X" → 挂到上一个动作上（这正是本产品的说法）
            if not steps:
                continue
            steps[-1]["expected_outcome"] = {
                "target": _target_of(tw),
                "on_fail": {"strategy": "notify",
                            "message": f"做完后没看到「{tw}」"}}
            pending.append(tw)
            continue
        params = {}
        if act == "type":
            params["text"] = str(raw.get("text") or "")
            if not params["text"]:
                continue
        st = {"id": sid(), "type": "action", "action": act,
              "target": _target_of(tw), "params": params}
        steps.append(st)
        pending.append(tw)

    for st in steps:
        for t in _walk_targets(st):
            if t.get("text") and t["text"] not in pending:
                pending.append(t["text"])
    sg = schema.new_script(name) | {"targets_rev": 0, "steps": steps}
    return {"script": sg, "pending": pending}


def _walk_targets(st: dict):
    for key in ("target",):
        if st.get(key):
            yield st[key]
    eo = st.get("expected_outcome") or {}
    if eo.get("target"):
        yield eo["target"]


def generate(sentence: str, vlm, opts=None) -> dict:
    """一句话 → 步骤骨架。vlm 需有 enabled + ask（测试可注入假模型）。"""
    opts = dict(opts or {})
    sentence = str(sentence or "").strip()
    if not sentence:
        return {"ok": False, "script": None, "notes": ["先说一句你想让电脑做什么"]}
    if vlm is None or not getattr(vlm, "enabled", False):
        return {"ok": False, "script": None,
                "notes": ["一句话生成需要云端帮忙理解你的意思，现在还没有接通或授权；"
                          "你也可以直接点「截图目标」手动搭（一样的）"]}
    try:
        r = vlm.ask(GEN_PROMPT.format(sentence=sentence), [])
    except Exception as e:
        return {"ok": False, "script": None, "notes": [f"云端调用失败：{e!r}"]}
    if not r.get("ok"):
        return {"ok": False, "script": None,
                "notes": [f"云端调用失败：{r.get('error')}"], "usage": r.get("usage") or {}}
    got = extract_json(r.get("text"))
    if not got["ok"]:
        return {"ok": False, "script": None,
                "notes": [f"没能从回复里读出步骤（{got['reason']}）"],
                "raw": got.get("raw", ""), "usage": r.get("usage") or {}}
    made = steps_from_ai(got["data"], opts.get("name") or "新脚本")
    if not made["script"]["steps"]:
        return {"ok": False, "script": None,
                "notes": ["没生成出可用的步骤，换个说法再试试"],
                "usage": r.get("usage") or {}}
    problems = schema.validate(made["script"])
    if problems:
        return {"ok": False, "script": None, "problems": problems,
                "notes": ["生成的步骤不合规范（已丢弃，避免给你一份跑不了的脚本）"]}
    notes = ["已经按你的话搭好步骤，**还差一步**：点「截图目标」框住要操作的页面，"
             "我就能按上面那些文字把位置找出来（位置永远由本机算，云端不给坐标）"]
    ai_notes = str(got["data"].get("notes") or "").strip()
    if ai_notes:
        notes.append("它自己说不确定的地方：" + ai_notes)
    if got["data"].get("page"):
        notes.append("它猜的界面是：" + str(got["data"]["page"]))
    # 边界自检：生成结果里**不该出现任何坐标**
    blob = json.dumps(made["script"], ensure_ascii=False)
    for bad in ("rect_in_page", "center_in_page", "\"coord\"", "\"image\""):
        if bad in blob:
            return {"ok": False, "script": None,
                    "notes": [f"生成的步骤里出现了 {bad}（云端不该给坐标），已丢弃"]}
    return {"ok": True, "script": made["script"], "pending": made["pending"],
            "usage": r.get("usage") or {}, "notes": notes,
            "elapsed_ms": round((time.time() - opts.get("_t0", time.time())) * 1000, 1)}
