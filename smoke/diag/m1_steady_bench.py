# -*- coding: utf-8 -*-
"""稳态基准 v3（含 ORT 干扰条件 + 偶发窗口检测）。"""
import sys
import time

sys.path.insert(0, ".")
import numpy as np

from engine import locator, matcher
from engine.tests import support as S

PX, PY = 300, 150


def scene(b):
    scr = S.mk_canvas(1500, 1050, (24, 24, 28))
    r = S.paste_scale(b, scr, PX, PY, 1.0)
    return scr, r


def main():
    login, lb = S.login_page()
    home, hb = S.home_page()
    sc, rect = scene(login)
    sc2, rect2 = scene(home)
    t1 = S.widget_target(login, S.page_spec_of(login), lb["login_btn"], text="登录")
    t2 = S.widget_target(home, S.page_spec_of(home), hb["menu"], text="库存查询")
    t3 = S.widget_target(home, S.page_spec_of(home), hb["welcome"], text="登录成功")
    hspec = S.page_spec_of(home)
    matcher.ocr_engine()(home[100:200, 0:400])
    items = [
        ("login btn locate", lambda: locator.locate_widget_on_screen(sc, rect, t1)),
        ("home menu locate", lambda: locator.locate_widget_on_screen(sc2, rect2, t2)),
        ("welcome exists", lambda: locator.locate_widget_on_screen(sc2, rect2, t3,
                                                                   exists=True)),
        ("page locate home", lambda: locator.locate_page(sc2, hspec)),
    ]
    for label, fn in items:
        fn()
    for round_i in range(3):
        print("-- round %d --" % round_i)
        for label, fn in items:
            t0 = time.perf_counter()
            r = fn()
            ms = (time.perf_counter() - t0) * 1000
            print("%-20s %6.0fms" % (label, ms), flush=True)


if __name__ == "__main__":
    main()
