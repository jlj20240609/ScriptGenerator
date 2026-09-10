# -*- coding: utf-8 -*-
"""诊断：点了登录之后"密码错误"到底有没有显示、OCR/抓屏为什么读不到。

跑法：python smoke/diag/probe_login_tip.py
产出：smoke/diag/login_initial.png / login_after_click.png（供视觉核对）
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from engine import capture, matcher  # noqa: E402
from engine.executor import LiveDriver, RunConfig, run_script  # noqa: E402
from engine.scripts import bench as B  # noqa: E402

OUT = ROOT / "smoke" / "diag"
capture.init_dpi_aware()
case = B.CASES["login_full"]

hwnd, shot, page_rect, spec = B._case_window_and_page(case, must="登录")
B.reset_page(hwnd, case)                      # f5：清空表单（fixture 的 load 事件负责清空）
hwnd, shot, page_rect, spec = B._case_window_and_page(case, must="登录")
print(f"hwnd={hwnd} iconic={capture.is_iconic(hwnd)} page_rect={page_rect} "
      f"页面图 shape={shot.shape}")


def probe_texts(img, tag):
    """关键文字能不能被读到（含分数与位置）。"""
    for word in ("密码错误", "登录", "登 录", "密码", "工号", "请输入工号"):
        hits = matcher.find_text_all_ocr(img, word, thr=0.4)
        got = [(h["matched_text"], round(h["score"], 3), list(h["box"])) for h in hits[:2]]
        print(f"  [{tag}] {word!r} → {got or '未读到'}")


cv2.imwrite(str(OUT / "login_initial.png"), shot)
print(f"初始页面图已存（{shot.shape[1]}x{shot.shape[0]}）")
probe_texts(shot, "初始")
print(f"阶段 OCR 全量文字：{matcher.ocr_run(shot)['txts'][:14]}")

w = B._widget_factory(shot, page_rect, spec)
steps = [
    {"id": "s1", "type": "action", "action": "type", "params": {"text": "demo"},
     "target": w("请输入工号")},
    {"id": "s2", "type": "action", "action": "type", "params": {"text": "wrong-pass"},
     "target": w("请输入密码")},
    {"id": "s3", "type": "action", "action": "click", "params": {},
     "target": w("登录", pad_x=24)},
]
ph_human, ph_log = B.BenchHuman(), B.CountLogger()
rep = run_script({"version": "1.0", "name": "probe", "targets_rev": 0, "steps": steps},
                 LiveDriver({"hwnd": hwnd}), cfg=RunConfig(guard=True),
                 loc_logger=ph_log, human=ph_human)
print(f"探针 status={rep.get('status')} 人工={[c[0] for c in ph_human.calls]} "
      f"err={rep.get('error')} 日志={len(ph_log.rows)}条")
for st in rep.get("steps") or []:
    print(f"  · {st.get('status')} {st.get('label') or st.get('action')} "
          f"{st.get('box') or ''}")

time.sleep(1.8)
shot2 = capture.grab_screen(page_rect)
cv2.imwrite(str(OUT / "login_after_click.png"), shot2)
probe_texts(shot2, "点击后")
print(f"点击后 OCR 全量文字：{matcher.ocr_run(shot2)['txts'][:14]}")

if shot is not None and shot2 is not None and shot.shape == shot2.shape:
    diff = cv2.absdiff(shot, shot2)
    changed = int((diff.max(axis=2) > 24).sum())
    print(f"前后差异像素={changed}（>200 说明页面确实变了）")

# 再点一次：验证"第一次点击被激活窗口吞掉"这个假说
print("再点一次「登录」…")
again = run_script({"version": "1.0", "name": "click-again", "targets_rev": 0,
                    "steps": [{"id": "s1", "type": "action", "action": "click",
                               "params": {}, "target": w("登录", pad_x=24)}]},
                   LiveDriver({"hwnd": hwnd}), cfg=RunConfig(guard=True),
                   loc_logger=B.CountLogger(), human=B.BenchHuman())
print(f"  第二次点击 status={again.get('status')}")
time.sleep(1.6)
shot3 = capture.grab_screen(page_rect)
cv2.imwrite(str(OUT / "login_after_click2.png"), shot3)
probe_texts(shot3, "再点一次后")
print(f"再点后 OCR 全量文字：{matcher.ocr_run(shot3)['txts'][:14]}")
if shot2 is not None and shot3 is not None and shot2.shape == shot3.shape:
    changed2 = int((cv2.absdiff(shot2, shot3).max(axis=2) > 24).sum())
    print(f"两次点击之间差异像素={changed2}")
print(f"截图：{OUT / 'login_initial.png'} / {OUT / 'login_after_click.png'} / "
      f"{OUT / 'login_after_click2.png'}")
