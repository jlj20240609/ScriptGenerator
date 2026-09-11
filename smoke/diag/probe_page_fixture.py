# -*- coding: utf-8 -*-
"""找一个"v1 页面模板真的找不到"的屏，给 PageLostCalibTest 当合理的前置。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import locator  # noqa: E402
from engine.tests import support as S  # noqa: E402

v1, b1 = S.erp_v1_page()
spec = S.page_spec_of(v1)
print("v1 页尺寸:", v1.shape[:2])

def _bgr(x):
    return x[0] if isinstance(x, tuple) else x


cands = {
    "home_page": _bgr(S.home_page()),
    "login_page": _bgr(S.login_page()),
    "erp_v2(暖色改版)": _bgr(S.erp_v2_page()),
}
for name, page in cands.items():
    scr = S.mk_canvas(1500, 1050, (24, 24, 28))
    rect = S.paste_scale(page, scr, 300, 150, 1.0)
    r = locator.locate_page(scr, spec)
    d = r.get("detail") or {}
    tpl = (d.get("page_tpl") or {}).get("verdict")
    ft = (d.get("page_feature") or {})
    print(f"  {name:<16} ok={r['ok']!s:<5} 模板={tpl} 特征ok={ft.get('ok')} "
          f"内点={ft.get('inliers')}")
