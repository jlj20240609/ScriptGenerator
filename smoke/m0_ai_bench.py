# -*- coding: utf-8 -*-
"""M0③ 云端 AI 视觉验证套件（智谱 glm-4.6v）
A. 页面 grounding 定位：对真实页面截图输出部件中心坐标 → 与双截图真值比对（通过线 ≥85% 近距命中）
B. 部件图文字识别：widget 裁剪图 → 文字/按钮名 → 与 ocr_text 比对
C. 语义判定（D3 预研）：界面文案四分类（登录成功/凭据错误/网络异常/其他）
D. 全程记录往返延迟 → data/ai_bench.jsonl + 汇总表
用法：$env:ZHIPU_API_KEY=... ; python smoke/m0_ai_bench.py
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import cv2

import m0lib

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("ZHIPU_MODEL", "glm-4.6v")
DATA = Path(__file__).parent / "data"
IMG = DATA / "target_img"
CAP = Path(__file__).parent / "targets"


def chat(prompt, image=None, max_w=1024):
    """image: BGR ndarray（自动缩放到 max_w 宽并 base64）。返回 (ok, text, elapsed_ms, usage)。"""
    content = [{"type": "text", "text": prompt}]
    scale = 1.0
    if image is not None:
        img = image
        h, w = img.shape[:2]
        if w > max_w:
            scale = max_w / w
            img = cv2.resize(img, (max_w, int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + b64}})
    body = {"model": MODEL, "messages": [{"role": "user", "content": content}],
            "temperature": 0.0}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"],
                                          "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        elapsed = (time.perf_counter() - t0) * 1000
        text = out["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "elapsed_ms": round(elapsed),
                "usage": out.get("usage", {}), "scale": scale}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")[:400]
            except Exception:
                pass
        return {"ok": False, "text": "", "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "error": repr(e), "detail": detail, "scale": scale}


def parse_xy(text):
    """从回复提取 'x y' 整数对（容忍逗号/括号/中文标点）"""
    nums = re.findall(r"-?\d{1,5}", text.replace(",", " ").replace("，", " "))
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None


def log(row):
    p = DATA / "ai_bench.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------- 任务集 ----------------

def task_grounding():
    """对页面截图定位部件中心。"""
    print("\n=== A. 页面 grounding 定位（要求模型输出部件中心像素坐标） ===")
    specs = [("login", "登录按钮里的“登录”文字"), ("erp", "左侧菜单“库存查询”"),
             ("dyn", "卡片标题“服务状态”"), ("tk", "任务表第一行的“M0桌面验证标记行”文字"),
             ("bili_set", "左侧菜单“常规设置”"), ("cpl", "窗口标题“控制面板”文字"),
             ("calc", "数字键“8”")]
    rows = []
    for tid, desc in specs:
        j = CAP / ("%s.json" % tid)
        if not j.exists():
            print("跳过 %s（无 target）" % tid)
            continue
        t = m0lib.Target.load(j)
        page = m0lib.load_png(IMG / t.page["file"])
        wr = t.widget["rect_in_page"]
        cx, cy = wr[0] + wr[2] // 2, wr[1] + wr[3] // 2  # 真值中心（部件框中心）
        prompt = ("这是软件界面截图。请找到%s，"
                  "给出该元素中心点的像素坐标。图片左上角为(0,0)。只输出两个整数，格式：x y") % desc
        r = chat(prompt, page)
        pt = parse_xy(r.get("text", "")) if r["ok"] else None
        dev = None
        hit_near = False
        hit_strict = False
        if pt is not None:
            px = pt[0] / r["scale"] if r["scale"] else pt[0]
            py = pt[1] / r["scale"] if r["scale"] else pt[1]
            dev = round(max(abs(px - cx), abs(py - cy)), 1)
            tol = max(40, int(min(wr[2], wr[3]) * 0.5))
            hit_near = dev <= tol
            hit_strict = (wr[0] <= px <= wr[0] + wr[2]) and (wr[1] <= py <= wr[1] + wr[3])
        status = "HIT" if hit_near else ("MISS" if pt else "NO_PT")
        print("%-9s 真值中心=%-8s AI=%s dev=%s 容差=%s %s (%sms)" %
              (tid, (cx, cy), pt, dev, hit_near, status, r.get("elapsed_ms")))
        rows.append({"type": "ai_grounding", "target": tid, "ok": r["ok"],
                     "hit_near": hit_near, "hit_strict": hit_strict, "dev_px": dev,
                     "model_xy": pt, "truth_xy": [cx, cy], "reply": r.get("text", "")[:80],
                     "elapsed_ms": r.get("elapsed_ms"), "usage": r.get("usage")})
        log(rows[-1])
        time.sleep(0.3)
    if rows:
        n = sum(1 for x in rows if x["hit_near"])
        print("grounding 近距命中: %d/%d = %.0f%%" % (n, len(rows), 100 * n / len(rows)))


def task_widget_ocr():
    print("\n=== B. 部件图文字识别（widget 裁剪 → 文字） ===")
    rows = []
    for tid in ("login", "erp", "dyn", "tk", "bili_set", "cpl", "calc"):
        j = CAP / ("%s.json" % tid)
        if not j.exists():
            continue
        t = m0lib.Target.load(j)
        wimg = m0lib.load_png(IMG / t.widget["file"])
        r = chat("这张截图是什么控件？用文字概括其名称，只输出最主要内容。", wimg)
        expect = m0lib.text_needle_short(t.ocr_text, 4)
        got = r.get("text", "") if r["ok"] else ""
        sim = m0lib.text_similar(expect, got) if got else 0.0
        okk = sim >= 0.55
        print("%-9s 期望=%-10r 识别=%-30r sim=%.2f %s (%sms)" %
              (tid, expect, got[:28], sim, "OK" if okk else "X", r.get("elapsed_ms")))
        rows.append({"type": "ai_widget_ocr", "target": tid, "ok": r["ok"] and okk,
                     "sim": round(sim, 3), "expect": expect, "got": got[:60],
                     "elapsed_ms": r.get("elapsed_ms")})
        log(rows[-1])
        time.sleep(0.3)
    if rows:
        n = sum(1 for x in rows if x["ok"])
        print("部件识别: %d/%d = %.0f%%" % (n, len(rows), 100 * n / len(rows)))


def task_semantic():
    print("\n=== C. 语义判定（界面文案四分类，D3 预研） ===")
    cases = [
        ("登录成功，欢迎回来", "登录成功"), ("操作已完成", "登录成功"), ("支付成功", "登录成功"),
        ("用户名或密码错误", "凭据错误"), ("密码不正确，请重试", "凭据错误"), ("账号或密码有误", "凭据错误"),
        ("网络连接失败，请检查网络", "网络异常"), ("请求超时，请稍后重试", "网络异常"), ("无法连接到服务器", "网络异常"),
        ("您有新消息", "其他"), ("系统维护中，请稍后再试", "其他"), ("版本已是最新", "其他"),
    ]
    rows = []
    for text, truth in cases:
        prompt = ("界面提示文案：“%s”。它属于下面哪一类？只输出类别名："
                  "登录成功 / 凭据错误 / 网络异常 / 其他") % text
        r = chat(prompt)
        got = (r.get("text") or "").strip() if r["ok"] else ""
        okk = truth[:2] in got or got in truth
        rows.append({"type": "ai_semantic", "case": text, "truth": truth, "got": got[:20],
                     "ok": okk, "elapsed_ms": r.get("elapsed_ms")})
        log(rows[-1])
        print("%-14s → %-16s 判定=%-10s %s (%sms)" %
              (truth, text[:14], got[:10], "OK" if okk else "X", r.get("elapsed_ms")))
        time.sleep(0.2)
    n = sum(1 for x in rows if x["ok"])
    print("语义判定: %d/%d = %.0f%%" % (n, len(rows), 100 * n / len(rows)))


if __name__ == "__main__":
    m0lib.setup_utf8_stdio()
    if not os.environ.get("ZHIPU_API_KEY"):
        raise SystemExit("缺少环境变量 ZHIPU_API_KEY")
    print("模型:", MODEL)
    task_grounding()
    task_widget_ocr()
    task_semantic()
    print("\n完成。记录写入 smoke/data/ai_bench.jsonl")
