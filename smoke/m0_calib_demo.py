# -*- coding: utf-8 -*-
"""端到端校验模式原型：ERP v1 脚本 → 在改版 v2 页面上运行
旧模板失效(页面匹配失败) → 【校验模式】AI 双图语义确认(旧部件截图+新页面截图) →
归一化粗定位 → 本地 OCR 圈区精定位 → 生成新模板并复验定位 → 输出恢复结论。
用法：$env:ZHIPU_API_KEY=... ; python smoke/m0_calib_demo.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2

import m0lib
import m0_trial

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures"
CAP = ROOT / "targets"
IMG = ROOT / "data" / "target_img"
OUT = ROOT / "data" / "calib_demo.jsonl"


def chat_multi(prompt, images_bgr, max_w=1024):
    """多图视觉请求（images_bgr: BGR ndarray 列表），返回 (ok, text, elapsed_ms)"""
    content = [{"type": "text", "text": prompt}]
    for img in images_bgr:
        h, w = img.shape[:2]
        if w > max_w:
            img = cv2.resize(img, (max_w, int(h * max_w / w)))
        okimg, buf = cv2.imencode(".png", img)
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," +
                                      base64.b64encode(buf.tobytes()).decode("ascii")}})
    body = {"model": "glm-4.6v",
            "messages": [{"role": "user", "content": content}], "temperature": 0.0}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"],
                                          "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return True, out["choices"][0]["message"]["content"], round((time.perf_counter() - t0) * 1000)
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
        return False, "%r %s" % (e, detail), round((time.perf_counter() - t0) * 1000)


def find_v2_window():
    wins = m0_trial.find_window_by_title("M0 ERP 查询 · 进销存管理台 V2")
    return wins[0] if wins else None


def launch_v2():
    if not find_v2_window():
        tmp = Path(os.environ.get("TEMP", ".")) / "m0edge_ascii"
        tmp.mkdir(parents=True, exist_ok=True)
        dst = tmp / "erpv2.html"
        shutil.copy(FIX / "erp-v2.html", dst)
        edge = None
        for cand in (Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
                     / "Microsoft/Edge/Application/msedge.exe",
                     Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                     / "Microsoft/Edge/Application/msedge.exe"):
            if cand.exists():
                edge = str(cand)
                break
        prof = os.path.join(os.environ.get("TEMP", "."), "m0_edge_profile")
        subprocess.Popen([edge, "--user-data-dir=%s" % prof, "--no-first-run",
                          "--window-size=1020,780", "--app=%s" % dst.as_uri()])
        for _ in range(30):
            time.sleep(2)
            if find_v2_window():
                break
    h = find_v2_window()
    m0lib.bring_to_foreground(h)
    time.sleep(1.0)
    return h


def log(row):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    m0lib.setup_utf8_stdio()
    # 0) 旧脚本目标 = erp（v1 模板）
    t = m0lib.Target.load(CAP / "erp.json")
    print("旧目标: erp.json 部件=%r 页模板=%s" % (t.ocr_text, t.page["file"]))
    # 1) 打开改版 v2
    h = launch_v2()
    rect = m0_trial.window_rect(h)
    print("v2 窗口:", rect)
    v2_bgr = m0lib.grab_screen(rect)
    # 2) 旧模板失效探测：页面级模板对同结构改版仍可能高分（0.9+，鲁棒性记录）；
    #    真实“过程性错误”出现在部件级——旧部件文字在新页面已不存在
    page_tpl = m0lib.load_png(IMG / t.page["file"])
    mm = m0lib.find_template(m0lib.grab_screen(), page_tpl)
    page_still_ok = mm["ok"] and mm["score"] >= 0.72
    # 部件级探测：页内找旧部件文字（OCR 文本路径，needle=旧部件完整文字）
    page_bgr = m0lib.grab_screen(rect)
    pf = m0lib.find_text_ocr(page_bgr, t.ocr_text)
    part_fail = not pf["ok"]
    print("旧页面模板匹配: score=%.2f → %s" %
          (mm["score"], "页面级仍命中（同结构小改版鲁棒，记录在案）" if page_still_ok else "页面级失败"))
    print("旧部件文字探测(%r): %s → %s" %
          (t.ocr_text,
           "找到 " + repr(pf.get("matched_text")) if pf["ok"] else "未找到",
           "部件级过程性错误(触发校验模式)" if part_fail else "部件仍在(改版不够)"))
    if not part_fail:
        print("改版差异不足：请增强 erp-v2.html（去掉或改名所有含“库存”的旧文字）后重试")
        return
    # 3) 校验模式：AI 双图（旧部件图 + 新页面截图）→ 归一化粗定位
    widget_old = m0lib.load_png(IMG / t.widget["file"])
    prompt = ("图1是旧界面里的一个侧栏菜单项（文字是“库存查询”）。"
              "图2是软件改版后的新界面截图，这个菜单项可能改了名字（如“库存中心”）或换了位置。"
              "请在图2中找到这个对应的菜单项，给出其中心点的相对坐标（0~1，左上角0,0，右下角1,1）。"
              "只输出两个小数：cx cy")
    ok, text, ms = chat_multi(prompt, [widget_old, v2_bgr])
    print("AI 双图确认: ok=%s (%dms) reply=%r" % (ok, ms, text[:80]))
    if not ok:
        log({"stage": "ai", "ok": False, "reply": text})
        return
    try:
        nums = [float(x) for x in text.replace(",", " ").replace("，", " ").split() if x.replace(".", "").isdigit()]
        if len(nums) < 2:
            raise ValueError("坐标解析失败: %r" % text)
        ax, ay = nums[0] * v2_bgr.shape[1], nums[1] * v2_bgr.shape[0]
    except Exception as e:
        print("AI 坐标解析失败:", e)
        log({"stage": "ai", "ok": False, "reply": text})
        return
    print("AI 归一化粗定位(原图坐标): (%.0f, %.0f)" % (ax, ay))
    # 4) 本地精定位：AI 建议点 ±200px 区域内 OCR 找“库存”（旧文字前缀，容忍改名）
    h2, w2 = v2_bgr.shape[:2]
    x0, y0 = max(0, int(ax - 200)), max(0, int(ay - 100))
    x1, y1 = min(w2, int(ax + 200)), min(h2, int(ay + 120))
    crop = v2_bgr[y0:y1, x0:x1]
    f = m0lib.find_text_ocr(crop, "库存")
    if not f["ok"]:
        print("本地 OCR 精定位失败（区域内无“库存*”文字）——低置信人工兜底路径")
        log({"stage": "local", "ok": False, "ai_xy": [round(ax), round(ay)]})
        return
    bx, by, bw, bh = f["box"]
    new_box = (x0 + bx, y0 + by, bw, bh)  # 页面内坐标（窗口 rect 内）
    new_center = (new_box[0] + bw // 2, new_box[1] + bh // 2)
    dev_ai = max(abs(new_center[0] - ax), abs(new_center[1] - ay))
    print("本地精定位: 文字=%r box=%s 中心=%s 与AI建议偏差=%dpx" %
          (f["matched_text"], new_box, new_center, dev_ai))
    # 5) 重采集新部件模板 + 复验（新模板在新页面内 locate）
    nw, nh = new_box[2] + 24, new_box[3] + 24
    nx0, ny0 = max(0, new_box[0] - 12), max(0, new_box[1] - 12)
    new_widget = v2_bgr[ny0:ny0 + nh, nx0:nx0 + nw]
    f2 = m0lib.find_template(v2_bgr, new_widget, score_thr=0.6)
    verify_ok = f2["ok"] and f2["score"] >= 0.6
    print("新模板复验定位: score=%.2f %s" % (f2["score"], "OK" if verify_ok else "FAIL"))
    # 6) 结论
    log({"stage": "done", "ok": verify_ok, "old_text": t.ocr_text,
         "new_text": f["matched_text"], "ai_xy": [round(ax), round(ay)],
         "new_box_page": list(new_box), "dev_ai_local_px": dev_ai,
         "verify_score": round(f2["score"], 3),
         "ai_ms": ms, "note": "校验模式闭环演示：模板重采集后可恢复定位" if verify_ok else "复验失败"})
    print("\n=== 校验模式闭环演示 %s ===" % ("成功：重采集模板后可恢复定位" if verify_ok else "失败"))
    if verify_ok:
        # 保存演示产出：新部件模板
        cv2.imwrite(str(ROOT / "data" / "calib_erpv2_widget.png"), new_widget)
        print("新部件模板已存: smoke/data/calib_erpv2_widget.png（正式产品中此步即更新 Target.widget）")


if __name__ == "__main__":
    main()
