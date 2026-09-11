# -*- coding: utf-8 -*-
"""
smoke/diag/export_e2e.py — M4-WP1 的 DoD 验收：**导出产物真的能加载并跑通**。

DoD 原文（过程文档 §3 M4）："导出产物在真实 Agent 目录加载运行通过"。
"加载"容易验（能 import）；"运行"才是关键——产物里的 `build_task(params)` 必须
产出一份**能被引擎跑通**的脚本。这个脚本就做这道闭环：

  脚本（合成登录场景）
    → 导出（engine/exporter.py）→ 落到一个真实的目录结构（skills/ + tools/）
    → 从那个目录里 import 产物 → build_task(用户名=…, 密码=…)
    → 把产出的脚本交给执行器**真跑一遍**（合成屏 + 替身驱动）
    → 核对：跑通了、且参数真的生效了（用的是传进去的账号，不是脚本里写死的）

全程离线、可复现，不需要人操作。
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import exporter as E          # noqa: E402
from engine import executor as X          # noqa: E402
from engine import schema                 # noqa: E402
from engine.logger import MemoryLogger    # noqa: E402
from engine.tests import support as S     # noqa: E402

PX, PY = 300, 150
CANVAS = (1400, 1000)
OLD_USER, OLD_PWD = "admin", "pass123"


def build_script():
    """搭一个贴近真实的登录脚本（用合成场景里的真实部件框）。"""
    login, boxes = S.login_page()
    home, hboxes = S.home_page()
    spec = S.page_spec_of(login, rect=None)
    hspec = S.page_spec_of(home, rect=None)
    login_btn = S.widget_target(login, spec, boxes["login_btn"], text="登录")
    user_box = S.widget_target(login, spec, boxes["user_label"], text="用户名")
    pwd_box = S.widget_target(login, spec, boxes["pwd_label"], text="密码")
    welcome = S.widget_target(home, hspec, hboxes["welcome"], text="登录成功")
    return schema.new_script("自动登录") | {"steps": [
        {"id": "s1", "type": "action", "action": "type", "target": user_box,
         "params": {"text": OLD_USER}},
        {"id": "s2", "type": "action", "action": "type", "target": pwd_box,
         "params": {"text": OLD_PWD}},
        {"id": "s3", "type": "action", "action": "click", "target": login_btn,
         "params": {},
         "expected_outcome": {"target": welcome,
                              "on_fail": {"strategy": "notify",
                                          "message": "登录没成功"}}},
    ]}


def make_env():
    """合成场景 + 替身驱动（与 test_executor 同一套搭法）。"""
    login, boxes = S.login_page()
    home, hboxes = S.home_page()
    scene = S.StatefulScene({"login": (login, boxes), "home": (home, hboxes)},
                            {"login": (PX, PY, 1.0), "home": (PX, PY, 1.0)},
                            canvas=CANVAS, initial="login")
    scene.add_zone("login_btn", boxes["login_btn"], "home")
    driver = S.FakeDriver(scene.provider, on_click=scene.on_click)
    cfg = X.RunConfig(l1_retries=1, l1_retry_interval_s=0.01,
                      l2_poll_interval_s=0.05, l2_timeout_s=2.0, guard=True)
    return scene, driver, cfg


def main() -> int:
    fail = []

    def check(ok, label, detail=""):
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not ok:
            fail.append(label)
        return ok

    print("=" * 70)
    print("M4-WP1 DoD 验收：导出产物加载 + 跑通（脚本 → 导出 → 产物 → build_task → 真跑）")
    print("=" * 70)

    sg = build_script()
    print(f"源脚本：{sg['name']}，{len(sg['steps'])} 步")

    # ---- 1) 导出到真实目录结构
    tmp = Path(tempfile.mkdtemp(prefix="agent_dir_"))
    res = E.export_plan(sg)
    if not check(res["ok"], "导出计划无问题", str(res["problems"][:2])):
        return 1
    for rel, src in res["files"].items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    print(f"  导出到：{tmp}")
    for rel in sorted(res["files"]):
        size = len(res["files"][rel])
        print(f"    · {rel}（{size} 字节）")

    skill_rel = next(p for p in res["files"] if p.startswith("skills/")
                     and p.endswith(".py") and not p.endswith("__init__.py"))
    check(bool(skill_rel), "产出了 skill 文件", skill_rel)
    check(any(p.startswith("tools/") for p in res["files"]), "产出了 tools 文件")

    # ---- 2) 从那个目录里 import 产物（"加载通过"）
    sys.path.insert(0, str(tmp))
    try:
        mod = importlib.import_module(skill_rel[:-3].replace("/", "."))
        screen = importlib.import_module("tools.screen")
    except Exception as e:
        check(False, "从导出目录里 import 产物", repr(e))
        return 1
    finally:
        sys.path.remove(str(tmp))
    check(True, "从导出目录里 import 产物（加载通过）")
    check(len(screen.WIDGETS) >= 4, "产物的 WIDGETS 里带着部件数据",
          f"{len(screen.WIDGETS)} 个：{list(screen.WIDGETS)}")
    check("verify_result" in mod.TOOLS and "find_target" in mod.TOOLS,
          "TOOLS 清单里有恒定的两个工具", str(mod.TOOLS))
    check("单向导出" in Path(tmp / skill_rel).read_text(encoding="utf-8"),
          "产物标注了单向导出（DoD 第 3 条）")

    # ---- 3) build_task(params) 参数真的生效
    task = mod.build_task(p_1="zhangsan", p_2="s3cret")
    sent = [st.get("params", {}).get("text") for st in task["steps"]
            if st.get("action") == "type"]
    check(sent == ["zhangsan", "s3cret"], "build_task 把参数填进了脚本", str(sent))
    check(OLD_USER not in json.dumps(task, ensure_ascii=False),
          "产出的脚本里不再有写死的旧账号")

    # ---- 4) 把产物产出的脚本交给执行器**真跑一遍**
    scene, driver, cfg = make_env()
    human = S.FakeHuman()
    logger = MemoryLogger()
    rep = X.run_script(task, driver, cfg=cfg, loc_logger=logger, human=human)
    print("  执行结果：")
    for row in rep.get("steps", []):
        print(f"    · [{row.get('status')}] {row.get('label') or row.get('step_id')}")
    check(rep["status"] == "ok", "导出产物产出的脚本**跑通了**", rep.get("error", ""))
    check(scene.state == "home", "界面真的转场了（点登录后到了首页）",
          f"state={scene.state}")
    typed = "".join(getattr(t, "text", "") for t in scene.typed) \
        if hasattr(scene, "typed") else ""
    check(not human.outcome_calls, "没有触发人工介入（结果校验一次通过）",
          str(human.outcome_calls))

    print("=" * 70)
    if fail:
        print(f"结果：{len(fail)} 项未通过")
        for f in fail:
            print("  × " + f)
        return 1
    print("结果：全部通过（导出 → 加载 → 参数化 → 跑通，闭环成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
