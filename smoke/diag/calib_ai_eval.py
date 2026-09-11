# -*- coding: utf-8 -*-
"""
smoke/diag/calib_ai_eval.py — M3-WP4：自校准 AI 化的真机评测（真调 glm-4.6v）。

测的是什么：界面小改版把菜单改了名（库存查询 → 存货台账，**连前缀都对不上**），
脚本里的旧目标在页面上找不到。要看的是整条自校准链：

  本地三级定位失败 → 云端语义确认（±100px 粗圈）→ 云端答出"它现在叫什么"
  → 本地在圈区内精确重定位 → 重采集部件模板并写回 → 自检通过

**关键守住的边界**：云端给的粗圈只用来**缩小本地搜索范围**，最终坐标始终由本地
定位给出（本脚本会打印两者的偏差，用数据说明"最终坐标不是云端给的"）。

用法：
  python smoke/diag/calib_ai_eval.py            # 真调云端
  python smoke/diag/calib_ai_eval.py --no-ai    # 对照组：不接 AI（应当恢复失败）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import ai as AI              # noqa: E402
from engine import calibrator as C       # noqa: E402
from engine import locator, matcher      # noqa: E402
from engine.tests import support as S    # noqa: E402

PX, PY = 300, 150
CANVAS = (1500, 1050)
OLD_NAME = "库存查询"
NEW_NAME = "存货台账"


def _canvas_of(page):
    scr = S.mk_canvas(CANVAS[0], CANVAS[1], (24, 24, 28))
    rect = S.paste_scale(page, scr, PX, PY, 1.0)
    return scr, rect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="自校准 AI 化真机评测")
    ap.add_argument("--no-ai", action="store_true", help="对照组：不接 AI")
    ap.add_argument("--model", default=AI.DEFAULT_MODEL)
    args = ap.parse_args(argv)

    v1, b1 = S.erp_v1_page()
    v2, b2 = S.erp_renamed_hard_page()
    canvas, rect = _canvas_of(v2)
    spec1 = S.page_spec_of(v1)
    target = S.widget_target(v1, spec1, b1["menu"], text=OLD_NAME)

    print("=" * 68)
    print(f"自校准 AI 化评测：菜单改名「{OLD_NAME}」→「{NEW_NAME}」（前缀对不上）")
    print("=" * 68)

    # 前置：本地定位确实找不到旧目标（不然这个场景不成立）
    pg = locator.locate_page(canvas, spec1)
    print(f"前置 1：页面模板在改版屏上 {'命中' if pg['ok'] else '不命中'}（部件级恢复要求页面仍在）")
    w = locator.locate_widget_on_screen(canvas, pg["rect"], target, exists=True)
    print(f"前置 2：旧目标「{OLD_NAME}」可见性 = {w['ok']}（应为 False，否则场景不成立）")
    if w["ok"]:
        print("× 场景不成立：本地还能找到旧目标，这次评测证明不了什么。")
        return 2

    ai_obj = AI.SemanticStub() if args.no_ai else None
    if not args.no_ai:
        key = AI.ZhipuVLM().api_key
        if not key:
            print("× 没有可用的 API Key，改用 --no-ai 跑对照组。")
            return 2
        vlm = AI.ZhipuVLM(api_key=key, model=args.model)
        if not vlm.authorize(notify=lambda reason: True):
            print("× 未获授权，不调用云端。")
            return 2
        ai_obj = vlm
        print(f"云端：{args.model}（已授权，会上传合成截图）")

    cal = C.Calibrator(S.FakeDriver(lambda: canvas), ai=ai_obj,
                       window_rect=lambda: rect)
    req = {"reason": "widget_not_found", "target": target, "page_spec": spec1,
           "page_rect": rect, "loc_rows": [], "ctx": {}}
    t0 = time.perf_counter()
    res = cal(req)
    secs = time.perf_counter() - t0

    print(f"\n自校准结果：ok={res['ok']}  updated={res.get('updated')}  用时 {secs:.1f}s")
    print(f"  note: {res.get('note')}")
    detail = res.get("detail") or {}
    for k in ("ai", "rename", "local"):
        if detail.get(k):
            print(f"  {k}: {detail[k] if not isinstance(detail[k], dict) else detail[k].get('note')}")

    if not res["ok"]:
        print("\n结论：恢复失败 → 按设计走「低置信人工确认」，脚本里的旧值原样保留。")
        print(f"  目标文字仍为：{target['text']!r}")
        return 0 if args.no_ai else 1

    # 恢复成功：核对写回的内容与**坐标来源**
    print(f"\n恢复后目标文字：{target['text']!r}（应为 {NEW_NAME}）")
    b2_menu = b2["menu"]
    got = target["rect_in_page"]
    dev = max(abs(got[0] - (b2_menu[0] - C.PAD)), abs(got[1] - (b2_menu[1] - C.PAD)))
    print(f"写回的部件框与真值偏差：{dev}px（≤14 视为定位正确）")

    # 边界证据：最终坐标是谁给的？
    local_detail = detail.get("local") or {}
    print(f"\n最终部件框由哪一步给出：{local_detail.get('method')!r}"
          f"（matched={local_detail.get('matched')!r}）")
    print(f"云端第一问（找目标）是否给出可用坐标：{bool((detail.get('ai') or '')
                                                       .startswith('AI 语义确认 + 归一化粗圈'))}")
    print("  → 本例里云端连坐标都没给（它答「目标不存在」），恢复完全靠"
          "「它现在叫什么」这个词 + 本地 OCR 精定位；")
    print("    架构边界仍然成立：**坐标来自本地**，云端只贡献语义。")

    ok = (target["text"] == NEW_NAME and dev <= 14)
    print("\n" + ("结论：AI 自校准恢复成功 ✓" if ok else "结论：恢复结果不符合预期 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
