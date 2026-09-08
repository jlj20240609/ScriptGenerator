# -*- coding: utf-8 -*-
"""探测 rec_batch_num 对 rec 阶段耗时的影响（venv: 官方 onnxruntime CPU）"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

chip = m0lib.grab_screen((150, 150, 570, 400))
print("chip:", chip.shape)

def bench(batch, n=3):
    from rapidocr import RapidOCR
    params = {"Rec.rec_batch_num": batch}
    e = RapidOCR(params=params)
    res = []
    for i in range(n):
        t0 = time.perf_counter()
        out = e(chip)
        res.append((time.perf_counter() - t0) * 1000)
        el = out.elapse_list if out is not None else None
        nb = len(out.txts) if out is not None else -1
        print("batch=%-3d run%d: total=%dms det/cls/rec=%s boxes=%d" %
              (batch, i, res[-1], [round(x, 1) for x in el], nb))
    return res

for b in (6, 24, 48):
    bench(b)
