# -*- coding: utf-8 -*-
"""
engine.tuning — M2-WP3：融合参数调优（证据重放 + 网格搜索 + 留出集复验）。

为什么这么做（设计取舍）：
  阈值调优必须能"换一组参数重算同一批数据"，否则每次试参数都要重新跑真机。
  所以本模块不重跑图像，而是**重放证据**：每条证据 = 一次定位里"有哪些候选、各自
  多少分、离录点多远、邻居对不对上、真值是哪一个"。生成一次证据，之后可以离线试
  任意多组参数——真机跑批（bench --dump-evidence）与合成场景产出的证据是同一格式。

  决策规则本身**不在这里另写一份**：与 engine.locator 共用 rank_text_candidates，
  免得"调参器算出来的最优参数在引擎里表现不一样"。

口径（M2 DoD）：命中 = 选中了真值候选；**误报** = 没命中却选中了别的候选
（宁可失败也不误点 → 目标函数对误报的惩罚远大于漏检）。
"""
from __future__ import annotations

import random
from itertools import product

from engine.locator import rank_text_candidates

# 目标函数里的代价（用户口径"宁可失败也不误点"）：误报的代价是漏检的若干倍
FALSE_COST = 4.0


# ---------------------------------------------------------------- 证据

def evidence_sample(sample_id: str, group: str, cands: list, truth_index=None,
                    anchor_xy=None, far_limit=160, difficulty="", has_target=None) -> dict:
    """一条定位证据。

    cands: [{box, score, dist, nearby_ok, ...}]（box 为页内矩形；dist 离录点距离）
    truth_index: 真值候选在 cands 里的下标。
    has_target: **页面上到底有没有这个目标**（与"候选里有没有真值"是两件事，必须分开）：
      - has_target=True, truth_index=i → 应该选中 i（选中别的 = 误定位）；
      - has_target=True, truth_index=None → 目标在，但候选里没有它（= 漏检，**不是误报**）；
      - has_target=False → 页面上本来就没目标（= 负例，此时选中任何候选都是误报）。
      默认按 truth_index 推断（有真值就是有目标），但正例"一个候选都没对上"必须显式传 True。
    far_limit: 引擎按录制模板尺寸算出的"太远"界限（不是自由参数；调参只调它的**倍数**）。
    group: 场景/脚本名——留出集按 group 划分，防止同一场景既训练又测试（防过拟合）。
    """
    if has_target is None:
        has_target = truth_index is not None
    return {"id": sample_id, "group": group, "difficulty": difficulty,
            "anchor_xy": list(anchor_xy or []), "cands": list(cands or []),
            "truth_index": truth_index, "has_target": bool(has_target),
            "far_limit": float(far_limit)}


# ---------------------------------------------------------------- 参数与空间

DEFAULT_PARAMS = {
    "text_sim_min": 0.75,       # ②a 文字相似度门槛
    "tpl_score_min": 0.70,      # ②b 部件模板门槛
    "ring_score_min": 0.60,     # ②c 环带模板门槛
    "allow_far": True,          # 近处全无命中时，是否允许用"太远"的兜底候选
    "prefer_nearby": True,      # 邻居对得上的候选是否优先
    "far_limit_scale": 1.0,     # "太远"界限的倍数（引擎原值 × 本倍数）
    "max_dist": 600,            # 兜底候选的距离上限（再远就当没找到）
}

PARAM_SPACE = {
    "text_sim_min": [0.70, 0.75, 0.80, 0.85],
    "prefer_nearby": [True, False],
    "allow_far": [True, False],
    "max_dist": [240, 360, 600, 1000],
}


def combos(space: dict):
    """参数网格 → 组合列表（dict）。"""
    keys = list(space.keys())
    for vals in product(*(space[k] for k in keys)):
        yield dict(zip(keys, vals))


# ---------------------------------------------------------------- 决策（重放）

def decide_text(sample: dict, params: dict) -> dict:
    """用一组参数在**一条证据**上做②a决策。

    返回 {chosen_index, wrong（是否误报）, missed（是否漏检）, source}
    - 门槛：分数 < text_sim_min 的候选直接不算候选（这正是阈值调优的作用面）；
    - 排序：与引擎共用 rank_text_candidates；
    - 太远的候选只有在 allow_far 且距离 ≤ max_dist 时才当兜底。
    """
    thr = params.get("text_sim_min", DEFAULT_PARAMS["text_sim_min"])
    max_dist = params.get("max_dist", DEFAULT_PARAMS["max_dist"])
    base_far = float(sample.get("far_limit") or 160)
    far_limit = base_far * float(params.get("far_limit_scale", 1.0))
    indexed = [(i, c) for i, c in enumerate(sample.get("cands") or [])
               if c.get("score", 0.0) >= thr]
    near, far = rank_text_candidates([c for _, c in indexed], far_limit)
    if not params.get("prefer_nearby", True):
        near = sorted(near, key=lambda c: (c.get("dist", 0), -c.get("score", 0.0)))
    pool = list(near)
    if params.get("allow_far", True):
        pool += [c for c in far if c.get("dist", 0) <= max_dist]
    truth = sample.get("truth_index")
    if not pool:
        # 一个候选都没有：目标在页面上就是**漏检**；页面上本来就没目标则是正确行为
        return {"chosen_index": None, "wrong": False, "missed": _has_target(sample),
                "source": "none"}
    chosen = pool[0]
    idx = next((i for i, c in indexed if c is chosen), None)
    if truth is None:
        if not _has_target(sample):
            # 负例：本来就没目标，此时选中任何候选都是**误报**（比漏检严重得多）
            return {"chosen_index": idx, "wrong": True, "missed": False,
                    "source": "false_positive"}
        # 目标在页面上，但真值候选没被找出来 → 选中的是别的东西 = 误定位
        return {"chosen_index": idx, "wrong": True, "missed": True,
                "source": "wrong_pick"}
    if idx == truth:
        return {"chosen_index": idx, "wrong": False, "missed": False, "source": "hit"}
    return {"chosen_index": idx, "wrong": True, "missed": True, "source": "wrong_pick"}


def _has_target(sample: dict) -> bool:
    """页面上有没有目标（老证据没有该字段时按"有真值就是有目标"推断）。"""
    ht = sample.get("has_target")
    return bool(ht) if ht is not None else sample.get("truth_index") is not None


def evaluate(samples: list, params: dict) -> dict:
    """一组参数在一批证据上的表现（调参的目标函数来源）。"""
    n = len(samples)
    hits = wrongs = misses = negatives = 0
    for s in samples:
        d = decide_text(s, params)
        if not _has_target(s):
            negatives += 1
            wrongs += int(d["wrong"])
            continue
        if d["source"] == "hit":
            hits += 1
        elif d["source"] == "none":
            # 门槛把候选全滤掉了：**漏检**，绝不能算命中（这类错会让调参一路偏向"放宽阈值"）
            misses += 1
        else:                                   # wrong_pick：选错了，既误报也漏检
            wrongs += 1
            misses += 1
    pos = n - negatives                        # 命中率分母只含"页面上真有目标"的样本
    hit_rate = hits / pos if pos else 0.0
    false_rate = wrongs / n if n else 0.0
    # 目标函数：命中率 − 误报惩罚（误报的代价远高于漏检）
    score = hit_rate - FALSE_COST * false_rate
    return {"n": n, "pos": pos, "negatives": negatives, "hits": hits, "wrongs": wrongs,
            "misses": misses, "hit_rate": hit_rate, "false_rate": false_rate,
            "score": score, "params": dict(params)}


# ---------------------------------------------------------------- 搜索与复验

def search(samples: list, space: dict = None, base: dict = None) -> dict:
    """网格搜索：返回 {best, rows}（rows 按目标函数降序）。"""
    space = space or PARAM_SPACE
    base = dict(base or DEFAULT_PARAMS)
    rows = []
    for c in combos(space):
        p = dict(base, **c)
        r = evaluate(samples, p)
        r["grid"] = c
        rows.append(r)
    rows.sort(key=lambda r: (-r["score"], -r["hit_rate"], r["false_rate"]))
    return {"best": rows[0] if rows else None, "rows": rows}


def split_groups(samples: list, holdout_ratio=0.4, seed=7):
    """按 **group**（场景/脚本）划分训练集与留出集。

    按 group 而不是按样本随机切：同一场景的样本高度相似，随机切会让留出集"见过"训练
    场景，把过拟合当成泛化（WP3 备注里点名要防的坑）。
    """
    groups = sorted({s.get("group") or s.get("id") or "" for s in samples})
    rng = random.Random(seed)
    rng.shuffle(groups)
    n_hold = max(1, int(round(len(groups) * holdout_ratio))) if len(groups) > 1 else 0
    hold = set(groups[:n_hold])
    train = [s for s in samples if (s.get("group") or s.get("id")) not in hold]
    test = [s for s in samples if (s.get("group") or s.get("id")) in hold]
    return train, test, sorted(hold)


def compare(before: dict, after: dict) -> dict:
    """调优前后对比（报告用）。"""
    return {
        "hit_rate": (before["hit_rate"], after["hit_rate"], after["hit_rate"] - before["hit_rate"]),
        "false_rate": (before["false_rate"], after["false_rate"],
                       after["false_rate"] - before["false_rate"]),
        "score": (before["score"], after["score"], after["score"] - before["score"]),
    }


def format_table(rows: list, limit=12) -> str:
    """网格搜索结果 → Markdown 表。"""
    lines = ["| # | 参数 | 命中率 | 误报率 | 目标值 |", "|---|---|---|---|---|"]
    for i, r in enumerate(rows[:limit], 1):
        ps = ", ".join(f"{k}={v}" for k, v in sorted((r.get("grid") or {}).items()))
        lines.append(f"| {i} | {ps} | {r['hit_rate']:.3f} | {r['false_rate']:.3f} "
                     f"| {r['score']:.3f} |")
    return "\n".join(lines)
