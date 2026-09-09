# -*- coding: utf-8 -*-
"""M0③ 补充：区域方位定位（VLM 粗定位可行性）+ dyn 长超时重试"""
import json
import os
import sys
import time

import m0lib
from m0_ai_bench import chat, log

GRID = {  # 3x3 网格编号（列,行）
    "左上": (0, 0), "上中": (1, 0), "右上": (2, 0),
    "左中": (0, 1), "正中": (1, 1), "右中": (2, 1),
    "左下": (0, 2), "下中": (1, 2), "右下": (2, 2),
}
CELL_W, CELL_H = 3, 3


def truth_cell(wr, pw, ph):
    cx = (wr[0] + wr[2] / 2) / pw
    cy = (wr[1] + wr[3] / 2) / ph
    return (min(2, int(cx * CELL_W)), min(2, int(cy * CELL_H)))


def cell_name(col, row):
    for name, (c, r) in GRID.items():
        if (c, r) == (col, row):
            return name
    return "?"


def main():
    m0lib.setup_utf8_stdio()
    specs = [("login", "登录按钮里的“登录”文字"), ("erp", "左侧菜单“库存查询”"),
             ("dyn", "卡片标题“服务状态”"), ("tk", "任务表第一行里的“M0桌面验证标记行”"),
             ("bili_set", "左侧菜单“常规设置”")]
    print("=== 区域方位定位（3x3 网格：目标位于哪一格？） ===")
    rows = []
    for tid, desc in specs:
        import m0lib as m
        t = m.Target.load("smoke/targets/%s.json" % tid)
        page = m.load_png("smoke/data/target_img/%s" % t.page["file"])
        h, w = page.shape[:2]
        wr = t.widget["rect_in_page"]
        tc = truth_cell(wr, w, h)
        prompt = ("这是软件界面截图。请判断“%s”位于画面的哪个区域？"
                  "把画面想象为 3x3 网格，只回答一格："
                  "左上/上中/右上/左中/正中/右中/左下/下中/右下") % desc
        r = chat(prompt, page, max_w=768)  # 更小图加速
        got = (r.get("text") or "").strip() if r["ok"] else ""
        # 匹配回答中的方位词
        hit_name = None
        for name in GRID:
            if name in got:
                hit_name = name
                break
        truth_name = cell_name(*tc)
        okk = hit_name == truth_name
        rows.append({"type": "ai_region", "target": tid, "truth": truth_name,
                     "got": hit_name, "ok": okk, "reply": got[:60],
                     "elapsed_ms": r.get("elapsed_ms")})
        log(rows[-1])
        print("%-9s 真值=%-4s AI=%-4s %s (%sms) %s" %
              (tid, truth_name, hit_name or "?", "OK" if okk else "X",
               r.get("elapsed_ms"), got[:40].replace("\n", " ")))
        time.sleep(0.3)
    n = sum(1 for x in rows if x["ok"])
    print("区域方位: %d/%d = %.0f%%" % (n, len(rows), 100 * n / len(rows)))
    # dyn 长超时重试（原 120s 超时无响应）
    print("\n=== dyn grounding 重试（放宽超时） ===")
    from m0_ai_bench import chat as ch2
    t = m0lib.Target.load("smoke/targets/dyn.json")
    page = m0lib.load_png("smoke/data/target_img/%s" % t.page["file"])
    r = ch2("请找到图中卡片标题“服务状态”的中心像素坐标，只输出两个整数：x y", page)
    print("ok:", r["ok"], "耗时:", r.get("elapsed_ms"), "ms 回复:", r.get("text", "")[:80])
    log({"type": "ai_grounding_retry", "target": "dyn", "ok": r["ok"],
         "reply": r.get("text", "")[:80], "elapsed_ms": r.get("elapsed_ms"),
         "error": r.get("error")})


if __name__ == "__main__":
    main()
