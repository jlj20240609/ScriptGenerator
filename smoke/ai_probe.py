# -*- coding: utf-8 -*-
"""智谱 AI 连通性探测：试模型名与返回结构"""
import base64
import json
import os
import sys
import time
import urllib.request

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def chat(model, prompt, image_b64=None, timeout=60):
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + image_b64}})
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "temperature": 0.1}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"],
                                          "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        elapsed = (time.perf_counter() - t0) * 1000
        return {"ok": True, "elapsed_ms": round(elapsed), "data": out}
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")[:500]
            except Exception:
                pass
        return {"ok": False, "elapsed_ms": round(elapsed), "error": repr(e), "detail": detail}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    model = sys.argv[1] if len(sys.argv) > 1 else "glm-4.6v"
    # 1) 纯文本 ping
    r = chat(model, "只回复两个字：在线")
    print("== 文本测试 model=%s ==" % model)
    print("ok:", r["ok"], "耗时:", r.get("elapsed_ms"), "ms")
    if r["ok"]:
        print("回复:", r["data"]["choices"][0]["message"]["content"][:120])
        print("usage:", r["data"].get("usage"))
    else:
        print("错误:", r.get("error"))
        print("详情:", r.get("detail"))
    # 2) 小图视觉测试（用已捕获的 tk widget 图）
    import cv2
    p = "smoke/data/target_img/tk_widget.png"
    if os.path.exists(p):
        img = cv2.imread(p)
        okimg, buf = cv2.imencode(".png", img)
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        r2 = chat(model, "这张截图里的文字是什么？只输出文字内容。", b64)
        print("== 视觉测试 ==")
        print("ok:", r2["ok"], "耗时:", r2.get("elapsed_ms"), "ms")
        if r2["ok"]:
            print("回复:", r2["data"]["choices"][0]["message"]["content"][:200])
        else:
            print("错误:", r2.get("error"), r2.get("detail"))
