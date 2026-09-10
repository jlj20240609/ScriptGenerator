# -*- coding: utf-8 -*-
"""临时探针：填入内容到什么程度，整块模板才真的失配（决定测试构造）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import locator, matcher  # noqa: E402
from engine.tests import support as S  # noqa: E402

page, boxes = S.login_page()
spec = S.page_spec_of(page)
bx = boxes["pwd_box"]
print("输入框 box:", bx, "模板尺寸:", (bx[2], bx[3]), "环带:", matcher.ring_mask((bx[3], bx[2]))[1])
tpl = page[bx[1]:bx[1] + bx[3], bx[0]:bx[0] + bx[2]]


def score(case, draw):
    img = S.Image.fromarray(page[:, :, ::-1])
    draw(S.ImageDraw.Draw(img))
    live = S.pil_to_bgr(img)
    screen, rect = S.scene_of(live, 300, 150, 1.0, canvas_w=1500, canvas_h=1050)
    t = S.widget_target(page, spec, bx, text="")
    r = locator.locate_widget_on_screen(screen, rect, t)
    # 只看目标位置附近（同页还有另一个外观相同的输入框，不限定会撞到它）
    area = [rect[0] + bx[0] - 50, rect[1] + bx[1] - 50, bx[2] + 100, bx[3] + 100]
    whole = matcher.find_template(screen, tpl, scales=(1.0,), score_thr=0.0, search=area)
    ring = matcher.find_template_ring(screen, tpl, score_thr=0.0, search=area)
    print(f"{case:22s} 定位={r['ok']}/{r.get('method')}/level={r.get('level')} "
          f"整块score={whole['score']:.3f} 环带sim={ring['score']:.3f}")


score("空框（录制态）", lambda d: None)
score("填 short", lambda d: d.text((bx[0] + 24, bx[1] + 20), "wrong-pass",
                                 font=S.font(20), fill=(30, 30, 30)))
score("填 long", lambda d: d.text((bx[0] + 14, bx[1] + 16), "a" * 30,
                                font=S.font(24), fill=(30, 30, 30)))
score("填 long+掩码点", lambda d: (
    d.text((bx[0] + 10, bx[1] + 12), "••••••••••••••••••••", font=S.font(28),
           fill=(40, 40, 40)),))
score("整块涂黑", lambda d: d.rectangle((bx[0] + 4, bx[1] + 4, bx[0] + bx[2] - 4,
                                     bx[1] + bx[3] - 4), fill=(25, 25, 25)))
