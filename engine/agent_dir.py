# -*- coding: utf-8 -*-
"""
engine.agent_dir — 目标 Agent 目录适配与自动写入（M4-WP2，分析文档 §8.6）。

导出不是"生成文件让用户自己拷"：用户给出他的 Agent 项目目录 → 系统**只读扫描**
识别它的约定 → 按约定拼装 → 干跑预览 → 用户确认后**自动写入**对应文件夹。

四步（照 §8.6 的顺序）：
  1. 目录接入与自动检测（只读）：目录结构 / 工具定义方式 / 加载入口 / **已有工具清单**
  2. 按 Agent 拼装：原子 tools 与已有工具**求差集**（同名则复用、不重复写、不覆盖）；
     skill 引用的工具名与它已注册的名字对齐
  3. 自动写入：默认先干跑预览（列出将新建/覆盖/跳过的文件 + 差异）→ 冲突检测
     （覆盖/跳过/改名）→ 落盘 → 提示刷新
  4. 边界：检测与写入**都限定在用户指定的目录内**；识别不出框架时退回通用目录导出

三条安全默认（都是刻意的）：
  - **干跑优先**：plan() 只算不写，落盘必须显式 apply()；
  - **冲突默认跳过**：默认不覆盖别人已有的文件；要覆盖得用户在计划里逐个放行；
  - **绝不越界**：所有写入路径都先做"是否在 agent_dir 之内"的检查。
"""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

from engine import exporter as E

SKILLS_DIRS = ("skills", "src/skills", "agent/skills")
TOOLS_DIRS = ("tools", "src/tools", "agent/tools")
ENTRY_HINTS = ("registry.py", "package.json", "agent.json", "config.json",
               "config.yaml", "config.yml", "manifest.json", "pyproject.toml")


# --------------------------------------------------------------------- 1) 扫描（只读）

def _py_defs(path: Path) -> list:
    """一个 .py 文件里定义的工具函数名（顶层 def，含被装饰的）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node.name)
    return out


def _json_names(path: Path) -> list:
    """JSON 里声明的工具名（几种常见写法都认）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out = []
    if isinstance(data, dict):
        for key in ("tools", "functions", "schemas"):
            arr = data.get(key)
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        name = (it.get("name")
                                or (it.get("function") or {}).get("name"))
                        if name:
                            out.append(str(name))
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                name = it.get("name") or (it.get("function") or {}).get("name")
                if name:
                    out.append(str(name))
    return out


def scan(agent_dir) -> dict:
    """只读扫描目标目录，识别框架约定与**已有工具清单**（§8.6 第 1 步）。

    识别不出框架不算失败：退回通用目录约定，并在 notes 里说清楚，让用户自己放。
    """
    root = Path(agent_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return {"ok": False, "note": f"这个目录不存在或不是目录：{root}",
                "tools": [], "skills": [], "entries": [], "notes": []}
    skills_dir = next((d for d in SKILLS_DIRS if (root / d).is_dir()), "skills")
    tools_dir = next((d for d in TOOLS_DIRS if (root / d).is_dir()), "tools")
    entries = [e for e in ENTRY_HINTS if (root / e).exists()
               or (root / tools_dir / e).exists()]
    tools, skills = [], []
    td = root / tools_dir
    if td.is_dir():
        for f in sorted(td.glob("*.py")):
            tools.extend(_py_defs(f))
        for f in sorted(list(td.glob("*.json")) + list(root.glob("*.json"))):
            tools.extend(_json_names(f))
    for d in (root / skills_dir, root / "scripts"):
        if d.is_dir():
            skills.extend(f.stem for f in sorted(d.glob("*.py")) if f.stem != "__init__")
    known = bool(entries) or bool(tools) or bool(skills)
    notes = []
    if not known:
        notes.append("没识别出这个 Agent 的框架约定，将按通用目录导出"
                     "（skills/ 与 tools/），可能需要你手动放置")
    if entries:
        notes.append("加载入口线索：" + "、".join(entries))
    return {"ok": True, "agent_dir": str(root), "skills_dir": skills_dir,
            "tools_dir": tools_dir, "entries": entries,
            "tools": sorted(set(tools)), "skills": sorted(set(skills)),
            "framework": "已识别" if known else "通用", "notes": notes}


# --------------------------------------------------------------------- 2) 计划（不落盘）

def plan(sg: dict, agent_dir, opts=None) -> dict:
    """产出写入计划：新增 / 覆盖 / 相同 / 跳过 + 复用的工具（§8.6 第 2、3 步）。

    默认**不覆盖**任何已存在且内容不同的文件（conflict 默认 "skip"），
    要覆盖得把该项的 action 改成 "overwrite" 后再 apply。
    """
    opts = dict(opts or {})
    sc = scan(agent_dir)
    if not sc["ok"]:
        return {"ok": False, "note": sc["note"], "files": [], "scan": sc}
    root = Path(sc["agent_dir"])
    strategy = str(opts.pop("conflict", "skip"))     # skip | overwrite | rename
    # 目标 Agent 已有的工具 → 交给导出器求差集（能复用就不重复写）
    res = E.export_plan(sg, {**opts, "existing_tools": sc["tools"]})
    if not res["files"]:
        return {"ok": False, "note": "导出器没有产出任何文件",
                "problems": res.get("problems", []), "scan": sc}
    files = []
    for rel, src in sorted(res["files"].items()):
        # 目录名按检测结果落位（识别不出就用通用的 skills/ tools/）
        if rel.startswith("skills/"):
            target_rel = f"{sc['skills_dir']}/{rel[len('skills/'):]}"
        elif rel.startswith("tools/"):
            target_rel = f"{sc['tools_dir']}/{rel[len('tools/'):]}"
        else:
            target_rel = rel
        abs_path = root / target_rel
        action, reason = "create", "新文件"
        old = None
        if abs_path.exists():
            old = abs_path.read_text(encoding="utf-8", errors="replace")
            if old == src:
                action, reason = "identical", "内容完全相同，无需写入"
            elif strategy == "overwrite":
                action, reason = "overwrite", "同名文件内容不同（按你的选择覆盖）"
            elif strategy == "rename":
                p = abs_path
                i = 2
                while p.exists():
                    p = abs_path.with_name(f"{abs_path.stem}_sg{i}{abs_path.suffix}")
                    i += 1
                abs_path = p
                target_rel = str(p.relative_to(root)).replace("\\", "/")
                action, reason = "create", "同名冲突，已改名"
            else:
                action, reason = "skip", "同名文件已存在，默认不覆盖"
        files.append({"rel": target_rel, "abs": str(abs_path), "action": action,
                      "reason": reason, "size": len(src), "_src": src,
                      "differs": bool(old is not None and old != src)})
    write_n = sum(1 for f in files if f["action"] in ("create", "overwrite"))
    notes = list(sc["notes"]) + list(res.get("notes", []))
    # 注册表被跳过是最容易"看起来成功、其实没接上"的情况：明确说清楚怎么办
    for f in files:
        if f["action"] == "skip" and Path(f["rel"]).name == "registry.py":
            notes.append(f"{f['rel']} 已存在且内容不同，默认没有覆盖——"
                         "如果它需要注册本产品导出的工具，请选择覆盖，"
                         "或按它的格式手工加一行")
    return {"ok": True, "scan": sc, "files": files,
            "write_count": write_n,
            "skip_count": sum(1 for f in files if f["action"] == "skip"),
            "same_count": sum(1 for f in files if f["action"] == "identical"),
            "reused_tools": res.get("reused_tools", []),
            "generated_tools": res.get("generated_tools", []),
            "problems": res.get("problems", []),
            "notes": notes,
            "dry_run": True, "conflict": strategy}


# --------------------------------------------------------------------- 3) 写入

def render_diff(plan_res: dict, limit: int = 12) -> str:
    """把计划渲染成人看的一段白话（界面/命令行都用它）。"""
    if not plan_res.get("ok"):
        return "导出计划不可用：" + str(plan_res.get("note") or "")
    lines = [f"目标目录：{plan_res['scan']['agent_dir']}"
             f"（框架：{plan_res['scan']['framework']}）"]
    for f in plan_res["files"]:
        word = {"create": "新建", "overwrite": "覆盖", "skip": "跳过",
                "identical": "已是最新"}[f["action"]]
        lines.append(f"  [{word}] {f['rel']}（{f['size']} 字节）—— {f['reason']}")
        if len(lines) > limit:
            lines.append("  …")
            break
    if plan_res["reused_tools"]:
        lines.append("复用目标 Agent 已有的工具（不重复写）："
                     + "、".join(plan_res["reused_tools"]))
    for n in plan_res["notes"]:
        lines.append("· " + n)
    return "\n".join(lines)


def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def apply(plan_res: dict, backup: bool = True) -> dict:
    """执行写入。**只写 agent_dir 之内的路径**，覆盖前先备份（.bak）。"""
    if not plan_res.get("ok"):
        return {"ok": False, "note": plan_res.get("note") or "计划不可用", "written": []}
    root = Path(plan_res["scan"]["agent_dir"])
    written, skipped, backed = [], [], []
    for f in plan_res["files"]:
        if f["action"] not in ("create", "overwrite"):
            skipped.append(f["rel"])
            continue
        p = Path(f["abs"])
        if not _inside(root, p):
            return {"ok": False, "note": f"拒绝写到目标目录之外：{p}",
                    "written": written, "skipped": skipped}
        p.parent.mkdir(parents=True, exist_ok=True)
        if f["action"] == "overwrite" and backup and p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            shutil.copy2(p, bak)
            backed.append(bak.name)
        p.write_text(f["_src"], encoding="utf-8")
        written.append(f["rel"])
    return {"ok": True, "written": written, "skipped": skipped, "backed_up": backed,
            "note": "写好了：请让 Agent 刷新或重启一次，它才会加载到新工具/新技能"
                    if written else "没有需要写入的文件"}


def export(sg: dict, agent_dir, opts=None) -> dict:
    """一步到位：计划 → （可选）直接写入。opts.dry_run=False 时才落盘。"""
    opts = dict(opts or {})
    dry = bool(opts.pop("dry_run", True))
    res = plan(sg, agent_dir, opts)
    res["dry_run"] = dry
    if not dry and res.get("ok"):
        res["apply"] = apply(res)
    return res
