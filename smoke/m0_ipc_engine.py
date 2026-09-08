# -*- coding: utf-8 -*-
"""M0④ 引擎桩：JSON-line 协议（stdin 请求 → stdout 响应），复用 smoke/m0lib。
请求:   {"id":1,"method":"ping"} / {"id":2,"method":"screen.find_text","params":{"text":"库存查询"}}
响应:   {"id":1,"result":{...}} 或 {"id":2,"error":"..."}
坐标一律物理像素（进程已 DPI-aware）。可丢弃产物。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m0lib


def handle(method, params):
    if method == "ping":
        return {"pong": True, "scale_factor_hint": "physical px"}
    if method == "screen.find_text":
        text = (params or {}).get("text", "")
        region = (params or {}).get("region")  # [x,y,w,h] 物理像素，限定 OCR 区域（往返 <1s 的关键）
        t0 = time.perf_counter()
        bgr = m0lib.grab_screen()
        if region:
            x, y, w, h = [int(v) for v in region]
            x, y = max(0, x), max(0, y)
            bgr = bgr[y:y + h, x:x + w]
        r = m0lib.find_text_ocr(bgr, text)
        total_ms = (time.perf_counter() - t0) * 1000
        if r["ok"]:
            ox, oy = (x, y) if region else (0, 0)
            c = (r["center"][0] + ox, r["center"][1] + oy)
            return {"ok": True, "center": list(c), "score": r["score"],
                    "matched": r.get("matched_text"), "elapsed_ms": round(r["elapsed_ms"], 1),
                    "total_ms": round(total_ms, 1), "screen": [bgr.shape[1], bgr.shape[0]]}
        return {"ok": False, "center": None, "score": r["score"],
                "elapsed_ms": round(r["elapsed_ms"], 1), "total_ms": round(total_ms, 1)}
    raise ValueError("unknown method: %s" % method)


def main():
    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            result = handle(req.get("method"), req.get("params") or {})
            resp = {"id": req.get("id"), "result": result}
        except Exception as e:
            resp = {"id": req.get("id") if 'req' in dir() else None,
                    "error": "%s: %s" % (type(e).__name__, e)}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
