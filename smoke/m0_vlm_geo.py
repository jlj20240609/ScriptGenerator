# -*- coding: utf-8 -*-
"""M0③-VLM 像素定位深测矩阵
变量：
  model    : glm-4.6v / glm-4v-flash / glm-4v-plus
  size     : orig(原图) / w1024 / w768
  style    : point_px(中心像素 x y) / bbox_px(左上+右下) / point_norm(归一化0~1) / think(先分析再坐标)
  repeat   : 每配置重复次数（默认1）
判定：落点相对真值中心的像素偏差；多个容差档位 + 框内命中；输出逐条与汇总。
用法：$env:ZHIPU_API_KEY=... ; python smoke/m0_vlm_geo.py [--model glm-4.6v] [--size orig]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

import m0lib

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DATA = Path(__file__).parent / "data"
IMG = DATA / "target_img"
CAP = Path(__file__).parent / "targets"

SPECS = [
    ("login", "登录按钮里的“登录”文字", "原图"),
    ("erp", "左侧菜单“库存查询”", "原图"),
    ("dyn", "卡片标题“服务状态”", "原图"),
    ("tk", "任务表第一行内容里的“M0桌面验证标记行”", "原图"),
    ("bili_set", "设置页左侧菜单“常规设置”", "原图"),
    ("cpl", "窗口标题栏里的“控制面板”文字", "原图"),
    ("calc", "计算器数字键“8”", "原图"),
]

PROMPTS = {
    "point_px": "这是软件界面截图。请找到“{desc}”，给出其中心点的像素坐标，图片左上角为(0,0)，宽度{w}像素、高度{h}像素。只输出两个整数：x y",
    "bbox_px": "这是软件界面截图。请框出“{desc}”所在位置，输出包围它的矩形的左上角与右下角像素坐标（图片左上角为(0,0)）。只输出4个整数：x1 y1 x2 y2",
    "point_norm": "这是软件界面截图。请找到“{desc}”的中心点，输出其相对坐标：横向比例与纵向比例，取值0~1之间（图片左上角为0,0，右下角为1,1）。只输出两个小数：cx cy",
    "think": "这是软件界面截图。第一步：观察整图，找到“{desc}”；第二步：估算它中心点离图片左边缘与上边缘的像素距离。最后只输出两个整数：x y",
}


def call(model, prompt, image, timeout=240):
    ok, buf = cv2.imencode(".png", image)
    b64 = base64 = __import__("base64").b64encode(buf.tobytes()).decode("ascii")
    body = {"model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
            "temperature": 0.0}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"],
                                          "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "text": out["choices"][0]["message"]["content"],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
        return {"ok": False, "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "error": repr(e), "detail": detail}


def parse_ints(text, n):
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", " ").replace("，", " "))
    return [float(x) for x in nums[:n]] if len(nums) >= n else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="glm-4.6v")
    ap.add_argument("--size", default="orig", choices=["orig", "w1024", "w768"])
    ap.add_argument("--style", default="all", choices=["all", "point_px", "bbox_px", "point_norm", "think"])
    ap.add_argument("--targets", default="", help="逗号分隔目标子集")
    args = ap.parse_args()
    m0lib.setup_utf8_stdio()
    styles = list(PROMPTS) if args.style == "all" else [args.style]
    want = {s.strip() for s in args.targets.split(",") if s.strip()}
    out_path = DATA / "vlm_geo.jsonl"
    print("模型=%s 尺寸=%s 提示=%s" % (args.model, args.size, styles))
    hits = {s: [0, 0] for s in styles}
    for tid, desc, _ in SPECS:
        if want and tid not in want:
            continue
        j = CAP / ("%s.json" % tid)
        if not j.exists():
            print("跳过 %s" % tid)
            continue
        t = m0lib.Target.load(j)
        page = m0lib.load_png(IMG / t.page["file"])
        h, w = page.shape[:2]
        if args.size == "w1024" and w > 1024:
            page = cv2.resize(page, (1024, int(h * 1024 / w)))
        elif args.size == "w768" and w > 768:
            page = cv2.resize(page, (768, int(h * 768 / w)))
        h2, w2 = page.shape[:2]
        sx, sy = w / w2, h / h2  # 小图坐标 → 原图坐标倍率
        wr = t.widget["rect_in_page"]
        cx, cy = wr[0] + wr[2] / 2, wr[1] + wr[3] / 2
        box = wr
        for style in styles:
            prompt = PROMPTS[style].format(desc=desc, w=w2, h=h2)
            r = call(args.model, prompt, page)
            need = 4 if style == "bbox_px" else 2
            nums = parse_ints(r.get("text", ""), need) if r["ok"] else None
            row = {"type": "vlm_geo", "model": args.model, "size": args.size,
                   "style": style, "target": tid, "ok": r["ok"],
                   "elapsed_ms": r.get("elapsed_ms"), "reply": (r.get("text") or "")[:100]}
            est_c = None
            if nums is not None and style in ("point_px", "point_norm", "think"):
                if style == "point_norm":
                    ex, ey = nums[0] * w, nums[1] * h
                else:
                    ex, ey = nums[0] * sx, nums[1] * sy
                est_c = (ex, ey)
                dev = max(abs(ex - cx), abs(ey - cy))
                in_box = box[0] <= ex <= box[0] + box[2] and box[1] <= ey <= box[1] + box[3]
                row.update({"est": [round(ex, 1), round(ey, 1)], "truth": [cx, cy],
                            "dev_px": round(dev, 1), "in_box": in_box,
                            "tol60": dev <= 60, "tol120": dev <= 120})
                print("%-9s %-10s dev=%6.1f 框内=%s est=%s truth=%s (%sms)" %
                      (tid, style, dev, in_box, row["est"], row["truth"], r.get("elapsed_ms")))
                hits[style][0] += 1
                hits[style][1] += 1 if in_box else 0
            elif nums is not None and style == "bbox_px":
                x1, y1, x2, y2 = nums
                bbox = (x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy)
                est_c = ((bbox[0] + bbox[2] / 2), (bbox[1] + bbox[3] / 2))
                dev = max(abs(est_c[0] - cx), abs(est_c[1] - cy))
                # IoU 与真值框
                inter_w = max(0, min(box[0] + box[2], bbox[0] + bbox[2]) - max(box[0], bbox[0]))
                inter_h = max(0, min(box[1] + box[3], bbox[1] + bbox[3]) - max(box[1], bbox[1]))
                inter = inter_w * inter_h
                union = box[2] * box[3] + bbox[2] * bbox[3] - inter
                iou = inter / union if union > 0 else 0.0
                row.update({"est": [round(v, 1) for v in est_c], "truth": [cx, cy],
                            "dev_px": round(dev, 1), "iou": round(iou, 3),
                            "bbox": [round(v, 1) for v in bbox]})
                print("%-9s %-10s dev=%6.1f IoU=%.2f bbox=%s truth_center=%s (%sms)" %
                      (tid, style, dev, iou, row["bbox"], [cx, cy], r.get("elapsed_ms")))
                hits[style][0] += 1
                hits[style][1] += 1 if iou >= 0.3 else 0
            else:
                row.update({"note": "parse_fail or api_fail"})
                print("%-9s %-10s 解析失败/API失败 (%sms) reply=%s" %
                      (tid, style, r.get("elapsed_ms"), (r.get("text") or r.get("error"))[:60]))
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            time.sleep(0.3)
    print("\n=== 命中汇总（style: 命中/总数） ===")
    for s in styles:
        if hits[s][0]:
            print("%-12s 有效 %d，命中 %d (%.0f%%)" % (s, hits[s][0], hits[s][1],
                                                    100 * hits[s][1] / hits[s][0]))
    print("记录 →", out_path)


if __name__ == "__main__":
    main()
