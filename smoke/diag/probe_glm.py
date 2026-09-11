# -*- coding: utf-8 -*-
"""验证智谱 glm-4.6v 的连通性与两项 M3 能力（不回显 Key 内容）。

用法：python smoke/diag/probe_glm.py
检查四件事：
  ① Key 是否可获取（环境变量 → 本地 .secrets 文件兜底；只打印长度与前 4 位）
  ② AiGate 关卡：未授权时 enabled 必须为 False（首次启用要显式授权）
  ③ 连通性 + 中文读字（用 PIL 画中文，cv2.putText 画不出中文，之前踩过）
  ④ D3 语义判定雏形：拿真实页面截图问"有没有报错 / 有没有某个按钮"，要求返回 JSON
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from engine import ai as A  # noqa: E402

env_key = os.environ.get("ZHIPU_API_KEY", "")
file_key = A.local_api_key()
key = env_key or file_key
print(f"① Key：环境变量={'有' if env_key else '无'}，本地文件兜底={'有' if file_key else '无'}"
      f"（长度 {len(key)}，前缀 {key[:4] + '...' if key else '-'}）")
print(f"   模型：{A.DEFAULT_MODEL}   端点：{A.API_URL}")

vlm = A.ZhipuVLM()
print(f"② 未授权时 enabled={vlm.enabled}（应为 False —— 首次启用必须显式授权）")


def _agree(msg):
    print(f"   授权提示：{msg}")
    return True          # 注意：回调必须返回 True 才代表同意（返回 None 会被当成拒绝）


ok = vlm.authorize(notify=_agree)
print(f"   授权结果={ok} → enabled={vlm.enabled}")

# ③ 合成图读中文（PIL + 微软雅黑；cv2.putText 不支持中文）
img_pil = Image.new("RGB", (620, 150), (246, 246, 246))
d = ImageDraw.Draw(img_pil)
f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 38)
d.text((20, 50), "密码错误，请重新输入", font=f, fill=(200, 40, 40))
img = np.array(img_pil)[:, :, ::-1].copy()
r = vlm._chat("这张图里有什么文字？只回答文字本身，不要标点以外的解释。", [img])
print(f"③ 合成图读字：ok={r['ok']} 耗时={r['elapsed_ms']:.0f}ms")
print(f"   模型回答：{r.get('text', '').strip()[:120]!r}")
if not r["ok"]:
    print(f"   错误：{r.get('error')}\n   详情：{str(r.get('detail'))[:300]}")

# ④ 真实页面截图 + 语义判定（D3 雏形）
shot = ROOT / "smoke" / "diag" / "interference_page.png"
if shot.exists() and cv2.imread(str(shot)) is not None:
    page = cv2.imread(str(shot))
    q = ("这是一个企业系统的界面截图。请只输出 JSON：\n"
         '{"error": "<界面上出现的错误提示原文，没有就空字符串>", '
         '"has_query_button": "<有/没有>"}')
    t0 = time.perf_counter()
    r2 = vlm._chat(q, [page])
    print(f"④ 真实截图语义判定：ok={r2['ok']} 耗时={r2['elapsed_ms']:.0f}ms")
    print(f"   模型回答：{r2.get('text', '').strip()[:300]!r}")
else:
    print("④ 找不到真实截图，跳过")
