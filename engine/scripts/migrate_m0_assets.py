# -*- coding: utf-8 -*-
"""
M1 波次2：M0 目标资产迁移 → .sgscript.json v1.0 常驻用例（离线，无需重开窗口）。

输入：smoke/targets/{name}.json（schema 1，M0 录制）+ smoke/data/target_img/*.png
输出：engine/tests/assets/live/{name}.sgscript.json（v0.31 §11 全量字段内嵌 base64）

口径（与 M0 采集一致）：页面 = 目标窗口整窗截图（1288×988 @125% DPI，
Edge --window-size=1020,780 的 app 窗口）；widget 页内矩形以窗口左上为原点。

生成脚本为“单击部件”演示步骤（action=click）；erp 资产同时用于 v2 改版校准演示
（对 erp-v2.html 运行 → click_guard 失败 → 自动校准写回）。

用法：python engine/scripts/migrate_m0_assets.py [--names login,erp,dyn]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import matcher, schema  # noqa: E402

TARGETS = {
    "login": ("web-login", "登录按钮（登录页）"),
    "erp": ("erp-web", "左侧菜单里的“库存查询”"),
    "dyn": ("dynamic", "监控卡片标题“服务状态”"),
}
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "assets" / "live"
M0_TARGETS = ROOT / "smoke" / "targets"
M0_IMG = ROOT / "smoke" / "data" / "target_img"


def build(name: str, cls: str, semantic: str):
    old = __import__("json").loads((M0_TARGETS / f"{name}.json").read_text(encoding="utf-8"))
    page_img = matcher.load_png(M0_IMG / old["page"]["file"])
    widget_img = matcher.load_png(M0_IMG / old["widget"]["file"])
    page_rect = old["page"]["rect"]
    ctx = {"process": "msedge.exe",
           "title": old["page"].get("window_title", ""),
           "class": old["page"].get("window_class", "Chrome_WidgetWin_1")}
    page = schema.page_spec(
        image_dataurl=matcher.bgr_to_dataurl(page_img),
        size=(page_img.shape[1], page_img.shape[0]),
        context=ctx, rect_in_screen=list(page_rect),
        capture_meta={"dpi": old.get("display", {}).get("dpi", 120),
                      "ts": old.get("captured_at", ""),
                      "cap_method": old["page"].get("cap_method", "screen"),
                      "visible": True})
    w = old["widget"]
    target = schema.widget_target(
        page=page,
        image_dataurl=matcher.bgr_to_dataurl(widget_img),
        text=old.get("ocr_text", "") or "",
        semantic=semantic,
        rect_in_page=w["rect_in_page"],
        center_in_page=w["center_in_page"],
        uia={"name": old.get("uia_name", "")} if old.get("uia_name") else None)
    sg = schema.new_script(name=f"M1 回归 · {name}（M0 资产迁移）")
    sg["steps"] = [{"id": f"{name}_1", "type": "action", "action": "click",
                    "target": target, "params": {}}]
    return sg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="login,erp,dyn")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for n in args.names.split(","):
        n = n.strip()
        if n not in TARGETS:
            print(f"跳过未知 {n}")
            continue
        sg = build(n, *TARGETS[n])
        p = Path(args.out) / f"{n}.sgscript.json"
        schema.dump(sg, p)
        problems = schema.validate(schema.load(p))
        print(f"{p.name}: 步骤={len(sg['steps'])} 校验={'OK' if not problems else problems}")
    print("输出目录:", OUT_DIR)


if __name__ == "__main__":
    main()
