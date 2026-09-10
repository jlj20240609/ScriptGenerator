# -*- coding: utf-8 -*-
"""
engine/scripts/tune.py — M2-WP3：融合参数调优（证据 → 网格搜索 → 留出集复验 → 参数表）。

用法：
  python engine/scripts/tune.py                 # 用合成场景生成证据并调参
  python engine/scripts/tune.py --save-evidence # 顺便把证据落盘（可复跑、可与真机证据合并）
  python engine/scripts/tune.py --evidence engine/scripts/tune_evidence.jsonl  # 用已有证据（含真机）

产出：
  docs/M2_融合参数表.md      —— 每个参数的取值依据 + 调优前后对比（WP3 要求的交付物）
  engine/scripts/tune_evidence.jsonl（--save-evidence 时）

为什么是"证据重放"而不是"重跑图像"：见 engine/tuning.py 模块说明。一句话——换参数不必
重跑真机；录制/跑批时把候选证据（分数/距离/邻居/真值）存一次，之后离线试任意多组参数。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import locator, matcher  # noqa: E402
from engine.locator import LocConfig  # noqa: E402
from engine.tests import support as S  # noqa: E402
from engine.tuning import (DEFAULT_PARAMS, PARAM_SPACE, compare, decide_text,  # noqa: E402
                           evaluate, evidence_sample, format_table, search, split_groups)

EVIDENCE = ROOT / "engine" / "scripts" / "tune_evidence.jsonl"
DOC = ROOT / "docs" / "M2_融合参数表.md"
# 证据里的候选留痕要多收几个（默认只记 3 个"够审查用"，调参需要更全的候选面）
EVIDENCE_TOP_N = 8
TRUTH_TOL_PX = 12          # 候选中心离录制框中心多近算"这就是真值"


# ---------------------------------------------------------------- 合成场景

def _dup_query_page(rows=("物料编码", "供应商"), w=900, h=620):
    """同页两个一模一样的"查询"，各配不同的邻居文字；两行挨得够近（落在同一条搜索带里）。

    行序可由 rows 决定：录制用 (A,B)、运行用 (B,A) 就能造出"页面重排后，真值离录点更远、
    但邻居对得上"的局面——这正是"邻居优先"这条规则的用武之地。
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (252, 252, 254))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 44), fill=(226, 232, 240))
    bx = {"title": S.draw_text(img, 20, 10, "库存管理", size=20, fill=(30, 50, 80))}
    for i, label in enumerate(rows):
        y = (120, 185)[i]
        bx[f"label{i}"] = S.draw_text(img, 40, y, label, size=20, fill=(70, 70, 70))
        bx[f"query{i}"] = S.draw_text(img, 700, y, "查询", size=20, fill=(20, 60, 120))
    return S.pil_to_bgr(img), bx


def _target(page_bgr, spec, box, text, nearby_boxes=None):
    """手工造 target（含邻居文字记录，用于验证"邻居优先"这类消歧约束）。"""
    x, y, w_, h_ = [int(v) for v in box]
    crop = page_bgr[y:y + h_, x:x + w_]
    t = {"page": spec, "image": matcher.bgr_to_dataurl(crop), "text": text,
         "match": "auto", "rect_in_page": [x, y, w_, h_],
         "center_in_page": [x + w_ // 2, y + h_ // 2]}
    if nearby_boxes:
        cx, cy = x + w_ // 2, y + h_ // 2
        t["nearby"] = [{"text": nb[4], "rect_in_page": list(nb[:4]),
                        "offset": [nb[0] + nb[2] // 2 - cx, nb[1] + nb[3] // 2 - cy]}
                       for nb in nearby_boxes]
    return t


def _sample_from_locate(sid, group, screen, rect, target, difficulty="", has_target=True,
                       truth_box=None) -> dict:
    """跑一次真实定位，把②a 的候选留痕变成一条可重放的证据。

    truth_box：**运行时**真值应该在哪（页面重排的场景里，它不等于录制框——录制框只用来
    算"离录点多远"）。默认按录制框判真值。
    """
    cfg = LocConfig(evidence_top_n=EVIDENCE_TOP_N)
    r = locator.locate_widget_on_screen(screen, rect, target, cfg=cfg)
    det = (r.get("detail") or {}).get("l2_ocr") or {}
    truth = truth_box or target.get("rect_in_page")
    tc = (truth[0] + truth[2] // 2, truth[1] + truth[3] // 2) if truth else None
    cands, truth_index = [], None
    for i, item in enumerate(det.get("top3") or []):
        box, score, dist, nb = item[0], item[1], item[2], item[3]
        cands.append({"box": [int(v) for v in box], "score": float(score),
                      "dist": int(dist or 0), "nearby_ok": nb})
        if tc is not None:
            c = (box[0] + box[2] // 2, box[1] + box[3] // 2)
            if max(abs(c[0] - tc[0]), abs(c[1] - tc[1])) <= TRUTH_TOL_PX:
                truth_index = i
    return evidence_sample(sid, group, cands, truth_index=truth_index,
                           has_target=has_target, difficulty=difficulty)


def gen_synthetic_evidence() -> list:
    """合成证据：正例（真值在候选里）+ 负例（本来就找不到目标 → 一个候选都不该选）。

    分组 group 用**场景名**：留出集按场景切，避免同一场景既训练又测试（防过拟合假象）。
    """
    out: list = []

    def add_login():
        page, boxes = S.login_page()
        for k, (px, py, sc) in enumerate(((300, 150, 1.0), (260, 120, 1.15))):
            screen, rect = S.scene_of(page, px, py, sc, canvas_w=1500, canvas_h=1050)
            spec = S.page_spec_of(page, rect=rect)
            bx = boxes.get("login_btn")
            if bx:
                t = S.widget_target(page, spec, bx, text="登录")
                out.append(_sample_from_locate(f"login_pos{k}", "login", screen, rect, t))
            out.append(_sample_from_locate(
                f"login_neg{k}", "login_missing", screen, rect,
                _target(page, spec, [120, 300, 180, 34], "提交订单"),
                has_target=False, difficulty="negative"))

    def add_erp():
        page, boxes = S.erp_v1_page()
        for k, (px, py) in enumerate(((200, 120), (320, 200))):
            screen, rect = S.scene_of(page, px, py, 1.0, canvas_w=1500, canvas_h=1050)
            spec = S.page_spec_of(page, rect=rect)
            key = "menu" if "menu" in boxes else next(iter(boxes))
            t = S.widget_target(page, spec, boxes[key], text="库存查询")
            out.append(_sample_from_locate(f"erp_pos{k}", "erp", screen, rect, t))
            out.append(_sample_from_locate(
                f"erp_neg{k}", "erp_missing", screen, rect,
                _target(page, spec, [160, 360, 200, 34], "导出报表"),
                has_target=False, difficulty="negative"))

    def add_dup_query():
        """同页两个"查询" + 运行时行序对调：只有邻居文字能认出哪个才对。

        录制时"物料编码/查询"在上方（录点=上方那个）；运行时两行对调 → 上方那个（离录点近，
        但邻居是"供应商"）是错的，下方那个（离录点远 80px，邻居"物料编码"对得上）才对。
        这正好考"邻居优先 vs 就近优先"这条偏好顺序。
        """
        rec_page, bx_rec = _dup_query_page(("物料编码", "供应商"))
        run_page, bx_run = _dup_query_page(("供应商", "物料编码"))
        for k, (px, py) in enumerate(((240, 130), (180, 90))):
            screen, rect = S.scene_of(run_page, px, py, 1.0, canvas_w=1500, canvas_h=1050)
            spec = S.page_spec_of(rec_page, rect=rect)
            t = _target(rec_page, spec, bx_rec["query0"], "查询",
                        nearby_boxes=[list(bx_rec["label0"]) + ["物料编码"]])
            out.append(_sample_from_locate(
                f"dup_q_{k}", "dup_query", screen, rect, t, truth_box=bx_run["query1"],
                difficulty="same_text_reordered"))
            # 负例：页面上没有"物料编号"，只有字形相近的"物料编码"（difflib 相似度正好 0.75）
            # → 门槛设 0.75 就会误点、设 0.80 就不会：这条证据直接考 text_sim_min
            t2 = _target(rec_page, spec, bx_rec["label0"], "物料编号")
            out.append(_sample_from_locate(
                f"dup_neg_{k}", "dup_missing", screen, rect, t2, has_target=False,
                difficulty="negative_similar"))

    def add_feed():
        scene = S.FeedScene(w=1100, h=760, top_h=46)
        for k in range(2):
            if k:
                scene.next_frame()
            screen = scene.screen()
            rect = (0, 0, 1100, 760)
            spec = S.page_spec_of(screen, rect=rect)
            t = scene.widget_target("热门", page_spec=spec)
            out.append(_sample_from_locate(f"feed_pos{k}", "feed", screen, rect, t,
                                           difficulty="dynamic"))
            out.append(_sample_from_locate(
                f"feed_neg{k}", "feed_missing", screen, rect,
                _target(screen, spec, [400, 300, 200, 34], "我的收藏"),
                has_target=False, difficulty="negative"))

    for fn in (add_login, add_erp, add_dup_query, add_feed):
        try:
            fn()
        except Exception as e:                   # 某个场景造不出来不该毁掉整轮调参
            print(f"  场景生成失败（跳过）：{fn.__name__} {e!r}")
    return out


# ---------------------------------------------------------------- 报告

def sensitivity(samples: list, best: dict, space: dict) -> dict:
    """单参数敏感性：固定其他最优值，逐个扫该参数的候选值（参数表的"取值依据"）。"""
    out = {}
    for param, values in space.items():
        rows = []
        for v in values:
            p = dict(best, **{param: v})
            r = evaluate(samples, p)
            rows.append((v, r["hit_rate"], r["false_rate"], r["score"]))
        out[param] = rows
    return out


def _coverage_note(samples: list) -> str:
    """如实说明这批证据能考什么、不能考什么——避免"表格好看就当真"。"""
    groups = {s["group"] for s in samples}
    near = [c for s in samples for c in s["cands"]
            if c.get("dist", 0) <= s.get("far_limit", 160)]
    far = [c for s in samples for c in s["cands"]
           if c.get("dist", 0) > s.get("far_limit", 160)]
    scores = [c.get("score", 0.0) for s in samples for c in s["cands"]]
    mid = [x for x in scores if 0.5 <= x < 0.95]
    same_text = sum(1 for s in samples if str(s.get("difficulty", "")).startswith("same_text"))
    neg_similar = sum(1 for s in samples
                      if str(s.get("difficulty", "")) == "negative_similar")
    neg_with_cands = [s for s in samples if not s.get("has_target") and s.get("cands")]
    lines = ["- 有区分力的：",
             f"  - `prefer_nearby`：{same_text} 条「同页同名 + 运行期行序调换」证据"
             "（真值只有靠邻居文字才认得出）；",
             f"  - `text_sim_min`：{neg_similar} 条字形相近的负例（相似度恰好压线，"
             f"门槛放宽就会误点）；候选分数分布里中间段（0.5~0.95）有 {len(mid)} 个。"]
    miss = []
    if not far:
        miss.append("`allow_far` / `far_limit_scale` / `max_dist`：这批证据的候选**全部**"
                    "落在近处，考不出来")
    if not any(not s.get("has_target") and s.get("cands") for s in samples):
        miss.append("负例里没有「存在误匹配候选」的样本，误报率的分母偏乐观")
    miss.append("图片/模板路径的参数（`tpl_score_min` / `ring_score_min`）："
                "本轮只采了**文字**候选的证据")
    miss.append("页面级同源三档（`page_sim_min` / `page_sim_soft` / `page_soft_tpl_min`）："
                "属于整窗路径，不在②a 证据里")
    lines.append("- **还没被考到的**（不要根据本表改这些）：")
    lines += [f"  - {m}；" for m in miss]
    lines.append("- 结论：本表只足以给 `prefer_nearby` 与 `text_sim_min` 提供依据，"
                 "其余参数等真机证据（跑批 `--dump-evidence`）再定。")
    lines.append(f"- 场景分组：{', '.join(sorted(groups))}；"
                 f"近处候选 {len(near)} / 远处候选 {len(far)}"
                 f"（含候选的负例 {len(neg_with_cands)} 条）。")
    return "\n".join(lines)


def _diff_sources(samples: list, base: dict, best: dict):
    """换参数后**行为真的变了**的样本有哪些、属于哪类难度。

    这一步是为了把"留出集没提升"讲清楚：如果留出集里根本没有这类样本，那是**覆盖面不足**
    （不能据此判过拟合）；只有留出集覆盖了同类样本却仍无提升，才是过拟合信号。
    """
    changed, labels = [], []
    for s in samples:
        a, b = decide_text(s, base), decide_text(s, best)
        if (a["source"], a["chosen_index"]) != (b["source"], b["chosen_index"]):
            changed.append(s)
            lab = str(s.get("difficulty") or "-")
            if lab not in labels:
                labels.append(lab)
    return changed, labels


def write_doc(samples, train, hold, train_res, hold_before, hold_after, space, args) -> str:
    lines = ["# M2 融合参数表（WP3：相似度融合权重调优）", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 证据：{len(samples)} 条（正例 {sum(1 for s in samples if s['truth_index'] is not None)}"
             f" / 负例 {sum(1 for s in samples if s['truth_index'] is None)}），"
             f"分组 {len({s['group'] for s in samples})} 个",
             f"- 划分：训练 {len(train)} 条 / 留出 {len(hold)} 条"
             f"（**按场景分组切**，留出场景：{', '.join(sorted({s['group'] for s in hold})) or '—'}）",
             "- 口径：命中 = 选中真值候选；误报 = 没命中却选了别的（代价 4×，宁缺勿滥）；"
             "目标值 = 命中率 − 4 × 误报率", ""]
    best = train_res["best"]
    if best:
        lines += ["## 1. 结论", "",
                  f"- 训练集最优：`{', '.join(f'{k}={v}' for k, v in sorted(best['grid'].items()))}`"
                  f" → 命中 {best['hit_rate']:.3f}，误报 {best['false_rate']:.3f}",
                  f"- **留出集复验**：线上默认参数 命中 {hold_before['hit_rate']:.3f} / "
                  f"误报 {hold_before['false_rate']:.3f}；训练最优参数 命中 "
                  f"{hold_after['hit_rate']:.3f} / 误报 {hold_after['false_rate']:.3f}"]
        c = compare(hold_before, hold_after)
        changed, labels = _diff_sources(samples, DEFAULT_PARAMS, best["params"])
        hold_labels = {str(s.get("difficulty") or "-") for s in hold}
        uncovered = [lab for lab in labels if lab not in hold_labels]
        lines.append(f"- 换参数后**行为变化**的样本：{len(changed)} 条"
                     f"（难度标签：{', '.join(labels) or '—'}）；"
                     f"留出集含的标签：{', '.join(sorted(hold_labels)) or '—'}")
        if c["score"][2] > 0:
            lines.append(f"- 建议：在留出集上也有提升（目标值 {c['score'][2]:+.3f}），"
                         "可进入真机证据复验；**最终采纳仍需 M2-1 跑批数据确认**。")
        elif uncovered:
            lines.append(f"- 建议：**暂不采纳，但这不是过拟合**——留出集没有覆盖受影响的"
                         f"样本（缺：{', '.join(uncovered)}），所以它本来就考不出这个改动；"
                         "应把这批证据补进覆盖面，或等真机证据再判。")
        else:
            lines.append(f"- 建议：**暂不采纳**（留出集目标值变化 {c['score'][2]:+.3f}）——"
                         "留出集覆盖了同类样本却没有提升，属过拟合信号。")
        lines.append("")
    lines += ["## 2. 每个参数的取值依据（单参数敏感性：固定其他为最优值，在**全部证据**上扫）", ""]
    sens = sensitivity(samples, best["params"] if best else DEFAULT_PARAMS, space)
    lines += ["| 参数 | 取值 | 命中率 | 误报率 | 目标值 |", "|---|---|---|---|---|"]
    for param, rows in sens.items():
        for v, hr, fr, sc in rows:
            mark = " ←当前" if DEFAULT_PARAMS.get(param) == v else ""
            lines.append(f"| {param}{mark} | {v} | {hr:.3f} | {fr:.3f} | {sc:.3f} |")
    lines += ["", "## 3. 网格搜索前 12（训练集）", "",
              format_table(train_res["rows"], limit=12), "",
              "## 4. 证据覆盖面自评（哪些参数**还没被考到**）", "",
              _coverage_note(samples), "",
              "## 5. 复验注意", "",
              "- 本表来自**合成证据**（候选层面的差异），真机差异（遮挡/DPI/动态内容/多窗口）"
              "没有覆盖 → 必须用 M2-1 跑批证据复验后才写回引擎默认值。",
              "- 留出集按场景切分；若将来把真机证据合并进来，请保持"
              "「按脚本/场景分组」的划分方式。",
              "- 证据可由跑批产出（`--dump-evidence`）：真机证据与合成证据同一格式，"
              "`--evidence` 直接喂给本脚本。"]

    doc = "\n".join(lines) + "\n"
    DOC.write_text(doc, encoding="utf-8")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default="", help="已有证据 jsonl（真机或合成）")
    ap.add_argument("--save-evidence", action="store_true", help="把生成的证据落盘复用")
    ap.add_argument("--holdout", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.evidence:
        samples = [json.loads(l) for l in Path(args.evidence).read_text(
            encoding="utf-8").splitlines() if l.strip()]
        print(f"读入证据 {len(samples)} 条：{args.evidence}")
    else:
        print("生成合成证据（每个样本跑一次真实定位，稍慢）…")
        samples = gen_synthetic_evidence()
        print(f"  得到 {len(samples)} 条证据")
        if args.save_evidence and samples:
            EVIDENCE.write_text("\n".join(json.dumps(s, ensure_ascii=False)
                                          for s in samples) + "\n", encoding="utf-8")
            print(f"  证据已落盘：{EVIDENCE}")
    neg = sum(1 for s in samples if s["truth_index"] is None)
    print(f"  正例 {len(samples) - neg} / 负例 {neg}；候选总数 "
          f"{sum(len(s['cands']) for s in samples)}")
    if not samples:
        print("没有证据，结束")
        return 1

    train, hold, hold_groups = split_groups(samples, args.holdout, args.seed)
    print(f"训练 {len(train)} 条 / 留出 {len(hold)} 条（留出场景：{hold_groups}）")
    res = search(train, PARAM_SPACE, base=DEFAULT_PARAMS)
    base = res["best"]["params"] if res["best"] else DEFAULT_PARAMS
    hold_before = evaluate(hold or train, DEFAULT_PARAMS)
    hold_after = evaluate(hold or train, base)
    train_before = evaluate(train, DEFAULT_PARAMS)
    print(f"\n训练集：线上默认 命中 {train_before['hit_rate']:.3f} / 误报 "
          f"{train_before['false_rate']:.3f}"
          f" → 最优 命中 {res['best']['hit_rate']:.3f} / 误报 {res['best']['false_rate']:.3f}")
    print(f"留出集：线上默认 命中 {hold_before['hit_rate']:.3f} / 误报 "
          f"{hold_before['false_rate']:.3f}"
          f" → 训练最优 命中 {hold_after['hit_rate']:.3f} / 误报 {hold_after['false_rate']:.3f}")
    print(f"\n最优参数：{', '.join(f'{k}={v}' for k, v in sorted(res['best']['grid'].items()))}")
    print("\n" + format_table(res["rows"], limit=8))
    write_doc(samples, train, hold, res, hold_before, hold_after, PARAM_SPACE, args)
    print(f"\n参数表：{DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
