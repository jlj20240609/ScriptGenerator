# -*- coding: utf-8 -*-
"""探针：找一个"整窗模板与特征都匹配不上、但仍含改名后菜单"的屏。

PageLostCalibTest 想测的是「页面找不到 → 语义粗圈 + 整窗重采集」这条恢复路径。
它原来的 fixture（erp_v2 暖色改版）在 M2 加上 ORB 特征兜底之后**已经能被找到**
（左上仅差 3px，是真命中），前置断言因此过期——但直接把断言删掉又会让这条路径
再也测不到。所以造一个版式完全不同的屏：既要"真的找不到"，又要"改名后的菜单在"，
这样恢复路径才走得通、也才有意义。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import locator  # noqa: E402
from engine.tests import support as S  # noqa: E402


def redesigned(scheme="dark"):
    def deco(d, img):
        if scheme == "dark":
            d.rectangle((0, 46, 1000, 640), fill=(22, 28, 40))
            for i, name in enumerate(["工作台", "订单管理", "库存中心", "我的审批"]):
                d.rectangle((40, 80 + i * 96, 940, 156 + i * 96), fill=(38, 46, 62))
                S.draw_text(img, 70, 98 + i * 96, name, size=28, fill=(226, 232, 240))
        else:                                   # 左右分栏版式
            d.rectangle((0, 46, 260, 640), fill=(240, 244, 250))
            for i, name in enumerate(["工作台", "订单管理", "库存中心", "我的审批"]):
                S.draw_text(img, 24, 90 + i * 70, name, size=24, fill=(40, 50, 70))
            d.rectangle((260, 46, 1000, 640), fill=(255, 255, 255))
            S.draw_text(img, 300, 90, "今日待办 3 项", size=30, fill=(40, 50, 70))
    return S.make_page(w=1000, h=640, bg=(18, 22, 32) if scheme == "dark" else (250, 251, 253),
                       header_text="示例 ERP · 新界面", deco=deco)


def main():
    v1, b1 = S.erp_v1_page()
    spec = S.page_spec_of(v1)
    for scheme in ("dark", "split"):
        page = redesigned(scheme)
        scr = S.mk_canvas(1500, 1050, (24, 24, 28))
        rect = S.paste_scale(page, scr, 300, 150, 1.0)
        r = locator.locate_page(scr, spec)
        d = r.get("detail") or {}
        ft = d.get("page_feature") or {}
        print(f"  {scheme:<6} ok={r['ok']!s:<5} 模板={(d.get('page_tpl') or {}).get('verdict')} "
              f"特征ok={ft.get('ok')} 内点={ft.get('inliers')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
