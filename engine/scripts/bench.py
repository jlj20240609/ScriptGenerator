# -*- coding: utf-8 -*-
"""M2 案例库跑批器：逐案例跑 N 轮，统计无人工介入率 / 成功率 / 误报率 / 耗时。

口径（docs/M2_计划.md §0）：
  · 无人工介入率（主指标）：run_done=ok **且**全程没有触发过任何人工交互
  · 成功率（辅助）：run_done=ok
  · 误报率：报成功但**终态断言不成立**的轮次（引擎说"找到了"，结果却不对）
  · 单步定位：模板路径中位数（沿用 M1 口径）

用法：
    python engine/scripts/bench.py --list
    python engine/scripts/bench.py --gen login_full     # 生成"登录含输错密码"案例
    python engine/scripts/bench.py --gen feed           # 生成强动态内容流案例
    python engine/scripts/bench.py --rounds 20          # 跑全部启用案例（默认不含真实客户端）
    python engine/scripts/bench.py --rounds 20 --case login_full --perturb move
    python engine/scripts/bench.py --rounds 20 --include-real   # 需要你在场时才用

产出：engine/scripts/bench_raw.jsonl（每轮一行）、engine/scripts/bench_report.md（汇总）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import ai as ai_mod  # noqa: E402
from engine import capture, locator, matcher, schema  # noqa: E402
from engine.calibrator import Calibrator  # noqa: E402
from engine.executor import (HUMAN_CONTINUE, HumanIO, LiveDriver,  # noqa: E402
                             RunConfig, run_script)

ASSETS = ROOT / "engine" / "tests" / "assets" / "live"
RAW = ROOT / "engine" / "scripts" / "bench_raw.jsonl"
REPORT = ROOT / "engine" / "scripts" / "bench_report.md"

# ---------------------------------------------------------------- 案例定义
# kind=fixture 用 smoke/fixtures 的网页；kind=real 是真实桌面客户端（默认不跑）
# expect_all：运行结束后页面上必须能找到的文字（终态断言；空则只看 status）
# reset：每轮开始前的复位（f5 = 刷新页面回初始状态）
CASES: dict[str, dict] = {
    "login_full": dict(kind="fixture", fixture="web-login.html", title="M0 演示登录",
                       asset="login_full", expect_all=["密码错误"], reset="f5",
                       enabled=True, note="登录：输工号 → 故意输错密码 → 点登录 → 看到错误就提示我"),
    "login": dict(kind="fixture", fixture="web-login.html", title="M0 演示登录",
                  asset="login", expect_all=[], reset="f5", enabled=True,
                  note="M0 资产：只点一下登录（回归基线）"),
    "erp": dict(kind="fixture", fixture="erp-web.html", title="M0 ERP 查询",
                asset="erp", expect_all=[], reset="f5", enabled=True,
                note="静态 ERP 页：页内定位与点击"),
    "dyn": dict(kind="fixture", fixture="dynamic-web.html", title="M0 动态监控台",
                asset="dyn", expect_all=[], reset="f5", enabled=True,
                note="动态心跳页：内容周期性变化"),
    "feed": dict(kind="fixture", fixture="dynamic-feed.html", title="M1 动态工作台",
                 asset="feed", expect_all=["推荐"], reset="f5", enabled=True,
                 note="强动态内容流（主区每 1.2s 重排）"),
    "real": dict(kind="real", fixture="", title="控制面板", asset="real",
                 expect_all=[], reset="none", enabled=False,
                 note="真实桌面客户端（默认跳过，需要你在场时用 --include-real）"),
}


# ---------------------------------------------------------------- 跑批用替身

class BenchHuman(HumanIO):
    """记录每一次"需要人工"，并按"继续"作答（保证轮次能跑完）。"""

    def __init__(self):
        self.calls = []

    def notify(self, message):
        self.calls.append(("notify", str(message)))

    def prompt_not_found(self, message, target_text):
        self.calls.append(("not_found", str(message)))
        return HUMAN_CONTINUE

    def prompt_outcome_fail(self, message):
        self.calls.append(("outcome_fail", str(message)))
        return HUMAN_CONTINUE


class CountLogger:
    """内存定位日志（与 LocLogger 同接口）：统计定位方式与模板路径耗时。"""

    def __init__(self):
        self.rows = []

    def log(self, step_id, event, method, confidence=0.0, rect=None, screen_meta=None,
            extra=None):
        self.rows.append({"step_id": step_id, "event": event, "method": method,
                          "confidence": confidence, "rect": rect, "extra": extra or {}})

    def tail(self, step_id, n=30):
        return [r for r in self.rows if r.get("step_id") == step_id][-n:]

    def methods(self) -> dict:
        out: dict[str, int] = {}
        for r in self.rows:
            if r["event"] in ("locate_page", "locate_widget", "click_guard"):
                key = f"{r['event']}:{r['method']}"
                out[key] = out.get(key, 0) + 1
        return out

    def tpl_ms(self) -> list:
        return [float((r.get("extra") or {}).get("elapsed_ms") or 0)
                for r in self.rows
                if r["event"] == "locate_widget" and r["method"] in ("tpl", "tpl_ring")]


# ---------------------------------------------------------------- 窗口与扰动

def _lr():
    from engine.scripts import live_regress as LR
    return LR


def ensure_case_window(case: dict) -> int:
    if case["kind"] == "real":
        hits = [h for h in capture.find_windows_by_title(case["title"])
                if not capture.is_iconic(h)]
        return hits[0] if hits else 0
    LR = _lr()
    edge = LR.find_edge()
    if not edge:
        raise RuntimeError("未找到 msedge.exe")
    return LR.ensure_window(edge, case["title"], case["fixture"])


def reset_page(hwnd: int, case: dict) -> None:
    if case.get("reset") != "f5":
        return
    capture.bring_to_foreground(hwnd)
    time.sleep(0.4)
    try:
        import win32api
        import win32con
        win32api.keybd_event(win32con.VK_F5, 0, 0, 0)
        win32api.keybd_event(win32con.VK_F5, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    time.sleep(2.0)


def perturb_window(hwnd: int, mode: str, rng: random.Random):
    if mode != "move":
        return None
    l, t, r, b = capture.window_rect(hwnd)
    dx, dy = rng.randint(-60, 60), rng.randint(-40, 40)
    try:
        import win32con
        import win32gui
        win32gui.SetWindowPos(hwnd, 0, l + dx, t + dy, 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
    except Exception:
        return None
    time.sleep(1.2)
    return [dx, dy]


def check_expect(case: dict, hwnd: int) -> dict:
    """终态断言：运行结束后页面上应能找到 expect_all 里的每条文字。"""
    want = case.get("expect_all") or []
    if not want:
        return {"ok": True, "detail": []}
    capture.bring_to_foreground(hwnd)
    time.sleep(0.6)
    l, t, r, b = capture.window_rect(hwnd)
    rect = [l, t, r - l, b - t]
    shot = capture.grab_screen(rect)
    detail, ok = [], True
    for text in want:
        res = locator.locate_widget_on_screen(shot, tuple(rect),
                                              {"text": text, "match": "text_first"})
        detail.append({"text": text, "found": bool(res["ok"]), "method": res.get("method"),
                       "ms": round(res.get("elapsed_ms", 0))})
        ok = ok and bool(res["ok"])
    return {"ok": ok, "detail": detail}


# ---------------------------------------------------------------- 单轮

def run_round(case_name: str, case: dict, hwnd: int, round_no: int, cfg: RunConfig,
              perturb: str, rng: random.Random) -> dict:
    row = {"case": case_name, "round": round_no, "ts": time.time(), "perturb": perturb,
           "status": None, "prompts": 0, "prompt_kinds": [], "assert_ok": None,
           "assert_detail": [], "calib": [], "ms": 0.0, "methods": {},
           "tpl_ms_median": None, "move": None, "error": None, "counters": {}}
    try:
        sg = schema.load(ASSETS / f"{case['asset']}.sgscript.json")
    except Exception as e:
        row["error"] = f"脚本资产缺失：{e}"
        return row
    row["move"] = perturb_window(hwnd, perturb, rng)
    reset_page(hwnd, case)
    driver = LiveDriver({"hwnd": hwnd})
    human = BenchHuman()
    logger = CountLogger()
    calibrator = None
    try:
        calib = Calibrator(driver, ai=ai_mod.SemanticStub(), window_rect=driver.window_rect)
        calibrator = lambda req: calib(req) or {}       # noqa: E731
    except Exception:
        calibrator = None
    t0 = time.perf_counter()
    try:
        rep = run_script(sg, driver, cfg=cfg, loc_logger=logger, human=human,
                         calibrator=calibrator)
    except Exception as e:
        rep = {"status": "failed", "steps": [], "counters": {}, "calib": [], "error": repr(e)}
    row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    row["status"] = rep.get("status")
    row["prompts"] = len(human.calls)
    row["prompt_kinds"] = [c[0] for c in human.calls]
    row["calib"] = [c.get("reason") for c in (rep.get("calib") or []) if c.get("reason")]
    row["methods"] = logger.methods()
    row["counters"] = rep.get("counters", {})
    row["error"] = rep.get("error")
    tpl = sorted(logger.tpl_ms())
    if tpl:
        row["tpl_ms_median"] = round(tpl[len(tpl) // 2], 1)
    chk = check_expect(case, hwnd)
    row["assert_ok"] = chk["ok"]
    row["assert_detail"] = chk["detail"]
    return row


# ---------------------------------------------------------------- 生成案例

def _grab_page_verified(w, case: dict, must: str, tries: int = 3):
    """前置窗口 → 抓屏 → 用 OCR 确认抓到的**确实是目标页面**，不对就重试。

    M1 的教训：别的前台窗口压在上面时，抓屏会拿到别人的画面（生成案例会"找不到文字"）。
    """
    last_txts: list = []
    for attempt in range(tries):
        try:
            import win32gui
            if not win32gui.IsWindow(w):
                raise RuntimeError("窗口已不存在（可能被关掉了）")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"窗口句柄失效：{e}") from e
        if capture.is_iconic(w) or True:
            capture.bring_to_foreground(w)         # 最小化会顺手还原
        time.sleep(1.8)
        l, t, r, b = capture.window_rect(w)
        rect = [l, t, r - l, b - t]
        shot = capture.grab_screen(rect)
        if shot is None:
            continue
        last_txts = matcher.ocr_run(shot)["txts"]
        if not must or any(must in x for x in last_txts):
            spec = capture.make_page_spec(
                bgr=shot, rect_in_screen=rect,
                context={"process": "msedge.exe", "title": capture.window_title(w),
                         "class": capture.window_class(w)})
            return shot, rect, spec
        print(f"  第 {attempt + 1} 次抓到的不是目标页面（见到：{last_txts[:5]}），重试…")
        time.sleep(1.0)
    raise RuntimeError(f"连续 {tries} 次都没抓到目标页面（要看到 {must!r}；"
                       f"最后见到：{last_txts[:6]}）——可能有别的窗口压在上面")


def _case_window_and_page(case: dict, must: str = ""):
    """打开窗口 → 验证抓屏 → 返回 (hwnd, page_bgr, page_rect, page_spec)。"""
    hwnd = ensure_case_window(case)
    if not hwnd:
        raise RuntimeError("窗口拉起失败")
    shot, page_rect, spec = _grab_page_verified(hwnd, case, must)
    return hwnd, shot, page_rect, spec


def _widget_factory(shot, page_rect, spec):
    """在给定页面上按文字定位 → 裁出部件 target（模拟用户"框住"那一块）。"""
    def widget(text: str, pad_x=40, pad_y=10):
        res = locator.locate_widget_on_screen(shot, tuple(page_rect),
                                              {"text": text, "match": "text_first"})
        if not res["ok"]:
            raise RuntimeError(f"页面上找不到 {text!r}（{res.get('method')}）")
        bx, by = res["box"][0] - page_rect[0], res["box"][1] - page_rect[1]
        bw, bh = res["box"][2], res["box"][3]
        box = [max(0, bx - pad_x), max(0, by - pad_y), bw + pad_x * 2, bh + pad_y * 2]
        crop = capture.crop_rect(shot, box)
        return schema.widget_target(
            image_dataurl=matcher.bgr_to_dataurl(crop), text=text, match="auto",
            rect_in_page=box, center_in_page=[box[0] + box[2] // 2, box[1] + box[3] // 2],
            page=spec)
    return widget


def _save(sg: dict, name: str) -> int:
    problems = schema.validate(sg)
    if problems:
        print("生成的脚本没通过校验：", problems)
        return 1
    out = ASSETS / f"{name}.sgscript.json"
    schema.dump(sg, out)
    print(f"已生成 {out.name}（{out.stat().st_size} 字节，{len(sg['steps'])} 步）")
    return 0


def gen_login_full() -> int:
    """生成"登录含输错密码"案例。

    条件分支的目标必须**先让它出现再录**（和真人操作一样）：所以先跑一次"输工号 +
    输错密码 + 点登录"，等红字出现再框它当条件目标。
    """
    case = CASES["login_full"]
    hwnd, shot, page_rect, spec = _case_window_and_page(case, must="登录")
    reset_page(hwnd, case)                       # 刷新回初始状态（空框 + 占位提示）
    hwnd, shot, page_rect, spec = _case_window_and_page(case, must="请输入工号")
    widget = _widget_factory(shot, page_rect, spec)
    w_user, w_pwd, w_btn = widget("请输入工号"), widget("请输入密码"), widget("登录", pad_x=24)
    head = [
        {"id": "s1", "type": "action", "action": "type", "params": {"text": "demo"},
         "target": w_user},
        {"id": "s2", "type": "action", "action": "type", "params": {"text": "wrong-pass"},
         "target": w_pwd},
        {"id": "s3", "type": "action", "action": "click", "params": {}, "target": w_btn},
    ]
    # 先跑一次，让"密码错误"出现在页面上
    print("先跑一次前 3 步，让错误提示出现…")
    probe = {"version": "1.0", "name": "probe", "targets_rev": 0, "steps": head}
    ph_human, ph_log = BenchHuman(), CountLogger()
    prev = run_script(probe, LiveDriver({"hwnd": hwnd}), cfg=RunConfig(guard=True),
                      loc_logger=ph_log, human=ph_human)
    print(f"  探针结果：status={prev.get('status')} 步骤={len(prev.get('steps') or [])} "
          f"需要人工={[c[0] for c in ph_human.calls]} err={prev.get('error')}")
    for st in (prev.get("steps") or [])[:4]:
        print(f"    · {st.get('status')} {st.get('label') or st.get('action')}")
    time.sleep(1.2)
    # 红字位置：优先"先跑一次再框"（和真人一样）；窗口不稳定/没出现时按位置构造
    w_err = None
    try:
        hwnd, shot2, page_rect2, spec2 = _case_window_and_page(case, must="密码错误")
        w_err = _widget_factory(shot2, page_rect2, spec2)("密码错误", pad_x=20, pad_y=8)
    except Exception as e:
        print(f"  （没能框到红字：{e}）→ 改用“密码框下方”的位置 + 明确文字构造条件目标")
        pbox = w_pwd["rect_in_page"]
        box = [pbox[0], pbox[1] + pbox[3] + 14, pbox[2], 30]
        crop = capture.crop_rect(shot, box)
        if crop is None or crop.size == 0:
            print("  取不到提示行区域，生成失败")
            return 1
        w_err = schema.widget_target(
            image_dataurl=matcher.bgr_to_dataurl(crop), text="密码错误，请重新输入",
            match="auto", rect_in_page=box,
            center_in_page=[box[0] + box[2] // 2, box[1] + box[3] // 2], page=spec)
    if w_err is None:
        print("  条件目标构造失败")
        return 1
    sg = {"version": "1.0", "name": "M2 案例 · 登录（含输错密码）", "targets_rev": 0,
          "steps": head + [
              {"id": "c1", "type": "condition",
               "condition": {"target": w_err, "exists": True},
               "then": [{"id": "n1", "type": "action", "action": "notify",
                         "params": {"message": "密码输错了，请重新输入"}}],
               "else": []}]}
    return _save(sg, "login_full")


def gen_feed() -> int:
    """生成强动态内容流案例：点顶栏"推荐"（顶栏是页面上稳定的一小块）。"""
    case = CASES["feed"]
    hwnd, shot, page_rect, spec = _case_window_and_page(case, must="推荐")
    widget = _widget_factory(shot, page_rect, spec)
    sg = {"version": "1.0", "name": "M2 案例 · 强动态内容流", "targets_rev": 0,
          "steps": [{"id": "s1", "type": "action", "action": "click", "params": {},
                     "target": widget("推荐", pad_x=24, pad_y=8)}]}
    return _save(sg, "feed")


# ---------------------------------------------------------------- 报告

def write_report(rows: list[dict], args) -> None:
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case"], []).append(r)
    lines = ["# M2 案例库跑批报告", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 每案例 {args.rounds} 轮；扰动：{args.perturb or '无'}；随机种子 {args.seed}",
             "- 口径：无人工介入 = 运行 ok **且**全程无人工交互；误报 = 报成功但终态断言不成立",
             "", "| 案例 | 轮数 | 成功率 | 无人工介入 | 误报 | 定位中位 | 触发校准 | 有人工的轮次 |",
             "|---|---|---|---|---|---|---|---|"]
    total = ok = clean = misreport = 0
    detail: list[str] = []
    for name, rs in by_case.items():
        n = len(rs)
        s_ok = sum(1 for r in rs if r["status"] == "ok")
        s_clean = sum(1 for r in rs if r["status"] == "ok" and r["prompts"] == 0)
        s_mis = sum(1 for r in rs if r["status"] == "ok" and r.get("assert_ok") is False)
        med = sorted(r["tpl_ms_median"] for r in rs if r.get("tpl_ms_median"))
        med_v = med[len(med) // 2] if med else None
        calib = sum(1 for r in rs if r["calib"])
        bad = [r["round"] for r in rs if r["prompts"]]
        lines.append(f"| {name} | {n} | {s_ok}/{n} | {s_clean}/{n} "
                     f"({100.0 * s_clean / n:.0f}%) | {s_mis} | "
                     f"{med_v if med_v is not None else '—'} ms | {calib} | {bad or '—'} |")
        total += n
        ok += s_ok
        clean += s_clean
        misreport += s_mis
        for r in rs:
            if r["status"] != "ok" or r["prompts"] or r.get("assert_ok") is False:
                detail.append(f"  - [{name} #{r['round']}] status={r['status']} "
                              f"prompts={r['prompt_kinds']} 断言={r.get('assert_ok')} "
                              f"calib={r['calib']} move={r.get('move')} err={r.get('error')}")
    rate = 100.0 * clean / total if total else 0.0
    mis_rate = 100.0 * misreport / total if total else 0.0
    ok_rate = 100.0 * ok / total if total else 0.0
    lines += ["", f"**合计**：{total} 轮；成功率 {ok}/{total}（{ok_rate:.1f}%，目标 ≥95%）；"
                  f"**无人工介入 {clean}/{total}（{rate:.1f}%，目标 ≥90%）**；"
                  f"误报 {misreport}（{mis_rate:.1f}%，目标 ≤2%）",
              "", "## 未达标轮次明细", ""] + (detail or ["  （无）"])
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with RAW.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n".join(lines[:15]))
    print(f"\n报告：{REPORT}\n原始：{RAW}")


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--case", default="")
    ap.add_argument("--perturb", default="", choices=["", "move"])
    ap.add_argument("--include-real", action="store_true")
    ap.add_argument("--gen", default="", help="生成案例脚本：login_full / feed")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    if args.list:
        for name, c in CASES.items():
            has = (ASSETS / f"{c['asset']}.sgscript.json").exists()
            print(f"  {name:11s} enabled={int(c['enabled'])} 资产={'有' if has else '缺'}  {c['note']}")
        return 0
    if args.gen:
        capture.init_dpi_aware()
        return {"login_full": gen_login_full, "feed": gen_feed}.get(
            args.gen, lambda: (print(f"不支持的生成目标：{args.gen}"), 2)[1])()

    capture.init_dpi_aware()
    names = [c.strip() for c in args.case.split(",") if c.strip()] or [
        n for n, c in CASES.items() if c["enabled"]]
    if args.include_real and "real" not in names:
        names.append("real")
    rng = random.Random(args.seed)
    cfg = RunConfig(guard=True, calibrate_first_run=True)
    rows: list[dict] = []
    for name in names:
        case = CASES.get(name)
        if not case:
            print(f"未知案例：{name}")
            continue
        if not case["enabled"] and not args.include_real:
            print(f"跳过未启用案例：{name}")
            continue
        if not (ASSETS / f"{case['asset']}.sgscript.json").exists():
            print(f"[{name}] 缺脚本资产（先跑 --gen {name}），跳过")
            continue
        hwnd = ensure_case_window(case)
        if not hwnd:
            print(f"[{name}] 窗口拉起失败，跳过")
            continue
        print(f"[{name}] 开始 {args.rounds} 轮…")
        for i in range(1, args.rounds + 1):
            row = run_round(name, case, hwnd, i, cfg, args.perturb, rng)
            rows.append(row)
            print(f"  #{i:02d} status={row['status']} prompts={row['prompts']} "
                  f"断言={row['assert_ok']} calib={row['calib']} {row['ms']:.0f}ms"
                  f"{'  err=' + str(row['error']) if row['error'] else ''}")
    if rows:
        write_report(rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
