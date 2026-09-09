# -*- coding: utf-8 -*-
"""M0③ 收尾：AI 文本候选选择（本地 OCR 给候选列表 → AI 纯文本选出目标部件）
若高准 → 定位架构 = 本地 OCR/模板坐标 + AI 语义确认，无需像素级 VLM grounding。
"""
import json
import os
import sys
import time

import m0lib
from m0_ai_bench import chat, log

SPECS = [
    ("login", "登录", "登录按钮"),
    ("erp", "库存查询", "ERP 左侧菜单"),
    ("dyn", "服务状态", "监控卡片标题"),
    ("tk", "M0桌面验证标记行", "任务表内容行"),
    ("bili_set", "常规设置", "bilibili 设置页侧栏"),
    ("cpl", "控制面板", "控制面板窗口标题栏"),
    ("calc", "8", "计算器数字键"),
]


def main():
    m0lib.setup_utf8_stdio()
    print("=== AI 文本候选选择（页面 OCR 列表 → AI 选出目标） ===")
    rows = []
    for tid, needle, desc in SPECS:
        t = m0lib.Target.load("smoke/targets/%s.json" % tid)
        page = m0lib.load_png("smoke/data/target_img/%s" % t.page["file"])
        r = m0lib.ocr_run(page)
        # 候选：去重 + 去空，最多 25 条
        cands, seen = [], set()
        for txt in r["txts"]:
            x = "".join(txt.split())
            if x and x not in seen:
                seen.add(x)
                cands.append(x)
        cands = cands[:25]
        listed = "\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(cands))
        prompt = ("软件界面 OCR 识别出的文字候选中，哪一个是【%s】（%s）？"
                  "只回答序号数字。候选列表：\n%s") % (needle, desc, listed)
        res = chat(prompt)
        got = (res.get("text") or "").strip() if res["ok"] else ""
        digits = "".join(ch for ch in got if ch.isdigit())
        pick = int(digits[:3]) - 1 if digits else -1
        truth = None
        for i, c in enumerate(cands):
            if m0lib.text_similar(c, needle) >= 0.75 or needle in c:
                truth = i
                break
        okk = truth is not None and pick == truth
        rows.append({"type": "ai_text_select", "target": tid, "ok": okk,
                     "pick": pick + 1 if pick >= 0 else None, "truth": (truth + 1) if truth is not None else None,
                     "reply": got[:40], "n_cands": len(cands),
                     "elapsed_ms": res.get("elapsed_ms")})
        log(rows[-1])
        print("%-9s 候选%d条 应选#%s AI选#%s %s (%sms) reply=%s" %
              (tid, len(cands), (truth + 1) if truth is not None else "-",
               (pick + 1) if pick >= 0 else "-", "OK" if okk else "X",
               res.get("elapsed_ms"), got[:24].replace("\n", " ")))
        time.sleep(0.2)
    n = sum(1 for x in rows if x["ok"])
    print("文本候选选择: %d/%d = %.0f%%" % (n, len(rows), 100 * n / len(rows)))


if __name__ == "__main__":
    main()
