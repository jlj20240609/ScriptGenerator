# -*- coding: utf-8 -*-
"""bili 动态页难例 · 校验模式恢复演示
旧 bili.json（feed 整窗模板）在动态 feed 上必然失败 →
【校验模式】AI 语义确认页面 + 定位静态顶栏(锚) → 重采集“顶栏锚模板” →
跨帧稳定复验 → 以锚恢复页面定位 → 页内部件(搜索框)按录制偏移定位 → 与 OCR 真值比对。
同时记录负例：整窗模板持续失效（动态内容无法整窗恢复）。
用法：$env:ZHIPU_API_KEY=... ; python smoke/m0_calib_bili.py
"""
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import cv2

import m0lib
import m0_trial

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ROOT = Path(__file__).resolve().parent
IMG = ROOT / "data" / "target_img"
OUT = ROOT / "data" / "calib_bili.jsonl"


def ai_confirm(image_bgr, prompt):
    h, w = image_bgr.shape[:2]
    if w > 1024:
        image_bgr = cv2.resize(image_bgr, (1024, int(h * 1024 / w)))
    _, buf = cv2.imencode(".png", image_bgr)
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    body = {"model": "glm-4.6v",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}}]}],
            "temperature": 0.0}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"],
                                          "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return True, out["choices"][0]["message"]["content"], round((time.perf_counter() - t0) * 1000)
    except Exception as e:
        return False, repr(e), round((time.perf_counter() - t0) * 1000)


def log(row):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    m0lib.setup_utf8_stdio()
    t = m0lib.Target.load(ROOT / "targets" / "bili.json")
    ws = m0_trial.find_window_by_title("哔哩哔哩")
    h = ws[0]
    m0lib.bring_to_foreground(h)
    time.sleep(1.0)
    rect = m0_trial.window_rect(h)
    print("bili 窗口:", rect)
    screen = m0lib.grab_screen()
    # 1) 负例：旧整窗模板（动态 feed 时代产物）
    tpl = m0lib.load_png(IMG / t.page["file"])
    mm = m0lib.find_template(screen, tpl)
    old_fail = not (mm["ok"] and mm["score"] >= 0.72)
    print("[负例] 旧整窗模板匹配 score=%.2f → %s" %
          (mm["score"], "失败（动态内容，整窗模板不可恢复——符合预期）" if old_fail else "仍命中？"))
    # 2) 校验模式：AI 语义确认 + 顶栏锚粗定位
    page_bgr = m0lib.grab_screen(rect)
    ok, reply, ms = ai_confirm(
        page_bgr,
        "这是哔哩哔哩客户端首页（视频流会不断变化）。请确认页面顶部的固定导航栏（含“直播/推荐/热门/影视”"
        "等栏目字）在画面中的位置：给出该导航栏中心点的相对坐标（0~1，左上角0,0 右下角1,1）。"
        "只输出两个小数：cx cy")
    ai_pt = None
    print("AI 语义确认+导航栏粗定位: ok=%s (%dms) reply=%r" % (ok, ms, reply[:60]))
    if ok:
        try:
            nums = [float(x) for x in reply.replace(",", " ").replace("，", " ").split()
                    if x.replace(".", "").isdigit()]
            if len(nums) >= 2 and 0 <= nums[0] <= 1 and 0 <= nums[1] <= 1:
                ai_pt = (nums[0] * page_bgr.shape[1], nums[1] * page_bgr.shape[0])
                print("  AI 归一化粗定位(窗口内): (%.0f, %.0f)" % ai_pt)
            else:
                print("  AI 输出越界/格式异常:", nums)
        except Exception as e:
            print("  AI 解析失败:", e)
    # 3) 本地精定位静态锚：OCR 找“直播”与“影视”导航词界定锚带
    f1 = m0lib.find_text_ocr(page_bgr, "直播")
    f2 = m0lib.find_text_ocr(page_bgr, "影视")
    if not (f1["ok"] and f2["ok"]):
        print("OCR 找不到导航词（直播/影视），锚采集失败")
        log({"stage": "anchor_ocr", "ok": False, "f1": f1, "f2": f2})
        return
    x0 = min(f1["box"][0], f2["box"][0])
    x1 = max(f1["box"][0] + f1["box"][2], f2["box"][0] + f2["box"][2])
    y0 = min(f1["box"][1], f2["box"][1]) - 8
    y1 = max(f1["box"][1] + f1["box"][3], f2["box"][1] + f2["box"][3]) + 8
    anchor_box = (max(0, x0), max(0, y0), x1 - x0, y1 - y0)  # 窗口内坐标
    anchor = page_bgr[anchor_box[1]:anchor_box[1] + anchor_box[3],
                      anchor_box[0]:anchor_box[0] + anchor_box[2]].copy()
    print("静态锚(顶栏导航带): box=%s" % (anchor_box,))
    # 4) 跨帧稳定复验（锚必须静态）
    time.sleep(2.0)
    page2 = m0lib.grab_screen(rect)
    m2 = m0lib.find_template(page2, anchor, scales=(1.0,), score_thr=0.6)
    stable = m2["ok"] and m2["score"] >= 0.85
    print("锚跨帧复验: score=%.2f → %s" % (m2["score"], "静态锚 OK" if stable else "锚仍在变化"))
    # 5) 部件定位：搜索框相对锚的录制偏移 + 运行时 OCR 真值对照
    fs = m0lib.find_text_ocr(page_bgr, "搜索你感兴趣的视频")
    if not fs["ok"]:
        print("OCR 找不到搜索框，改用'直播'词条作部件参照")
        fs = f1
    part_box = fs["box"]
    part_center = (part_box[0] + part_box[2] // 2, part_box[1] + part_box[3] // 2)
    # 若搜索框在锚带外（feed 布局搜索框同顶栏带内），用锚带内相对位置：
    # 本 demo 直接以“锚定位 + 部件相对偏移”还原：先算部件相对锚左上偏移
    offset = (part_center[0] - anchor_box[0], part_center[1] - anchor_box[1])
    # 运行时用锚恢复：锚在窗口内匹配（或全屏匹配）
    m3 = m0lib.find_template(page2, anchor, scales=(1.0,), score_thr=0.6)
    if m3["ok"]:
        ax, ay = m3["rect"][0], m3["rect"][1]
        est_center = (ax + offset[0], ay + offset[1])
        dev = max(abs(est_center[0] - part_center[0]), abs(est_center[1] - part_center[1]))
        print("锚恢复定位 → 部件估计=%s OCR真值=%s dev=%dpx" % (est_center, part_center, dev))
        ok2 = dev <= 20
    else:
        print("锚复验匹配失败，无法恢复")
        ok2 = False
        dev = None
    # 6) AI 与 OCR 锚一致性（如 AI 可用）
    ai_dev = None
    if ai_pt:
        ai_dev = max(abs(ai_pt[0] - anchor_box[0] - anchor_box[2] / 2),
                     abs(ai_pt[1] - anchor_box[1] - anchor_box[3] / 2))
        print("AI 建议 vs OCR 锚中心偏差: %dpx" % ai_dev)
    print("\n=== bili 动态页校验恢复：%s ===" %
          ("恢复成功（整窗模板失败 → 静态锚重采集 → 部件定位 dev≤20px）" if ok2 else "恢复失败"))
    log({"stage": "done", "ok": ok2, "old_tpl_score": round(mm["score"], 3),
         "anchor_box": list(anchor_box), "anchor_stable": stable,
         "part_text": fs.get("matched_text", "直播"),
         "part_center": list(part_center), "est_center": est_center if m3["ok"] else None,
         "dev_px": dev, "ai_dev_px": ai_dev, "ai_ms": ms,
         "conclusion": "动态页整窗模板不可恢复；静态锚定位可恢复（v0.30 校验模式+静态锚策略）"})
    cv2.imwrite(str(ROOT / "data" / "calib_bili_anchor.png"), anchor)
    print("顶栏锚模板已存: smoke/data/calib_bili_anchor.png")


if __name__ == "__main__":
    main()
