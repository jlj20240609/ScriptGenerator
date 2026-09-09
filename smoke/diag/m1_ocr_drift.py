# -*- coding: utf-8 -*-
"""诊断：ORT intra 线程数对连续 OCR 漂移的影响。"""
import sys
import time

sys.path.insert(0, ".")
import numpy as np

from rapidocr import RapidOCR
from engine.tests import support as S

strip = S.home_page()[0][100:200, 0:400]


def main(intra):
    eng = RapidOCR(params={"Det.limit_side_len": 960, "Det.limit_type": "max",
                           "Global.log_level": "error",
                           "EngineConfig.onnxruntime.intra_op_num_threads": intra,
                           "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    print("intra=%d warm..." % intra)
    eng(strip)
    eng(strip)
    for i in range(22):
        t0 = time.perf_counter()
        eng(strip)
        print("intra=%d call %02d: %.0fms" % (intra, i, (time.perf_counter() - t0) * 1000),
              flush=True)


if __name__ == "__main__":
    main(int(int(sys.argv[1])))
