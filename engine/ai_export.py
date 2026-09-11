# -*- coding: utf-8 -*-
"""
engine.ai_export — AI 代码脚本转译（M4-WP4，双导出出口 ②，分析文档 §8.9）。

出口 ① 是"图片脚本"（调用编排 + 部件数据，靠本机引擎识别）；出口 ② 是**代码脚本**：
云端多模态读**全部页面/部件截图** + 步骤序列，产出**带注释的可读源码**，
让开发者能直接看、能改、能脱离本产品（§8.9）。

三条规则写死在实现里（§8.9「规则与边界」）：
  1. **上传截图前必须显式授权**（复用 M3 的 AiGate）——云端会看到屏幕内容；
  2. **单向导出**：产物头部标注"由 vX 脚本生成，修改脚本后请重新导出"，不做反向同步；
  3. **引用完整性在导出前拦截**：AI 引用了不存在的部件 → 直接不给出产物，而不是等运行时崩。

另外两条边界：
  - AI 只做**转译**（把已有的步骤写成源码），**不产出坐标**——坐标仍由本机引擎在运行时求；
  - 目标应用没有可编程元素（UIA）时，产物只能退化成"坐标/视觉语义"代码，
    这时明确提示用户优先用出口 ①（§8.9 能力边界）。

为什么要支持注入假模型：转译的正确性（产物形状、引用校验、单向标注、降级）都是**确定性逻辑**，
不该被网络和模型波动挡住——离线测掉，真机只测"模型给的代码好不好看"。
"""
from __future__ import annotations

import re
import time

from engine import exporter as E
from engine import matcher

# 允许 AI 调用的原语：与出口 ① 的 tools 完全同名——这样同一套引用完整性校验能直接复用，
# 也让两份产物可以混着看（§8.9：② 不替代 ①，WIDGETS 始终保留、可随时回退）。
CODE_PRIMITIVES = ("find_target", "click", "dblclick", "type_text", "wait",
                   "notify", "verify_result", "hotkey")

CODE_PROMPT = """你在把一个已经搭好的桌面自动化脚本，翻译成**可读的 Python 源码模块**。

用户搭好的步骤（按顺序）：
{steps}

每一步用到的界面部件（名字 → 信息）：
{widgets}

请输出一个 Python 模块，要求：
1. 用这些**已经存在的原语**函数，名字必须原样使用：{prims}
   例如 `click("登录")`、`type_text("用户名", "admin")`；
   **部件名必须严格来自上面的清单**，不要自己编名字；
2. 每一步都写一行中文注释说明它在做什么；
3. 提供一个 `run(...)` 函数，把脚本里写死的值做成函数参数（而不是散落在函数体里）；
4. 模块开头写清：这个文件是生成出来的、改脚本后要重新导出；
5. 只输出源码本身，用 ```python 包起来，不要解释。

如果某一步依赖的东西说不清（例如界面上看不出可编程元素），就在注释里如实写明
"这里依赖坐标/视觉，建议改用图片脚本导出"，不要编造。
"""


def _steps_text(sg: dict) -> str:
    lines = []
    for i, st in enumerate(E.walk_steps(sg.get("steps")), 1):
        t = st.get("type")
        if t == "action":
            tw = ((st.get("target") or {}).get("text") or "").strip()
            p = st.get("params") or {}
            extra = ""
            if st.get("action") == "type":
                extra = f'（输入 {p.get("text")!r}）'
            elif st.get("action") == "wait":
                extra = f'（{p.get("seconds")} 秒）'
            eo = st.get("expected_outcome") or {}
            tail = ""
            if eo.get("target"):
                tail = f'；做完后应该看到「{(eo["target"].get("text") or "").strip()}」'
            lines.append(f"{i}. {st.get('action')}「{tw}」{extra}{tail}")
        elif t == "condition":
            tw = ((st.get("condition") or {}).get("target") or {}).get("text", "")
            lines.append(f"{i}. 如果看到「{tw}」就做 {len(st.get('then') or [])} 步，"
                         f"否则做 {len(st.get('else') or [])} 步")
        elif t == "loop":
            lp = st.get("loop") or {}
            lines.append(f"{i}. 重复（{lp.get('mode')}）{len(st.get('body') or [])} 步")
    return "\n".join(lines) or "（没有步骤）"


def _widgets_text(widgets: dict) -> str:
    lines = []
    for name, w in widgets.items():
        bits = [f"文字 {w.get('text')!r}"] if w.get("text") else []
        if w.get("uia"):
            bits.append("有 UI 元素信息")
        if w.get("coord"):
            c = w["coord"]
            bits.append(f"页面内坐标 ({c['x']},{c['y']},{c['w']},{c['h']})")
        if w.get("images"):
            bits.append(f"{len(w['images'])} 张截图")
        lines.append(f"- 「{name}」：{'；'.join(bits)}")
    return "\n".join(lines) or "（没有部件）"


def _images_of(sg: dict, widgets: dict, max_widgets=6):
    """要发给云端看的图：页面图 + 若干部件图（§8.9：读全部页面/部件截图）。"""
    out = []
    seen = set()
    for w in widgets.values():
        for u in w.get("images") or []:
            if u in seen:
                continue
            seen.add(u)
            try:
                out.append(matcher.dataurl_to_bgr(u))
            except Exception:
                continue
            if len(out) >= max_widgets:
                break
        if len(out) >= max_widgets:
            break
    # 页面图放最后（模型通常按顺序看，页面给全局观感）
    for st in E.walk_steps(sg.get("steps")):
        page = ((st.get("target") or {}).get("page")
                or ((st.get("condition") or {}).get("target") or {}).get("page") or {})
        u = (page or {}).get("image")
        if u and u not in seen:
            seen.add(u)
            try:
                out.append(matcher.dataurl_to_bgr(u))
            except Exception:
                pass
            break
    return out


def extract_code(reply: str) -> dict:
    """从模型回复里抠出源码（```python 包着，或整段就是代码）。"""
    raw = str(reply or "")
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
    code = (m.group(1) if m else raw).strip()
    if not code:
        return {"ok": False, "reason": "empty"}
    if "def " not in code:
        return {"ok": False, "reason": "no_function", "raw": raw[:120]}
    return {"ok": True, "code": code}


def _one_way_header(sg: dict, stamp: str) -> str:
    return (f"# -*- coding: utf-8 -*-\n"
            f'"""由「脚本构建器」把脚本转译成源码（双导出出口 ②）——**单向导出**。\n\n'
            f'来源脚本：{sg.get("name") or "未命名脚本"}（targets_rev={sg.get("targets_rev", 0)}）\n'
            f"转译时间：{stamp}　模型：{{model}}\n\n"
            f"**修改脚本后请重新导出**：这份源码不会反向同步回脚本。\n\n"
            f"⚠️ 坐标仍由本机引擎在运行时求（本地三级定位）；这份代码负责的是**编排**，\n"
            f'不是"算好的坐标表"。\n"""\n')


def translate(sg: dict, vlm, opts=None) -> dict:
    """把脚本转译成源码模块。

    vlm: 需有 enabled 属性与 ask(prompt, images) 方法（真机是 ZhipuVLM；测试里可注入假模型）
    返回 {ok, files, problems, notes, usage, raw}
    """
    opts = dict(opts or {})
    started = time.time()
    plan = E.export_plan(sg)
    if not plan.get("ok"):
        return {"ok": False, "files": {}, "problems": plan.get("problems", []),
                "notes": ["脚本本身不合法，先修好再转译"]}
    widgets, params = plan["widgets"], plan["params"]
    if vlm is None or not getattr(vlm, "enabled", False):
        return {"ok": False, "files": {},
                "problems": [],
                "notes": ["云端没有接通或没有授权：AI 代码转译需要上传页面/部件截图，"
                          "请先在弹窗里同意；也可以改用「图片脚本导出」（出口 ①）"]}
    prompt = CODE_PROMPT.format(steps=_steps_text(sg), widgets=_widgets_text(widgets),
                                prims="、".join(CODE_PRIMITIVES))
    images = _images_of(sg, widgets, max_widgets=int(opts.get("max_images", 6)))
    try:
        r = vlm.ask(prompt, images)
    except Exception as e:
        return {"ok": False, "files": {}, "problems": [],
                "notes": [f"云端调用失败：{e!r}"]}
    if not r.get("ok"):
        return {"ok": False, "files": {}, "problems": [],
                "notes": [f"云端调用失败：{r.get('error')}"],
                "usage": r.get("usage") or {}}
    got = extract_code(r.get("text"))
    if not got["ok"]:
        return {"ok": False, "files": {}, "problems": [],
                "notes": [f"模型没有给出可用的源码（{got['reason']}）"],
                "raw": got.get("raw", ""), "usage": r.get("usage") or {}}
    name = E.slug(sg.get("name"), "script")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    header = _one_way_header(sg, stamp).replace("{model}", str(getattr(vlm, "model", "?")))
    files = {f"scripts/{name}.py": header + "\n" + got["code"] + "\n"}
    # 引用完整性校验：AI 编造的部件名必须在这里被拦下（DoD 要求）
    problems = E.verify_product(files, sg, widgets, params)
    hard = [p for p in problems if "不存在的部件" in p or "无法编译" in p]
    if hard:
        return {"ok": False, "files": {}, "problems": problems,
                "notes": ["转译产物引用了脚本里没有的部件（或语法有问题），已拦下——"
                          "请重新转译，或改用图片脚本导出（出口 ①）"],
                "usage": r.get("usage") or {}, "raw": got["code"][:400]}
    return {"ok": True, "files": files, "widgets": widgets, "params": params,
            "problems": problems, "usage": r.get("usage") or {},
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "notes": [f"读了 {len(images)} 张图（页面/部件截图）",
                      "产物是给开发者看的源码：可读、可改、可脱离本产品",
                      "坐标仍由本机引擎在运行时求（本地三级定位）"]}
