# -*- coding: utf-8 -*-
"""探测：默认 EP vs 强制 CPU EP 的 RapidOCR 延迟对比（M0 延迟问题定位）"""
import sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import numpy as np

chip = m0lib.grab_screen((200, 200, 420, 220))  # 稳定小区域
print("chip shape:", chip.shape)

def timed(engine, img, n=3):
    times = []
    for i in range(n):
        t0 = time.perf_counter()
        out = engine(img)
        times.append((time.perf_counter() - t0) * 1000)
    return times, (len(out.txts) if out is not None else -1)

# 1) 默认
from rapidocr import RapidOCR
e1 = RapidOCR()
ts1, n1 = timed(e1, chip)
print("默认配置: %d boxes, ms=%s" % (n1, [int(x) for x in ts1]))

# 2) 强制 CPU EP
e2 = RapidOCR(params={"EngineConfig.onnxruntime.use_dml": False})
ts2, n2 = timed(e2, chip)
print("CPU-only : %d boxes, ms=%s" % (n2, [int(x) for x in ts2]))
