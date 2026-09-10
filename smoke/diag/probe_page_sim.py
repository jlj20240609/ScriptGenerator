# -*- coding: utf-8 -*-
"""定阈值：量出"同一页轻微改动"与"完全不同的页"各自的同源相似度。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import matcher  # noqa: E402
from engine.tests import support as S  # noqa: E402


def sim(a, b):
    return matcher.pixel_sim(a, b, size=(240, 150), thr=16.0)


login, _ = S.login_page()
home, _ = S.home_page()
light = login.copy()
light[520:560, 200:600] = (150, 150, 150)        # 页脚改色（轻微改版）
mid = login.copy()
mid[300:420, 150:560] = (235, 235, 235)          # 中部一块改色（中等改版）

print("同页-完全相同      :", round(sim(login, login), 4))
print("同页-轻微改版(页脚) :", round(sim(login, light), 4))
print("同页-中等改版(中部) :", round(sim(login, mid), 4))
print("异页-login vs home  :", round(sim(login, home), 4))
print("异页-home vs login  :", round(sim(home, login), 4))
blank, _ = S.login_page()
blank = blank.copy()
blank[:] = 250
print("异页-纯白 vs login  :", round(sim(blank, login), 4))

print("\n—— 配套的整窗模板分数（同源确认前的那道门）——")


def tpl_score(screen, tpl):
    return round(matcher.find_template(screen, tpl, scales=(1.0,),
                                       score_thr=0.0)["best_score"], 4)


print("同页-完全相同      :", tpl_score(login, login))
print("同页-轻微改版(页脚) :", tpl_score(light, login))
print("同页-中等改版(中部) :", tpl_score(mid, login))
print("异页-home          :", tpl_score(home, login))
print("异页-纯白          :", tpl_score(blank, login))
