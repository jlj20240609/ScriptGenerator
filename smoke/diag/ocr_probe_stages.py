# -*- coding: utf-8 -*-
"""阶段耗时拆解：det/cls/rec 各自耗时 + CPU 核数/机型信息"""
import sys, os, time, ctypes, platform
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()

print("machine:", platform.machine(), "processor:", platform.processor())
print("cores(logical):", os.cpu_count())
try:
    import subprocess
    out = subprocess.run(["wmic", "cpu", "get", "Caption,NumberOfCores,NumberOfLogicalProcessors"],
                         capture_output=True, text=True, timeout=20)
    print(out.stdout.strip())
except Exception as e:
    print("wmic fail:", e)

chip = m0lib.grab_screen((150, 150, 570, 400))
print("chip shape:", chip.shape)

from rapidocr import RapidOCR
e = RapidOCR()
for i in range(4):
    t0 = time.perf_counter()
    out = e(chip)
    total = (time.perf_counter() - t0) * 1000
    print("run%d total=%dms det/cls/rec=%s boxes=%d" % (
        i, total, [round(x, 1) for x in out.elapse_list],
        len(out.txts) if out is not None else -1))
