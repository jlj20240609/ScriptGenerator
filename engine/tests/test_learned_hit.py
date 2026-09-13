# -*- coding: utf-8 -*-
"""坐标固化与自动降级（2026-09-14，用户需求）。

需求原文：脚本第一次运行用图像识别（校对模式），通过后把点击位置按**相对操作页面**的坐标
记下来；之后多次运行直接按坐标点，直到"未能得到目标结果"再回到校对模式。

信任必须有边界，这组用例把边界钉住：

  1. 第一次运行（校对）：第 2 层认出来的位置被记进 `target.learned_hit`（页内坐标）；
  2. 第二次运行：直接按坐标点（`method=learned`），不再付大图 OCR 的钱；
  3. 那个位置已经不是目标了（点击前校验不过）→ **当场**回校对，并丢掉失效记录；
  4. "做完后应该看到 X"没出现 → 撤销这次学到的位置（用户说的"没能得到目标结果"）；
  5. 页面尺寸变了 → 旧坐标不当数（按校对模式跑）。
"""
import unittest

from engine import executor as X
from engine import matcher
from engine.logger import MemoryLogger
from engine.tests import support as S

CANVAS = (1500, 1050)
PX, PY = 300, 150


class Env:
    """一页（登录页）+ 替身驱动；页面可以整体替换，用来模拟"目标挪走了"。"""

    def __init__(self, guard=True):
        self.page, self.boxes = S.login_page()
        self.scene = S.StatefulScene({"main": (self.page, self.boxes)},
                                     {"main": (PX, PY, 1.0)}, canvas=CANVAS, initial="main")
        self.driver = S.FakeDriver(self.scene.provider)
        self.human = S.FakeHuman()
        self.cfg = X.RunConfig(l1_retries=0, l1_retry_interval_s=0.01,
                               l2_poll_interval_s=0.05, l2_timeout_s=1.0, guard=guard)
        self.logger = None

    def target(self, key="login_btn", text="登录"):
        spec = S.page_spec_of(self.page,
                              rect=(PX, PY, self.page.shape[1], self.page.shape[0]))
        return S.widget_target(self.page, spec, self.boxes[key], text=text)

    def run(self, sg, judge=None):
        self.logger = MemoryLogger()
        return X.run_script(sg, self.driver, cfg=self.cfg, loc_logger=self.logger,
                            human=self.human, calibrator=None, stop_event=None, judge=judge)

    def methods(self, event):
        return [r.get("method") for r in self.logger.tail(None, 1000)
                if r.get("event") == event]

    def events(self):
        return [r.get("event") for r in self.logger.tail(None, 1000)]

    def move_button(self, dy=100):
        """把登录按钮整块往下挪（页面其它部分不变）——模拟"目标挪走了"。

        位移要足够大才算"失效"：点击前校验是以部件框为中心外扩一小圈的（横向 ±160、
        纵向 ±60 的量级），挪几十像素它仍会认为"还是那个目标"（这是有意的容忍度，
        见 executor._click_guard）。这里挪 100px，超出容忍圈，也超出模板"录点附近"
        的搜索窗口 —— 正好把"坐标失效 → 回校对"这条路走一遍。
        """
        page, boxes = S.login_page()
        img = S.Image.fromarray(page[:, :, ::-1])
        bx, by, bw, bh = boxes["login_btn"]
        d = S.ImageDraw.Draw(img)
        d.rectangle((120, 460, 380, 524), fill=(255, 255, 255))       # 擦掉原来的按钮块
        ny = by + dy
        d.rectangle((120, ny - 17, 380, ny + 47), fill=(228, 233, 242),
                    outline=(120, 130, 150))
        boxes["login_btn"] = S.draw_text(img, bx, ny, "登录", size=24, fill=(30, 60, 130))
        new_page = S.pil_to_bgr(img)
        self.scene.pages["main"] = (new_page, boxes)
        self.page, self.boxes = new_page, boxes
        return boxes["login_btn"]


class LearnedHitTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.tgt = self.env.target()

    def _script(self, with_outcome=False):
        st = S.action_step("s1", "click", target=self.tgt)
        if with_outcome:
            # "做完后应该看到 X" —— 这里给一个页面上根本不存在的 X
            st["expected_outcome"] = {
                "target": {"page": self.tgt["page"], "text": "绝无此物XYZ",
                           "match": "text_first"},
                "on_fail": {"strategy": "notify", "message": "没看到预期结果"}}
        return S.script("坐标固化用例", [st])

    def test_first_run_learns_page_coordinates(self):
        sg = self._script()
        rep = self.env.run(sg)
        self.assertEqual(rep["status"], "ok", rep)
        rec = self.tgt.get("learned_hit")
        self.assertIsNotNone(rec, "校对成功后应当把位置记下来")
        bx = self.env.boxes["login_btn"]
        dev = max(abs(rec["rect_in_page"][0] - bx[0]), abs(rec["rect_in_page"][1] - bx[1]))
        self.assertLessEqual(dev, 8, f"记下来的应当是识别到的位置：{rec} vs {bx}")
        self.assertEqual(rec["page_size"], [self.env.page.shape[1], self.env.page.shape[0]])
        self.assertGreaterEqual(rec["hits"], 1)
        self.assertEqual(sg["targets_rev"], 1, "学到位了要让上层知道脚本该重存")
        self.assertTrue(any(e["what"] == "learn" for e in rep.get("learned", [])),
                        f"应当有一条「学到了」的轨迹：{rep.get('learned')}")
        # 第一次必须真的识别过（这就是"校对模式"）
        self.assertIn(self.env.methods("locate_widget")[0],
                      ("tpl", "ocr_text", "tpl_ring", "uia"))

    def test_second_run_uses_coordinates_without_big_ocr(self):
        sg = self._script()
        self.env.run(sg)                      # 第一次：校对
        self.assertIsNotNone(self.tgt.get("learned_hit"))
        big_px = {"first": 0}
        orig = matcher.ocr_run

        def spy(bgr, *a, **kw):
            r = orig(bgr, *a, **kw)
            if not r.get("cached") and bgr.shape[0] * bgr.shape[1] > 100000:
                big_px["run"] = big_px.get("run", 0) + bgr.shape[0] * bgr.shape[1]
            return r

        matcher.ocr_run = spy
        try:
            big_px["run"] = 0
            rep2 = self.env.run(sg)           # 第二次：按坐标跑
        finally:
            matcher.ocr_run = orig
        self.assertEqual(rep2["status"], "ok", rep2)
        self.assertEqual(self.env.methods("locate_widget")[-1], X.M_LEARNED,
                         f"第二次应当直接按记下来的位置点：{self.env.methods('locate_widget')}")
        self.assertEqual(big_px["run"], 0,
                         "按坐标跑时不该再扫整页/大条带（省下的就是这笔钱）")

    def test_stale_coordinates_fall_back_to_calibration(self):
        sg = self._script()
        self.env.run(sg)
        rec0 = self.tgt.get("learned_hit")
        self.assertIsNotNone(rec0)
        new_box = self.env.move_button(dy=100)         # 目标挪走了 → 旧坐标不再是目标
        rep2 = self.env.run(sg)
        self.assertEqual(rep2["status"], "ok", rep2)
        self.assertIn("learned_stale", self.env.events(),
                      f"旧坐标校验不过要留痕：{self.env.events()}")
        self.assertIn("tpl", self.env.methods("locate_widget"),
                      f"失效后当场回到校对模式：{self.env.methods('locate_widget')}")
        rec1 = self.tgt.get("learned_hit")
        self.assertIsNotNone(rec1, "校对成功后应当记下新位置")
        self.assertNotEqual(rec1["rect_in_page"], rec0["rect_in_page"],
                            "重新校对后位置应当更新到新地方")
        self.assertLessEqual(abs(rec1["rect_in_page"][1] - new_box[1]), 8, rec1)
        self.assertTrue(any(str(e["what"]).startswith("forget:")
                            for e in rep2.get("learned", [])),
                        f"失效应当有「忘掉」的轨迹：{rep2.get('learned')}")

    def test_outcome_failure_forgets_the_learned_position(self):
        sg = self._script(with_outcome=True)
        rep = self.env.run(sg)
        self.assertIsNotNone(rep)
        self.assertNotIn("learned_hit", self.tgt,
                         "预期结果没出现 → 这次学的位置不可信，应当撤销")
        whats = [e["what"] for e in rep.get("learned", [])]
        self.assertTrue(any(w.startswith("forget:") for w in whats),
                        f"应当有 forget 轨迹：{whats}")

    def test_page_size_change_ignores_learned(self):
        sg = self._script()
        self.tgt["learned_hit"] = {
            "rect_in_page": [100, 100, 40, 20], "center_in_page": [120, 110],
            "page_size": [999, 999], "page_scale": 1.0,
            "method": "tpl", "confidence": 1.0, "hits": 3, "source": "auto"}
        rep = self.env.run(sg)
        self.assertEqual(rep["status"], "ok", rep)
        self.assertNotEqual(self.env.methods("locate_widget")[-1], X.M_LEARNED,
                            "页面尺寸变了就不能再按旧坐标点")
        self.assertTrue(any(e["what"] == "page_size_changed"
                            for e in rep.get("learned", [])),
                        f"要能看出「为什么没用旧坐标」：{rep.get('learned')}")


if __name__ == "__main__":
    unittest.main()
