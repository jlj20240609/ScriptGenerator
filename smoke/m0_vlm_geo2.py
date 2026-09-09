# -*- coding: utf-8 -*-
"""point_norm 深化：重复 3 次稳定性 + dyn/cpl 描述修正
结论维度：命中率稳定性、偏差分布、修正描述能否解决歧义失败。
"""
import json
import os
import sys
import time

import numpy as np

import m0lib
from m0_vlm_geo import call, parse_ints, PROMPTS

SPECS = [
    ("login", "登录按钮里的“登录”文字"),
    ("erp", "左侧菜单“库存查询”"),
    ("dyn", "深色监控页第二张卡片里的标题“服务状态”"),
    ("tk", "任务表第一行开头“状态：”后的“M0桌面验证标记行”长文本"),
    ("bili_set", "设置页左侧竖排菜单里的“常规设置”"),
    ("cpl", "窗口最顶部标题栏左侧的“控制面板”标题文字（不是页面中间的内容）"),
    ("calc", "数字键盘上数字键“8”（在数字 7/9 之间，非显示区）"),
]

N_REPEAT = 3
OUT = m0lib.Path(__file__).parent / "data" / "vlm_geo2.jsonl"


def main():
    m0lib.setup_utf8_stdio()
    summary = {}
    for tid, desc in SPECS:
        t = m0lib.Target.load("smoke/targets/%s.json" % tid)
        page = m0lib.load_png("smoke/data/target_img/%s" % t.page["file"])
        h, w = page.shape[:2]
        wr = t.widget["rect_in_page"]
        cx, cy = wr[0] + wr[2] / 2, wr[1] + wr[3] / 2
        devs, inb = [], 0
        for rep in range(N_REPEAT):
            prompt = PROMPTS["point_norm"].format(desc=desc, w=w, h=h)
            r = call("glm-4.6v", prompt, page)
            nums = parse_ints(r.get("text", ""), 2) if r["ok"] else None
            row = {"type": "vlm_geo2", "target": tid, "rep": rep, "ok": r["ok"],
                   "elapsed_ms": r.get("elapsed_ms"), "reply": (r.get("text") or "")[:80]}
            if nums is not None:
                ex, ey = nums[0] * w, nums[1] * h
                dev = max(abs(ex - cx), abs(ey - cy))
                box_hit = (wr[0] <= ex <= wr[0] + wr[2]) and (wr[1] <= ey <= wr[1] + wr[3])
                devs.append(dev)
                inb += int(box_hit)
                row.update({"est": [round(ex, 1), round(ey, 1)], "dev_px": round(dev, 1),
                            "in_box": box_hit})
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            time.sleep(0.2)
        med = float(np.median(devs)) if devs else -1
        summary[tid] = (devs, inb)
        print("%-9s 框内 %d/%d | devs=%s 中位=%.0f" % (tid, inb, N_REPEAT,
                                                      [round(d) for d in devs], med))
    tot = sum(len(v[0]) for v in summary.values())
    hit = sum(v[1] for v in summary.values())
    print("\n总计: 框内命中 %d/%d = %.0f%%" % (hit, tot, 100 * hit / tot))
    all_dev = [d for v in summary.values() for d in v[0]]
    print("dev 中位=%.0f p75=%.0f p90=%.0f" % (np.median(all_dev),
                                               np.percentile(all_dev, 75),
                                               np.percentile(all_dev, 90)))


if __name__ == "__main__":
    main()
