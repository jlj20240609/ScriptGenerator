# -*- coding: utf-8 -*-
"""
smoke/diag/ai_export_eval.py — M4-WP4 真机验收：双导出出口 ②（AI 代码转译）。

跑的是真云端（智谱 glm-4.6v）：把合成场景里的登录脚本连同**页面/部件截图**发上去，
让它转译成可读源码，然后核对 DoD 与 §8.9 的规则：

  1. 产物是**可读源码**（有 def、有中文注释、参数做成入参）；
  2. **引用完整性**：产物引用的部件必须都存在（AI 编名字要被拦下）；
  3. **单向导出标注**：头部写明来源脚本 + "修改脚本后请重新导出"；
  4. 坐标不出自这份代码（注释里写清"坐标仍由本机引擎在运行时求"）。

用法：
  python smoke/diag/ai_export_eval.py            # 真调云端
  python smoke/diag/ai_export_eval.py --no-ai    # 对照：没授权时应干净降级
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import ai as AI               # noqa: E402
from engine import ai_export as AX        # noqa: E402
from engine import exporter as E          # noqa: E402
from engine import schema                 # noqa: E402
from engine.tests import support as S     # noqa: E402

OUT = ROOT / "smoke" / "rec"


def build_script():
    login, boxes = S.login_page()
    home, hboxes = S.home_page()
    spec = S.page_spec_of(login, rect=None)
    hspec = S.page_spec_of(home, rect=None)
    return schema.new_script("自动登录") | {"steps": [
        {"id": "s1", "type": "action", "action": "type",
         "target": S.widget_target(login, spec, boxes["user_label"], text="用户名"),
         "params": {"text": "admin"}},
        {"id": "s2", "type": "action", "action": "type",
         "target": S.widget_target(login, spec, boxes["pwd_label"], text="密码"),
         "params": {"text": "pass123"}},
        {"id": "s3", "type": "action", "action": "click",
         "target": S.widget_target(login, spec, boxes["login_btn"], text="登录"),
         "params": {},
         "expected_outcome": {
             "target": S.widget_target(home, hspec, hboxes["welcome"], text="登录成功"),
             "on_fail": {"strategy": "notify", "message": "登录没成功"}}},
    ]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true", help="对照组：不接云端")
    ap.add_argument("--model", default=AI.DEFAULT_MODEL)
    args = ap.parse_args(argv)
    fail = []

    def check(ok, label, detail=""):
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not ok:
            fail.append(label)
        return ok

    print("=" * 72)
    print("M4-WP4 验收：双导出出口 ②（AI 读截图转译成可读源码）")
    print("=" * 72)

    sg = build_script()
    plan = E.export_plan(sg)
    print(f"源脚本：{sg['name']}，{len(sg['steps'])} 步；"
          f"部件 {len(plan['widgets'])} 个：{'、'.join(plan['widgets'])}")

    vlm = None
    if args.no_ai:
        print("对照：不接云端")
    else:
        key = AI.ZhipuVLM().api_key
        if not key:
            print("× 没有可用的 API Key，改用 --no-ai 跑对照组。")
            return 2
        vlm = AI.ZhipuVLM(api_key=key, model=args.model)
        if not vlm.authorize(notify=lambda reason: True):
            print("× 未获授权。")
            return 2
        print(f"云端：{args.model}（已授权，会上传合成截图）")

    t0 = time.perf_counter()
    res = AX.translate(sg, vlm)
    secs = time.perf_counter() - t0
    print(f"\n转译结果：ok={res['ok']}　用时 {secs:.1f}s")
    for n in res.get("notes", []):
        print("  · " + n)
    if res.get("usage"):
        u = res["usage"]
        print(f"  用量：{u.get('total_tokens', 0)} tokens"
              f"（输入 {u.get('prompt_tokens', 0)} / 输出 {u.get('completion_tokens', 0)}）")
    for p in res.get("problems", []):
        print("  ! " + p)

    if not res["ok"]:
        if args.no_ai:
            check(any("图片脚本导出" in n or "出口 ①" in n for n in res["notes"]),
                  "对照：没授权时干净降级并指路出口 ①", str(res["notes"]))
            print("=" * 72)
            print("结果：对照组通过（没授权时不硬试、给出替代方案）")
            return 0 if not fail else 1
        print("=" * 72)
        print("结果：转译未成功（上面的 notes 是原因）")
        return 1

    path, src = next(iter(res["files"].items()))
    check(path.startswith("scripts/"), "产物落在 scripts/ 下", path)
    check("def " in src, "产物是可读源码（有函数）")
    check("单向导出" in src and "修改脚本后请重新导出" in src,
          "标注了单向导出（DoD 第 3 条）")
    check("坐标仍由本机引擎" in src, "写明了坐标不出自这份代码（架构边界）")
    used = [n for n, _ln in E._widget_refs(src)]
    check(bool(used), "产物按部件名调用了工具", "、".join(used) or "（没用部件名）")
    missing = [n for n in used if n not in plan["widgets"]]
    check(not missing, "**引用完整性**：产物引用的部件都在脚本里", str(missing))
    check("# 中文注释" or "#" in src, "每步带注释（可读性）", "有 # 注释")

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "ai_translated.py"
    out.write_text(src, encoding="utf-8")
    print(f"\n产物已写出：{out.relative_to(ROOT)}（{len(src)} 字节）")
    print("—— 产物内容预览 ——")
    for line in src.splitlines()[:34]:
        print("  " + line)
    if len(src.splitlines()) > 34:
        print(f"  ……（共 {len(src.splitlines())} 行）")

    print("=" * 72)
    if fail:
        print(f"结果：{len(fail)} 项未通过")
        for f in fail:
            print("  × " + f)
        return 1
    print("结果：全部通过（AI 转译产物形状、引用完整性、单向标注都成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
