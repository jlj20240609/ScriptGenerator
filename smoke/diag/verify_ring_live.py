# -*- coding: utf-8 -*-
"""真机验证：真实登录页上，输入框被填入内容后还能不能定位（环带路径）。

不动鼠标键盘：抓真实页面 → 用"请输入工号"定位输入框并裁剪成"录制态"模板 →
在图像上模拟"已填入内容" → 用同一 target 定位，看是否走 tpl_ring 且位置准确。
"""
import sys
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import capture, locator, matcher  # noqa: E402

capture.init_dpi_aware()
ws = [h for h in capture.find_windows_by_title("M0 演示登录") if not capture.is_iconic(h)]
if not ws:
    print("登录页不在场：先跑 engine/scripts/live_regress.py --case login --open-only")
    sys.exit(2)
hwnd = ws[0]
capture.bring_to_foreground(hwnd)
import time  # noqa: E402
time.sleep(1.0)
capture.demote_window(hwnd)
time.sleep(0.3)

l, t, r, b = capture.window_rect(hwnd)
win_rect = [l, t, r - l, b - t]
time.sleep(2.0)                        # 刚恢复/刚拉起的窗口要等渲染稳定（M0 经验 ~2s）
page = capture.grab_screen(win_rect)
print("登录页:", win_rect, "截图:", page.shape)

# ① 定位占位提示 → 推出输入框区域（= 录制时用户框住的那块）
# 注意：真机上"工号"框里可能已被填入内容（占位提示消失），这时用占位还在的密码框验证。
ocr0 = matcher.ocr_run(page)
print("整页 OCR（前 12）:", ocr0["txts"][:12])
needle = "请输入密码" if not any("请输入工号" in x for x in ocr0["txts"]) else "请输入工号"
print("用作'录制态'的占位提示:", needle)
res = {"ok": False}
for attempt in range(2):
    res = locator.locate_widget_on_screen(capture.grab_screen(), tuple(win_rect),
                                          {"text": needle, "match": "text_first"})
    if res["ok"]:
        break
    time.sleep(1.5)
if not res["ok"]:
    print("没定位到占位提示:", res.get("method"), res.get("elapsed_ms"))
    sys.exit(2)
bx, by, bw, bh = res["box"]
rx, ry = bx - l, by - t
box = [max(0, rx - 90), max(0, ry - 14), min(page.shape[1], rx + bw + 130),
       min(page.shape[0], ry + bh + 14)]
box_xywh = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
tpl = page[box_xywh[1]:box_xywh[1] + box_xywh[3], box_xywh[0]:box_xywh[0] + box_xywh[2]]
print("输入框（录制态）页内:", box_xywh, "模板:", tpl.shape,
      "环带:", matcher.ring_mask(tpl.shape[:2])[1])

spec = capture.make_page_spec(bgr=page, rect_in_screen=win_rect,
                              context={"process": "msedge.exe",
                                       "title": capture.window_title(hwnd)})
target = {"image": matcher.bgr_to_dataurl(tpl), "text": "请输入工号", "match": "auto",
          "rect_in_page": box_xywh,
          "center_in_page": [box_xywh[0] + box_xywh[2] // 2, box_xywh[1] + box_xywh[3] // 2],
          "page": spec}

# ② 模拟"已填入内容"：在真实截图上把输入框内容区写上一串字
im = Image.fromarray(page[:, :, ::-1])
d = ImageDraw.Draw(im)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
except Exception:
    font = ImageFont.load_default()
d.text((box_xywh[0] + 24, box_xywh[1] + 16), "demo-user-1234", font=font, fill=(25, 25, 25))
filled = im.convert("RGB").__array__()[:, :, ::-1].copy()
cv2.imwrite(str(Path(__file__).with_name("accept") / "ring_live.png"), filled)

# ③ 用同一 target 定位"已填入"的画面
rf = locator.locate_widget_on_screen(filled, tuple(win_rect), target)
exp = (win_rect[0] + target["center_in_page"][0], win_rect[1] + target["center_in_page"][1])
dev = (max(abs(rf["center"][0] - exp[0]), abs(rf["center"][1] - exp[1]))
       if rf.get("center") else None)
print(f"填入后定位: ok={rf['ok']} method={rf.get('method')} level={rf.get('level')} "
      f"center={rf.get('center')} 期望={exp} 偏差={dev} "
      f"整块best={rf['detail'].get('l2_tpl_best')} 环带={rf['detail'].get('l2_ring_score')} "
      f"{round(rf['elapsed_ms'])}ms")
