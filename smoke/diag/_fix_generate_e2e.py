# -*- coding: utf-8 -*-
"""给 generate_e2e.py 做三处修正（都是我自己的脚手架问题，不是引擎的）：

1. run_script 必须跑在**后台线程**、主线程继续 pump Tk —— 否则 Tk 不处理事件，
   点击/按键的效果永远不发生（这个坑在 e2e_replay.py 里已经记过一次，这次又犯了）；
2. 用**有界的 RunConfig**（重试次数与超时都限死）——裸用默认值遇到定位不到时会热循环，
   实测烧了 86 秒 CPU 还不出来；
3. 生成的骨架落盘缓存，重跑验证时用 --reuse 直接复用，不必每次都花云端调用。
"""
from pathlib import Path

p = Path("smoke/diag/generate_e2e.py")
src = p.read_text(encoding="utf-8")

# 1) 有界 RunConfig + 后台线程执行
src = src.replace(
    '        driver = X.LiveDriver({"hwnd": hwnd})\n'
    '        rep = X.run_script(run_sg, driver, cfg=X.RunConfig(), loc_logger=MemoryLogger(),\n'
    '                           human=None)',
    '        driver = X.LiveDriver({"hwnd": hwnd})\n'
    '        # 重试与超时都限死：裸用默认值遇到定位不到会热循环（实测烧了 86s CPU）\n'
    '        cfg = X.RunConfig(l1_retries=1, l1_retry_interval_s=0.05,\n'
    '                          l2_poll_interval_s=0.2, l2_timeout_s=3.0, guard=True)\n'
    '        # 跑在后台线程、主线程继续 pump Tk：否则 Tk 不处理事件，点击与按键\n'
    '        # 的效果永远不会发生（这个坑在 e2e_replay.py 里记过一次）\n'
    '        box = {}\n\n'
    '        def _run():\n'
    '            try:\n'
    '                box["rep"] = X.run_script(run_sg, driver, cfg=cfg,\n'
    '                                          loc_logger=MemoryLogger(), human=None)\n'
    '            except Exception as e:\n'
    '                box["err"] = repr(e)\n\n'
    '        th = threading.Thread(target=_run, daemon=True)\n'
    '        th.start()\n'
    '        t0 = time.time()\n'
    '        while th.is_alive() and time.time() - t0 < 25:\n'
    '            pump(root, 0.15)\n'
    '        pump(root, 0.8)\n'
    '        rep = box.get("rep") or {"status": "timeout", "steps": [],\n'
    '                                "error": box.get("err") or "真跑超时（25s）"}')

# 2) 骨架缓存：--reuse 时不再调云端
src = src.replace(
    '    res = AG.generate(args.sentence, vlm)',
    '    cache = ROOT / "smoke" / "rec" / "generated_skeleton.json"\n'
    '    if args.reuse and cache.exists():\n'
    '        import json as _json\n'
    '        sg_cached = _json.loads(cache.read_text(encoding="utf-8"))\n'
    '        res = {"ok": True, "script": sg_cached, "notes": ["（复用上次生成的骨架，未调云端）"],\n'
    '               "pending": [t for t in AF.pending_texts(sg_cached)], "usage": {}}\n'
    '    else:\n'
    '        res = AG.generate(args.sentence, vlm)')
src = src.replace(
    '    sg = res["script"]\n    print("  生成的动作：")',
    '    sg = res["script"]\n'
    '    try:\n'
    '        import json as _json\n'
    '        cache.parent.mkdir(parents=True, exist_ok=True)\n'
    '        cache.write_text(_json.dumps(sg, ensure_ascii=False, indent=1), encoding="utf-8")\n'
    '    except Exception:\n'
    '        pass\n'
    '    print("  生成的动作：")')
src = src.replace('    ap.add_argument("--no-run", action="store_true"',
                  '    ap.add_argument("--reuse", action="store_true",\n'
                  '                    help="复用上次生成的骨架（不调云端）")\n'
                  '    ap.add_argument("--no-run", action="store_true"')
src = src.replace("import sys\nimport time", "import sys\nimport threading\nimport time")

p.write_text(src, encoding="utf-8")
print("已修正 generate_e2e.py")
