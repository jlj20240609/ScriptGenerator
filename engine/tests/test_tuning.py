# -*- coding: utf-8 -*-
"""M2-WP3 调优框架离线测试：证据重放决策、口径、网格搜索、留出集防泄漏。

不触屏：全部用构造的"候选证据"（分数/距离/邻居是否对上/真值是哪一个）驱动。
"""
import unittest

from engine import tuning as T


def _cand(score, dist, nearby=None, box=(10, 20, 100, 30)):
    return {"box": list(box), "score": score, "dist": dist, "nearby_ok": nearby}


class DecideTest(unittest.TestCase):
    """②a 决策重放：门槛过滤 + 与引擎共用的排序规则。"""

    def test_threshold_filters_everything(self):
        s = T.evidence_sample("s", "g", [_cand(0.70, 12)], truth_index=0)
        d = T.decide_text(s, dict(T.DEFAULT_PARAMS, text_sim_min=0.75))
        self.assertIsNone(d["chosen_index"])
        self.assertTrue(d["missed"], "门槛把唯一候选滤掉 → 如实算漏检")
        self.assertFalse(d["wrong"], "漏检不等于误报")

    def test_picks_truth_when_near_and_high_score(self):
        s = T.evidence_sample("s", "g", [_cand(0.95, 8), _cand(0.60, 300)],
                              truth_index=0, far_limit=160)
        d = T.decide_text(s, T.DEFAULT_PARAMS)
        self.assertEqual(d["chosen_index"], 0)
        self.assertEqual(d["source"], "hit")

    def test_nearby_beats_closer(self):
        """同页两个相同文字：邻居对得上的那个才算（用户审查要求的消歧优先）。"""
        cands = [_cand(0.90, 20, nearby=False), _cand(0.90, 40, nearby=True)]
        s = T.evidence_sample("s", "g", cands, truth_index=1, far_limit=160)
        self.assertEqual(T.decide_text(s, T.DEFAULT_PARAMS)["source"], "hit")
        # 关掉"邻居优先" → 变成按距离选，选中了错的 → 误报（这就是偏好顺序的作用面）
        off = dict(T.DEFAULT_PARAMS, prefer_nearby=False)
        d = T.decide_text(s, off)
        self.assertEqual(d["source"], "wrong_pick")
        self.assertTrue(d["wrong"])

    def test_negative_sample_strict_threshold_avoids_false(self):
        """负例（本来就没目标）：严格门槛能把它压掉，宽松门槛就会误点。"""
        s = T.evidence_sample("s", "g", [_cand(0.78, 10)], truth_index=None)
        self.assertFalse(T.decide_text(s, dict(T.DEFAULT_PARAMS, text_sim_min=0.80))["wrong"])
        d = T.decide_text(s, dict(T.DEFAULT_PARAMS, text_sim_min=0.75))
        self.assertTrue(d["wrong"])
        self.assertEqual(d["source"], "false_positive")

    def test_far_candidate_gate(self):
        s = T.evidence_sample("s", "g", [_cand(0.95, 500)], truth_index=0, far_limit=160)
        self.assertIsNone(T.decide_text(s, dict(T.DEFAULT_PARAMS,
                                                allow_far=False))["chosen_index"])
        self.assertEqual(T.decide_text(s, T.DEFAULT_PARAMS)["chosen_index"], 0)

    def test_far_limit_scale_moves_the_boundary(self):
        s = T.evidence_sample("s", "g", [_cand(0.95, 500)], truth_index=0, far_limit=160)
        # 距离上限压到 240 → 500 的候选不再可用
        tight = dict(T.DEFAULT_PARAMS, max_dist=240)
        self.assertIsNone(T.decide_text(s, tight)["chosen_index"])
        # 把"太远"界限放大 4 倍（640）→ 同样这个候选变成"近处候选"，哪怕 max_dist 很紧也能用
        wide = dict(T.DEFAULT_PARAMS, far_limit_scale=4.0, max_dist=240)
        self.assertEqual(T.decide_text(s, wide)["chosen_index"], 0)


class HasTargetTest(unittest.TestCase):
    """「页面上有没有目标」与「候选里有没有真值」必须分开算，否则命中率会虚高。"""

    def test_target_present_but_no_candidate_is_miss_not_false(self):
        s = T.evidence_sample("s", "g", [_cand(0.9, 300)], truth_index=None, has_target=True,
                              far_limit=160)
        d = T.decide_text(s, T.DEFAULT_PARAMS)
        self.assertTrue(d["missed"], "目标没被找到 = 漏检")
        self.assertTrue(d["wrong"], "但选中的是别的东西 = 误定位")
        r = T.evaluate([s], T.DEFAULT_PARAMS)
        self.assertEqual(r["pos"], 1, "有目标的样本要进命中率分母")
        self.assertEqual(r["hits"], 0)
        self.assertAlmostEqual(r["false_rate"], 1.0, places=4)

    def test_target_present_zero_cands_is_pure_miss(self):
        s = T.evidence_sample("s", "g", [], truth_index=None, has_target=True)
        r = T.evaluate([s], T.DEFAULT_PARAMS)
        self.assertEqual((r["pos"], r["negatives"]), (1, 0))
        self.assertEqual(r["misses"], 1)
        self.assertAlmostEqual(r["false_rate"], 0.0, places=4, msg="没找到不是误报")

    def test_negative_sample_not_in_hit_denominator(self):
        s = T.evidence_sample("s", "g", [_cand(0.9, 10)], truth_index=None)
        self.assertFalse(s["has_target"], "truth_index=None 默认就是负例")
        r = T.evaluate([s], T.DEFAULT_PARAMS)
        self.assertEqual(r["pos"], 0, "负例不进命中率分母")
        self.assertAlmostEqual(r["hit_rate"], 0.0, places=4)
        self.assertAlmostEqual(r["false_rate"], 1.0, places=4, msg="没目标却选了 = 误报")

    def test_legacy_evidence_without_has_target_field(self):
        """老证据（没有 has_target 字段）按"有真值就是有目标"推断，行为不变。"""
        legacy = {"id": "s", "group": "g", "cands": [_cand(0.9, 10)], "truth_index": 0,
                  "far_limit": 160.0}
        r = T.evaluate([legacy], T.DEFAULT_PARAMS)
        self.assertEqual((r["pos"], r["hits"]), (1, 1))


class EvaluateTest(unittest.TestCase):
    def test_rates(self):
        samples = [
            T.evidence_sample("a", "g1", [_cand(0.9, 10)], truth_index=0),            # 命中
            T.evidence_sample("b", "g1", [_cand(0.9, 10), _cand(0.95, 300)],
                              truth_index=0, far_limit=160),                          # 命中近处
            T.evidence_sample("c", "g1", [_cand(0.9, 10)], truth_index=None),         # 负例，会误报
            T.evidence_sample("d", "g1", [_cand(0.5, 10)], truth_index=0),            # 门槛滤掉 → 漏检
        ]
        r = T.evaluate(samples, T.DEFAULT_PARAMS)
        self.assertEqual((r["n"], r["pos"], r["negatives"]), (4, 3, 1))
        self.assertEqual(r["hits"], 2)
        self.assertEqual(r["wrongs"], 1, "负例被选中 = 误报")
        self.assertAlmostEqual(r["hit_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(r["false_rate"], 1 / 4, places=4)
        self.assertAlmostEqual(r["score"], 2 / 3 - T.FALSE_COST * 0.25, places=4)


class SearchTest(unittest.TestCase):
    def test_combos_count(self):
        space = {"a": [1, 2, 3], "b": [True, False]}
        self.assertEqual(len(list(T.combos(space))), 6)

    def test_search_finds_stricter_threshold(self):
        """证据构造成：门槛 0.80 恰好"全命中且零误报"，更松会误报、更严会漏检。"""
        samples = [
            T.evidence_sample("p1", "g1", [_cand(0.88, 10)], truth_index=0),
            T.evidence_sample("p2", "g1", [_cand(0.90, 12)], truth_index=0),
            T.evidence_sample("p3", "g2", [_cand(0.83, 14)], truth_index=0),
            T.evidence_sample("n1", "g2", [_cand(0.78, 10)], truth_index=None),
            T.evidence_sample("n2", "g3", [_cand(0.79, 10)], truth_index=None),
        ]
        res = T.search(samples, {"text_sim_min": [0.70, 0.75, 0.80, 0.85]},
                       base=dict(T.DEFAULT_PARAMS, max_dist=600, allow_far=True))
        best = res["best"]
        self.assertEqual(best["params"]["text_sim_min"], 0.80, best)
        self.assertAlmostEqual(best["hit_rate"], 1.0, places=4)
        self.assertAlmostEqual(best["false_rate"], 0.0, places=4)
        looser = next(r for r in res["rows"] if r["params"]["text_sim_min"] == 0.75)
        self.assertGreater(looser["false_rate"], 0.0, "松门槛应出现误报")
        stricker = next(r for r in res["rows"] if r["params"]["text_sim_min"] == 0.85)
        self.assertLess(stricker["hit_rate"], 1.0, "严门槛应出现漏检")


class SplitTest(unittest.TestCase):
    def test_no_group_leak(self):
        samples = [T.evidence_sample(f"s{i}", group=f"g{i // 3}", cands=[_cand(0.9, 10)],
                                     truth_index=0) for i in range(12)]
        train, test, hold = T.split_groups(samples, holdout_ratio=0.5, seed=1)
        self.assertEqual(len(hold), 2)
        self.assertFalse({s["group"] for s in train} & {s["group"] for s in test},
                         "训练/留出不得共享 group（否则过拟合会被当成泛化）")
        self.assertEqual(len(train) + len(test), len(samples))
        self.assertTrue(test, "留出集不能为空")

    def test_single_group_no_holdout(self):
        samples = [T.evidence_sample("s1", "only", [_cand(0.9, 10)], truth_index=0)]
        train, test, hold = T.split_groups(samples)
        self.assertEqual(hold, [])
        self.assertEqual(len(train), 1)
        self.assertEqual(test, [])


class ReportTest(unittest.TestCase):
    def test_table_rows(self):
        samples = [T.evidence_sample("s", "g", [_cand(0.9, 10)], truth_index=0)]
        res = T.search(samples, {"text_sim_min": [0.70, 0.80]})
        table = T.format_table(res["rows"], limit=5)
        self.assertEqual(len(table.splitlines()), 2 + min(5, len(res["rows"])))
        self.assertIn("text_sim_min=", table)

    def test_compare(self):
        before = {"hit_rate": 0.8, "false_rate": 0.1, "score": 0.4}
        after = {"hit_rate": 0.9, "false_rate": 0.02, "score": 0.82}
        c = T.compare(before, after)
        self.assertAlmostEqual(c["hit_rate"][2], 0.1, places=6)
        self.assertAlmostEqual(c["false_rate"][2], -0.08, places=6)


if __name__ == "__main__":
    unittest.main()
