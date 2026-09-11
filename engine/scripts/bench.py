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
import os
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import ai as ai_mod  # noqa: E402
from engine import capture, locator, matcher, schema  # noqa: E402
from engine.calibrator import Calibrator  # noqa: E402
from engine.errors import EngineError  # noqa: E402
from engine.executor import (HUMAN_CONTINUE, HumanIO, LiveDriver,  # noqa: E402
                             RunConfig, run_script)

ASSETS = ROOT / "engine" / "tests" / "assets" / "live"
RAW = ROOT / "engine" / "scripts" / "bench_raw.jsonl"
REPORT = ROOT / "engine" / "scripts" / "bench_report.md"
SHOTS_DIR = ROOT / "engine" / "scripts" / "bench_shots"
EVIDENCE = ROOT / "engine" / "scripts" / "bench_evidence.jsonl"
TRUTH_TOL_PX = 12          # 候选中心离录制框中心多近算"这就是真值"


# ---------------------------------------------------------------- 原始数据（增量 + 续跑）
# 跑批是几十分钟级的任务，中途随时可能被抢占：每轮**立刻**落盘，下次直接续跑，
# 不把已经花掉的机时丢在内存里。

def row_key(row: dict) -> tuple:
    """一轮结果的唯一键（案例 + 轮号）。"""
    return (str(row.get("case") or ""), int(row.get("round") or 0))


def load_raw(path: Path = RAW) -> list[dict]:
    """读已有 jsonl（容忍坏行/半行：被打断时最后一行常常是半个 JSON）。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue                       # 一行坏了不能连累整批数据
        if isinstance(r, dict) and r.get("case"):
            rows.append(r)
    return rows


def append_raw(row: dict, path: Path = RAW) -> None:
    """追加一轮结果并 flush（断电/被抢占最多丢当前这一轮）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def load_dedup(path: Path = RAW) -> dict:
    """读回并按 (案例, 轮号) 去重：**后写的覆盖先写的**（重跑过的轮次以最后一次为准）。"""
    out: dict = {}
    for r in load_raw(path):
        out[row_key(r)] = r
    return out


def _arm_round_watchdog(seconds: float, case: str, round_no: int):
    """本轮看门狗：到点就落一条"超时轮"记录并**直接退出进程**。

    为什么 driver 层的超时不够（实测踩到）：锁屏/独占全屏时，抓屏可能**阻塞在系统调用里**
    而不是抛错，于是"调用前检查"根本轮不到执行——整批死在那儿（两次，各卡了 8 小时）。
    所以再加一层硬兜底：进程退出后由外层用同一命令续跑，已完成的轮次会被跳过，
    被标记的"屏幕不可用"轮次会被重跑。
    """
    if not seconds or seconds <= 0:
        return None

    def _fire():
        try:
            append_raw({"case": case, "round": round_no, "ts": time.time(),
                        "status": "timeout", "prompts": 0, "prompts_manual": 0,
                        "prompt_kinds": [], "assert_ok": None, "assert_detail": [],
                        "calib": [], "ms": float(seconds) * 1000, "methods": {},
                        "tpl_ms_median": None, "move": None, "shot": "", "counters": {},
                        "error": f"本轮超过 {float(seconds):.0f}s 仍未结束"
                                 f"（抓屏/输入被系统阻塞的典型表现，常见于锁屏）",
                        "screen_error": True})
        except Exception:
            pass
        finally:
            os._exit(3)                          # 硬退出：外层续跑（数据已落盘）

    t = threading.Timer(float(seconds), _fire)
    t.daemon = True
    t.start()
    return t


def summarize(rows: list[dict]) -> dict:
    """按案例汇总 + 合计（报告与测试共用同一口径，避免两处算法不一致）。

    口径（M2 DoD）：成功率 = status ok；**无人工介入率** = ok 且全程无人工交互；
    误报率 = 报 ok 但终态断言不成立。
    """
    by_case: dict[str, dict] = {}
    for r in rows:
        s = by_case.setdefault(r["case"], {
            "n": 0, "ok": 0, "clean": 0, "clean_manual": 0, "misreport": 0, "calib": 0,
            "calib_first": 0, "calib_abnormal": 0,
            "bad_rounds": [], "problems": [], "med_ms": None, "_ms": []})
        n_ok = r.get("status") == "ok"
        n_manual = int(r.get("prompts_manual", r.get("prompts", 0)) or 0)   # 异常求助
        n_any = int(r.get("prompts", 0) or 0)                              # 含脚本自带的「提示我」
        # 两个口径都算、都报——不擅自替用户选口径：
        #   严格（clean，主指标）= 文档里拍板的验收定义："ok 且全程没有出现任何 confirm_request
        #        （没走 L1 弹窗、没弹『提示我』、没触发人工兜底）"；
        #   放宽（clean_manual）= 只把异常求助（没找到 / 做完没看到）算人工介入，
        #        脚本自己设计的『提示我』不算。
        #   为什么两个都要：案例 ① 的设计就是"故意输错密码 → 提示我"，严格口径下它必然 0 分。
        #   这一点必须让人看见、由人来定，而不是悄悄放宽（我自己犯过这个错，已改回）。
        n_clean = n_ok and n_any == 0
        n_clean_manual = n_ok and n_manual == 0
        n_mis = n_ok and r.get("assert_ok") is False
        s["n"] += 1
        s["ok"] += int(n_ok)
        s["clean"] += int(n_clean)
        s["clean_manual"] += int(n_clean_manual)
        s["misreport"] += int(n_mis)
        s["calib"] += int(bool(r.get("calib")))
        # 校准分两类：first_run 是每轮的正常基线检查；其它（page_not_found / widget_not_found）
        # 才是"出了状况去补救"——混在一起会让人误以为每轮都在出问题。
        reasons = list(r.get("calib") or [])
        s["calib_first"] += int("first_run" in reasons)
        s["calib_abnormal"] += int(any(c != "first_run" for c in reasons))
        if r.get("tpl_ms_median"):
            s["_ms"].append(r["tpl_ms_median"])
        if n_any:
            s["bad_rounds"].append(r.get("round"))
        # 未达标 = 没做到"干净通过"，**或**断言不成立（误报）：后者同样是未达标轮次，
        # 必须出现在明细里，否则 DoD 的"每个不达标案例都能定位到轮次与原因"就落空了。
        if not n_clean or n_mis:
            s["problems"].append(
                f"  - [{r['case']} #{r.get('round')}] status={r.get('status')} "
                f"需人处理={r.get('prompts_manual', r.get('prompts'))}"
                f"{r.get('prompt_kinds')} 断言={r.get('assert_ok')} "
                f"calib={r.get('calib')} move={r.get('move')} "
                f"err={r.get('error')}{' shot=' + r['shot'] if r.get('shot') else ''}")
    for s in by_case.values():
        n = max(1, s["n"])
        s["ok_rate"] = 100.0 * s["ok"] / n
        s["clean_rate"] = 100.0 * s["clean"] / n
        s["clean_manual_rate"] = 100.0 * s["clean_manual"] / n
        s["mis_rate"] = 100.0 * s["misreport"] / n
        med = sorted(s.pop("_ms"))
        s["med_ms"] = med[len(med) // 2] if med else None
    total = len(rows)
    ok = sum(s["ok"] for s in by_case.values())
    clean = sum(s["clean"] for s in by_case.values())
    clean_manual = sum(s["clean_manual"] for s in by_case.values())
    mis = sum(s["misreport"] for s in by_case.values())
    slowest = sorted((r for r in rows if r.get("ms")), key=lambda r: -r["ms"])[:3]
    return {"by_case": by_case, "total": total, "ok": ok, "clean": clean,
            "clean_manual": clean_manual, "misreport": mis,
            "ok_rate": 100.0 * ok / total if total else 0.0,
            "clean_rate": 100.0 * clean / total if total else 0.0,
            "clean_manual_rate": 100.0 * clean_manual / total if total else 0.0,
            "mis_rate": 100.0 * mis / total if total else 0.0,
            "slowest": slowest}

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
                asset="dyn", expect_all=[], reset="f5", enabled=True, warmup_s=3.0,
                note="动态心跳页：内容周期性变化（复位后先等它变几拍再跑）"),
    "feed": dict(kind="fixture", fixture="dynamic-feed.html", title="M1 动态工作台",
                 asset="feed", expect_all=["推荐"], reset="f5", enabled=True, warmup_s=4.5,
                 note="强动态内容流（主区每 1.2s 重排；复位后先等它重排几轮再跑）"),
    "real": dict(kind="real", fixture="", title="控制面板", asset="real",
                 expect_all=[], reset="none", enabled=False,
                 note="真实桌面客户端（默认跳过，需要你在场时用 --include-real）"),
    # WP3 调参的证据来源：同页同名 / 相似文字 / 前缀干扰都在这一页上。
    # 别的案例里部件文字都独一无二，采出来的证据每条只有 1 个候选 → 阈值怎么调都一样。
    "interference": dict(kind="fixture", fixture="interference-web.html",
                         title="M2 干扰页", asset="interference",
                         expect_all=["已点：查询 @物料 C"], reset="f5", enabled=True,
                         warmup_s=0.0,
                         note="干扰页：同卡片 5 行各一个一样的「查询」（加载时随机换序；"
                              "行数多 + 随机序才能产出足够多「干扰更近」的可区分样本）"
                              " + 相似词「库存统计」+ 前缀干扰「保存/保存并关闭」"),
}


# ---------------------------------------------------------------- 跑批用替身

class BenchHuman(HumanIO):
    """记录每一次人工交互，并按"继续"作答（保证轮次能跑完）。

    注意区分两类：`notify` 是**脚本自己设计的**"提示我"步骤（例如"密码输错了"），
    属于执行成功的一部分；`not_found` / `outcome_fail` 才是**异常求助**。
    M2 DoD 的"无人工介入率"只看后者——否则案例 ① 这种"故意输错→提示我"的脚本会永远
    判成不达标（实测踩到）。
    """

    MANUAL_KINDS = ("not_found", "outcome_fail")

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

    def manual_count(self) -> int:
        """需要人处理的次数（不含脚本设计的"提示我"）。"""
        return sum(1 for c in self.calls if c[0] in self.MANUAL_KINDS)


class CountLogger:
    """内存定位日志：**实现官方接口 log_loc(row)**（executor 调用的就是它）。

    教训：这里原先写成了 `log(...)`，与 executor 的 `self.log.log_loc(row)` 根本对不上，
    每次定位都抛 AttributeError → 跑批 100% failed。这种错**只有真机跑批才会暴露**
    （合成单测里用的是官方 MemoryLogger，绕过了这个替身）。现在有测试盯着接口。
    另外 executor 会把 extra 字段**展开到 row 顶层**（见 executor._loc_log），
    所以统计与证据采集都从顶层读，不找 extra 子字典。
    """

    def __init__(self):
        self.rows = []

    def log_loc(self, row: dict) -> dict:
        full = dict(row or {})
        full.setdefault("ts", time.time())
        self.rows.append(full)
        return full

    def tail(self, step_id=None, n=50) -> list:
        rows = self.rows if step_id is None else [
            r for r in self.rows if r.get("step_id") == step_id]
        return rows[-n:]

    def methods(self) -> dict:
        out: dict[str, int] = {}
        for r in self.rows:
            if r.get("event") in ("locate_page", "locate_widget", "click_guard"):
                key = f"{r['event']}:{r.get('method')}"
                out[key] = out.get(key, 0) + 1
        return out

    def tpl_ms(self) -> list:
        return [float(r.get("elapsed_ms") or 0)
                for r in self.rows
                if r.get("event") == "locate_widget"
                and r.get("method") in ("tpl", "tpl_ring")]


# ---------------------------------------------------------------- 定位证据（M2-WP3）

def iter_steps(sg: dict) -> list:
    """递归遍历脚本全部步骤（含条件分支与循环体），返回 [(step_id, step)]。

    step_id 解析规则与 executor 一致（有 id 用 id，否则用嵌套路径），这样证据里的 step_id
    能对上定位日志。
    """
    def walk(arr, path=""):
        for i, st in enumerate(arr or []):
            if not isinstance(st, dict):
                continue
            sid = st.get("id", f"{path}{i}" if not path else f"{path}▶{i}")
            yield sid, st
            if st.get("type") == "condition":
                yield from walk(st.get("then"), sid)
                yield from walk(st.get("else"), sid)
            elif st.get("type") == "loop":
                yield from walk(st.get("body"), sid)

    return list(walk(sg.get("steps")))


def collect_evidence(case_name: str, round_no: int, sg: dict, logger) -> list:
    """把本轮定位日志转成调参证据（与 engine/scripts/tune.py 完全同一格式）。

    真值判定：候选中心落在**录制框**附近就算真值。对 fixture 案例（页面布局稳定）可靠；
    页面真被重排时这条判定会有噪声——所以证据里标了 difficulty=live，调参报告会提醒。
    """
    tgts = {}
    for sid, st in iter_steps(sg):
        if isinstance(st.get("target"), dict):
            tgts[sid] = st["target"]
    out = []
    for r in logger.rows:
        if r.get("event") != "locate_widget":
            continue
        top = r.get("top3") or []                  # extra 被 executor 展开到顶层，不找 extra 子字典
        if not top:
            continue
        t = tgts.get(r.get("step_id")) or {}
        rec = t.get("rect_in_page")
        far = 160.0
        tc = None
        if rec:
            far = float(max(2 * int(rec[2]), 2 * int(rec[3]), 160))
            tc = (rec[0] + rec[2] // 2, rec[1] + rec[3] // 2)
        cands, truth = [], None
        for i, item in enumerate(top):
            box, score, dist, nb = item[0], item[1], item[2], item[3]
            cands.append({"box": [int(v) for v in box], "score": float(score),
                          "dist": int(dist or 0), "nearby_ok": nb})
        # 真值判定：**有邻居线索时以邻居为准**。
        # 原按"候选离录制框 ≤12px"判，但页面重排后真值已经不在录制框附近，会把干扰误标成真值
        # （实测干扰页 25% 条目中招，直接导致调参算出"关掉邻居优先反而更好"的反向结论）。
        # 有邻居记录却**一个候选都对不上** → 说明真值根本没进候选（②a 的搜索带只覆盖了干扰），
        # 这时真值必须标成"不在候选里"（= 漏检），而不是硬指一个干扰当真值。
        if t.get("nearby"):
            nb_idx = [i for i, c in enumerate(cands) if c.get("nearby_ok") is True]
            truth = (min(nb_idx, key=lambda i: cands[i].get("dist", 0)) if nb_idx else None)
        elif tc is not None:
            for i, c in enumerate(cands):
                cc = (c["box"][0] + c["box"][2] // 2, c["box"][1] + c["box"][3] // 2)
                if max(abs(cc[0] - tc[0]), abs(cc[1] - tc[1])) <= TRUTH_TOL_PX:
                    truth = i
        out.append({"id": f"{case_name}_{round_no}_{r.get('step_id')}", "group": case_name,
                    "difficulty": "live", "anchor_xy": [], "cands": cands,
                    "truth_index": truth, "has_target": True, "far_limit": far})
    return out


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

def save_shot(driver, case_name: str, round_no: int) -> str:
    """失败/有人工介入的轮次留一张缩略截图（报告里能直接看到"当时屏幕上是什么样"）。"""
    try:
        import cv2
        screen, _ = driver.grab_screen()
        h, w = screen.shape[:2]
        k = 960.0 / max(1, w)
        if k < 1.0:
            screen = cv2.resize(screen, (int(w * k), int(h * k)),
                                interpolation=cv2.INTER_AREA)
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        p = SHOTS_DIR / f"{case_name}_{int(round_no):03d}.jpg"
        cv2.imwrite(str(p), screen, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return ""


def ensure_foreground(hwnd: int, tries: int = 3) -> bool:
    """确认目标窗口是前台（整屏抓屏要求它不被遮挡）；不是就再提一次。

    为什么要专门确认：页面定位抓的是**整屏**，上一个案例的窗口压在上面会让整窗模板直接失配
    （实测：erp 连续跑时 30 次定位全灭，会话里单跑却一次就过）。把结果记进轮次数据里，
    以后失败轮一眼就能看出"当时窗口根本不在前台"。
    """
    for i in range(max(1, tries)):
        info = capture.fg_window_info() or {}
        if int(info.get("hwnd") or 0) == int(hwnd):
            return True
        capture.bring_to_foreground(hwnd)
        time.sleep(0.6 if i == 0 else 0.4)
    info = capture.fg_window_info() or {}
    return int(info.get("hwnd") or 0) == int(hwnd)


def other_case_names(keep: str) -> list:
    """除 keep 之外、**不共用同一个 fixture 窗口**的案例名（拿去关窗口用）。

    坑（实测踩到）：不同案例可能共用同一个 fixture 与窗口标题——`login` 与 `login_full`
    都是 web-login.html、标题都是"M0 演示登录"。若按案例名去重，"关掉别的案例"就会把
    **自己那一个**也关掉：login_full 第 21 轮因此窗口句柄失效、35 次定位全灭、11 次求助。
    所以去重键是 fixture（不是案例名）。
    """
    me = CASES.get(keep) or {}
    my_fixture = me.get("fixture")
    return [n for n, c in CASES.items()
            if n != keep and c.get("fixture") and c.get("fixture") != my_fixture]


def close_other_cases(keep: str) -> list:
    """关掉**其他**案例的 fixture 窗口：多个 fixture 同时在场会互相遮挡、抢前台。

    跑批是逐案例串行的，别的案例窗口留着只有坏处（实测踩到：erp 连跑 30 次全失败）。
    共用同一个 fixture 的案例不动（见 other_case_names）。
    """
    closed = []
    for other in other_case_names(keep):
        oc = CASES.get(other) or {}
        try:
            _lr().close_windows(oc["title"])
            closed.append(other)
        except Exception:
            pass
    return closed


class DeadlinedDriver:
    """给 driver 套一个"本轮墙钟上限"：超时就抛错，让这一轮干脆失败。

    为什么需要（实测踩到）：真机跑批时如果屏幕不可用（用户锁屏/独占全屏），抓屏会抛
    `BitBlt: 拒绝访问`，而执行器会照常走"没找到 → 重试 → 提示 → 再试"的长链路，
    一轮能磨掉 **25 分钟**；没人管的话整批就死在那儿（实测卡了 8 小时）。
    卡住总是发生在 driver 调用上（抓屏/点击），所以在这里判最准：每次调用前检查一次，
    超时就抛 EngineError，交给 run_round 记成这一轮的 error。
    """

    def __init__(self, inner, max_round_s: float):
        self._inner = inner
        self._deadline = time.time() + float(max_round_s) if max_round_s and max_round_s > 0 else None

    def _check(self):
        if self._deadline is not None and time.time() > self._deadline:
            raise EngineError("round_timeout",
                              f"本轮超过墙钟上限（>{int(self._deadline - time.time())}s 已耗尽）")

    def grab_screen(self, *a, **kw):
        self._check()
        return self._inner.grab_screen(*a, **kw)

    def grab_rect(self, *a, **kw):
        self._check()
        return self._inner.grab_rect(*a, **kw)

    def click(self, *a, **kw):
        self._check()
        return self._inner.click(*a, **kw)

    def type_text(self, *a, **kw):
        self._check()
        return self._inner.type_text(*a, **kw)

    def hotkey(self, *a, **kw):
        self._check()
        return self._inner.hotkey(*a, **kw)

    def raise_if_needed(self, *a, **kw):
        return self._inner.raise_if_needed(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def ensure_anchors(sg: dict, hwnd: int) -> int:
    """确保脚本资产里的页面带**静态锚**（动态页必需），返回新增锚数。

    锚本该在录制时就采好，但 `--gen` 生成资产时没采（那会儿还不知道哪些区域稳定）。
    后果实测过：动态页没有锚 → 每轮都"整窗失配 → 校准"，锚永远用不上；真机上每次校准
    都要打扰用户一次——这正是 M0「bili feed 只有 3%」的机制。
    这里在跑批前补一次，采到的锚由调用方写回资产，之后每轮都能直接走锚定位。
    """
    from engine.calibrator import Calibrator as _Cal
    l, t, r, b = capture.window_rect(hwnd)
    wrect = (l, t, r - l, b - t)
    page_img = capture.grab_screen(wrect)
    if page_img is None or page_img.size == 0:
        return 0
    driver = LiveDriver({"hwnd": hwnd})
    cal = _Cal(driver, ai=ai_mod.SemanticStub(), window_rect=driver.window_rect,
               anchor_dt_s=1.6)
    # 不做"页面是否静态"的判断：那个探测在真机上会被后台节流等因素误导（实测误判成静态，
    # 于是锚一个都没补上）。这里无条件采——静态页多一层锚兜底无害，动态页则是必须的；
    # 采到的锚如果与其它锚不一致，运行时的多锚共识自己会把它排除。
    added = 0
    for _sid, st in iter_steps(sg):
        tgt = st.get("target")
        if not isinstance(tgt, dict):
            continue
        page = tgt.get("page")
        box = tgt.get("rect_in_page")
        if not isinstance(page, dict) or not box or page.get("anchors"):
            continue
        got = cal._collect_anchors(page_img, box, wrect, {})
        if got:
            page["anchors"] = [{k: v for k, v in a.items() if k != "_stable"} for a in got]
            added += len(got)
    return added


def run_round(case_name: str, case: dict, hwnd: int, round_no: int, cfg: RunConfig,
              perturb: str, rng: random.Random, shot: bool = False,
              evidence: list | None = None, max_round_s: float = 0.0) -> dict:
    """跑一轮。**整轮**都在异常保护里：任何一步炸掉都记成这一轮的 error，
    绝不让单轮问题中断整批（机时太贵，前面的结果必须留在盘上）。

    evidence：传一个 list 进来就把本轮的定位证据（调参用）收进去。
    max_round_s：单轮墙钟上限（0=不限）；超时/驱动错误的轮次会被标出来，
    连续多轮这样时由 main 决定停批（屏幕不可用时继续跑毫无意义）。
    """
    row = {"case": case_name, "round": round_no, "ts": time.time(), "perturb": perturb,
           "status": None, "prompts": 0, "prompts_manual": 0, "prompt_kinds": [],
           "assert_ok": None, "assert_detail": [], "calib": [], "ms": 0.0, "methods": {},
           "tpl_ms_median": None, "move": None, "error": None, "counters": {},
           "shot": "", "screen_error": False, "fg_ok": None, "warmup_s": 0,
           "page_delta": None}
    driver = None
    t0 = time.perf_counter()
    try:
        try:
            sg = schema.load(ASSETS / f"{case['asset']}.sgscript.json")
        except Exception as e:
            row["error"] = f"脚本资产缺失：{e}"
            return row
        row["move"] = perturb_window(hwnd, perturb, rng)
        reset_page(hwnd, case)
        # 预热：动态页复位后是"确定初态"，整窗模板必然命中——那样等于没考动态定位
        # （实测：f5 后立刻跑，feed/dyn 100% 走整窗模板、锚一次没用上）。
        # 先等页面自己变几拍，整窗模板才会真的失配，定位才有难度。
        warm = float(case.get("warmup_s") or 0)
        row["warmup_s"] = warm
        if warm > 0:
            # 顺带量一下"预热期间页面到底变了多少"：整窗模板命中与否取决于这个。
            # 没有这个数字，就没法判断"动态案例"是真的在考动态定位，还是页面压根没变。
            try:
                import cv2
                l0, t0, r0, b0 = capture.window_rect(hwnd)
                wr = [l0, t0, r0 - l0, b0 - t0]
                shot_a = capture.grab_screen(wr)
                time.sleep(warm)
                shot_b = capture.grab_screen(wr)
                if (shot_a is not None and shot_b is not None
                        and shot_a.shape == shot_b.shape):
                    row["page_delta"] = int(
                        (cv2.absdiff(shot_a, shot_b).max(axis=2) > 24).sum())
            except Exception:
                time.sleep(warm)
        row["fg_ok"] = ensure_foreground(hwnd)
        driver = DeadlinedDriver(LiveDriver({"hwnd": hwnd}), max_round_s)
        human = BenchHuman()
        logger = CountLogger()
        calibrator = None
        try:
            calib = Calibrator(driver, ai=ai_mod.SemanticStub(), window_rect=driver.window_rect)
            calibrator = lambda req: calib(req) or {}       # noqa: E731
        except Exception:
            calibrator = None
        try:
            rep = run_script(sg, driver, cfg=cfg, loc_logger=logger, human=human,
                             calibrator=calibrator)
        except Exception as e:
            rep = {"status": "failed", "steps": [], "counters": {}, "calib": [],
                   "error": repr(e)}
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        row["status"] = rep.get("status")
        row["prompts"] = len(human.calls)
        row["prompt_kinds"] = [c[0] for c in human.calls]
        row["prompts_manual"] = human.manual_count()
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
        if evidence is not None:
            try:
                evidence.extend(collect_evidence(case_name, round_no, sg, logger))
            except Exception as exc:
                # 不静默：证据采集坏了必须看得见——否则调参一直拿不到新数据却没人知道
                print(f"    ⚠ 本轮证据采集失败（跳过）：{exc!r}")
    except Exception as e:
        row["error"] = f"轮次异常：{e!r}"
        if row["status"] is None:
            row["status"] = "error"
        row["ms"] = row["ms"] or round((time.perf_counter() - t0) * 1000, 1)
    need_shot = (row["status"] != "ok" or row["prompts"] or row["assert_ok"] is False)
    if shot and need_shot and driver is not None:
        row["shot"] = save_shot(driver, case_name, round_no)
    # 屏幕不可用类错误（抓屏被拒 / 本轮超时）：上层据此决定要不要停批——
    # 锁屏或独占全屏时继续跑 100 轮毫无意义（实测卡过 8 小时）。
    err = str(row.get("error") or "")
    row["screen_error"] = bool(err) and any(
        k in err for k in ("driver_error", "round_timeout", "拒绝访问", "BitBlt"))
    return row


# ---------------------------------------------------------------- 生成案例

def _grab_page_verified(w, case: dict, must: str, tries: int = 5):
    """前置窗口 → 抓屏 → 用 OCR 确认抓到的**确实是目标页面**，不对就重试。

    M1 的教训：别的前台窗口压在上面时，抓屏会拿到别人的画面（生成案例会"找不到文字"）。
    M2 补：Edge `--app` 窗口刚起来时页面还在加载（抓到的只有标题栏），所以重试次数与
    间隔都要给够——否则会误判成"窗口被压住"。
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
        time.sleep(2.2 if attempt == 0 else 1.6)
        l, t, r, b = capture.window_rect(w)
        rect = [l, t, r - l, b - t]
        shot = capture.grab_screen(rect)
        if shot is None:
            continue
        last_txts = matcher.ocr_run(shot)["txts"]
        if not must or any(must in x for x in last_txts):
            spec = capture.make_page_spec(
                bgr=shot, rect_in_screen=rect,
                # 必须记下录制时的显示缩放：跨 DPI 时定位要靠"当前 DPI ÷ 录制 DPI"预判页面尺度。
                # 不记的话 WP8 的预判等于没装（实测：生成器一直没传 dpi，capture_meta.dpi=0）。
                dpi=capture.dpi_of(w),
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


def _pick_text_box(shot, text, near_text=None):
    """在页面上挑"就是这个词"的那个盒：**优先完全相等**的 OCR 命中。

    坑（实测踩到）：页面上别处的长句常包含目标词——例如副标题"统一身份认证·请使用工号登录"
    包含"登录"，而 text_similar 对"长串包含短词"给 0.98 分。于是按"离锚点近"排序时会选错
    元素（实测：按钮目标被框到副标题上，偏了 260px，点下去什么也没发生）。
    生成案例时用完全相等优先，歧义立刻消失。

    near_text：同页有**多个同名控件**时，用"离某个邻居文字最近的那个"来指定是哪一个
    （例如"物料编码"那一行的『查询』）——这与运行时靠邻居文字消歧是同一套思路。
    """
    hits = matcher.find_text_all_ocr(shot, text, thr=0.5)
    if not hits:
        return None
    norm = "".join(str(text).split())
    exact = [h for h in hits if "".join(str(h["matched_text"]).split()) == norm]
    pool = exact or hits
    if near_text:
        anchors = matcher.find_text_all_ocr(shot, near_text, thr=0.6)
        if anchors:
            ab = anchors[0]["box"]
            acx, acy = ab[0] + ab[2] // 2, ab[1] + ab[3] // 2

            def _d(h, _acx=acx, _acy=acy):
                b = h["box"]
                return max(abs(b[0] + b[2] // 2 - _acx), abs(b[1] + b[3] // 2 - _acy))

            return min(pool, key=_d)
    return max(pool, key=lambda h: float(h.get("score", 0.0)))


def _widget_factory(shot, page_rect, spec):
    """在给定**页面图**上按文字定位 → 裁出部件 target（坐标即页内坐标）。

    坑：shot 是"页面区域图"（`capture.grab_screen(窗口rect)` 的结果），**不是整屏**。
    原先这里调的是 `locate_widget_on_screen(shot, page_rect, ...)`（整屏版），于是坐标被
    page_rect 又偏移了一次、还常被裁剪到角落——生成出来的目标框指向页面上错误的位置。
    这种错在合成单测里看不出来，只有真机跑批/生成时才会暴露。正确做法是用页内版：
    页内版返回的 box 本身就是页内坐标，直接用。

    near_text：同页多个同名控件时指定"哪一行的那一个"；
    nearby_texts：要记进 target 的邻居文字（运行时用它消歧，录制侧必须记下来才有用）。
    """
    def widget(text: str, pad_x=40, pad_y=10, near_text=None, nearby_texts=None):
        ph_, pw_ = shot.shape[:2]
        res = locator.locate_widget(shot, (0, 0, pw_, ph_),
                                    {"text": text, "match": "text_first"})
        box0 = _pick_text_box(shot, text, near_text=near_text)
        if box0 is not None:
            bx, by, bw, bh = [int(v) for v in box0["box"]]      # 完全相等优先，不受歧义干扰
        elif res["ok"]:
            bx, by, bw, bh = [int(v) for v in res["box"]]
        else:
            raise RuntimeError(f"页面上找不到 {text!r}（{res.get('method')}）")
        box = [max(0, bx - pad_x), max(0, by - pad_y), bw + pad_x * 2, bh + pad_y * 2]
        crop = capture.crop_rect(shot, box)
        t = schema.widget_target(
            image_dataurl=matcher.bgr_to_dataurl(crop), text=text, match="auto",
            rect_in_page=box, center_in_page=[box[0] + box[2] // 2, box[1] + box[3] // 2],
            page=spec)
        if nearby_texts:
            cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
            nb = []
            for nt in nearby_texts:
                nbox = _pick_text_box(shot, nt)
                if not nbox:
                    continue
                nx, ny, nw, nh = [int(v) for v in nbox["box"]]
                nb.append({"text": nt, "rect_in_page": [nx, ny, nw, nh],
                           "offset": [nx + nw // 2 - cx, ny + nh // 2 - cy]})
            if nb:
                t["nearby"] = nb
        return t
    return widget


def verify_targets(sg: dict, shot, spec) -> list:
    """生成后自检：**目标框里是不是真的有那个词**。

    必须独立于定位器——用同一个定位器自检等于"自己验自己"：实测中它把按钮目标框到了副标题上，
    自检却一路 OK。这里直接在目标框里跑 OCR 并要求**完全匹配**，一眼就能看出框错了元素
    （副标题虽然含"登录"二字，但不是"登录"这个词本身）。
    """
    out = []
    for sid, st in iter_steps(sg):
        t = st.get("target")
        if not isinstance(t, dict) or not t.get("rect_in_page"):
            continue
        text = str(t.get("text") or "").strip()
        box = [int(v) for v in t["rect_in_page"]]
        crop = capture.crop_rect(shot, box)
        if crop is None or crop.size == 0:
            out.append((sid, text, "目标框越出页面"))
            continue
        words = matcher.ocr_run(crop)["txts"]
        norm = "".join(text.split())
        ok = any("".join(str(x).split()) == norm for x in words) if norm else False
        out.append((sid, text, "OK" if ok else
                    f"框里没有完全匹配的 {text!r}（见到：{words[:3]}）"))
    return out


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
    for sid, text, note in verify_targets(sg, shot, spec):
        print(f"  自检 {sid} {text!r}: {note}")
    return _save(sg, "login_full")


def gen_interference() -> int:
    """生成"干扰页"案例：点**物料 C 那一行**的「查询」。

    这是 WP3 调参的证据来源：别的案例部件文字都独一无二，采出来的证据每条只有 1 个候选，
    阈值怎么调都看不出来。这里刻意让同页有 5 个一模一样的「查询」，且每次加载随机换序——
    于是"真值被换到下面、干扰留在上面"的组合大量出现，偏好顺序才有足够样本被考出来。
    录制时把邻居文字（"物料 C"）一起记进 target —— 运行时靠它认出"是这一行的查询"。
    """
    case = CASES["interference"]
    hwnd, shot, page_rect, spec = _case_window_and_page(case, must="物料查询")
    widget = _widget_factory(shot, page_rect, spec)
    target = widget("查询", pad_x=16, pad_y=8, near_text="物料 C",
                    nearby_texts=["物料 C"])
    sg = {"version": "1.0", "name": "M2 案例 · 干扰页（同页同名查询）", "targets_rev": 0,
          "steps": [{"id": "s1", "type": "action", "action": "click", "params": {},
                     "target": target}]}
    for sid, text, note in verify_targets(sg, shot, spec):
        print(f"  自检 {sid} {text!r}: {note}")
    nb = target.get("nearby") or []
    print(f"  邻居线索：{[(x['text'], x['offset']) for x in nb] or '（无）'}")
    return _save(sg, "interference")


def gen_feed() -> int:
    """生成强动态内容流案例：点顶栏"推荐"（顶栏是页面上稳定的一小块）。"""
    case = CASES["feed"]
    hwnd, shot, page_rect, spec = _case_window_and_page(case, must="推荐")
    widget = _widget_factory(shot, page_rect, spec)
    sg = {"version": "1.0", "name": "M2 案例 · 强动态内容流", "targets_rev": 0,
          "steps": [{"id": "s1", "type": "action", "action": "click", "params": {},
                     "target": widget("推荐", pad_x=24, pad_y=8)}]}
    for sid, text, note in verify_targets(sg, shot, spec):
        print(f"  自检 {sid} {text!r}: {note}")
    return _save(sg, "feed")


# ---------------------------------------------------------------- 报告

def write_report(rows: list[dict], args, stopped_early: str = "") -> None:
    st = summarize(rows)
    lines = ["# M2 案例库跑批报告", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 每案例 {args.rounds} 轮；扰动：{args.perturb or '无'}；随机种子 {args.seed}",
             "- 口径（两个都报，不擅自替用户选）：**严格**=文档里拍板的验收定义"
             "（ok 且全程没有任何 confirm_request，含脚本自带的「提示我」）；"
             "**仅异常**=只把「没找到/做完没看到」算人工介入（脚本自己设计的「提示我」不算）。"
             "两者的差值就是「脚本自带的提示我」造成的；误报 = 报成功但终态断言不成立",
             "", "| 案例 | 轮数 | 成功率 | 无人工介入(严格) | 无人工介入(仅异常) | 误报 | 定位中位 | 校准(基线/异常) | 需要人处理的轮次 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, s in st["by_case"].items():
        med = s["med_ms"]
        lines.append(f"| {name} | {s['n']} | {s['ok']}/{s['n']} ({s['ok_rate']:.0f}%) | "
                     f"{s['clean']}/{s['n']} ({s['clean_rate']:.0f}%) | "
                     f"{s['clean_manual']}/{s['n']} ({s['clean_manual_rate']:.0f}%) | "
                     f"{s['misreport']} | "
                     f"{med if med is not None else '—'} ms | "
                     f"{s['calib_first']}/{s['calib_abnormal']} | "
                     f"{s['bad_rounds'] or '—'} |")
    lines += ["", f"**合计**：{st['total']} 轮；成功率 {st['ok']}/{st['total']}"
                  f"（{st['ok_rate']:.1f}%，目标 ≥95%）；"
                  f"**无人工介入（严格口径，见口径说明）{st['clean']}/{st['total']}"
                  f"（{st['clean_rate']:.1f}%，目标 ≥90%）**；"
                  f"无人工介入（只算异常求助）{st['clean_manual']}/{st['total']}"
                  f"（{st['clean_manual_rate']:.1f}%）；"
                  f"误报 {st['misreport']}（{st['mis_rate']:.1f}%，目标 ≤2%）"]
    if stopped_early:
        lines.append(f"- ⚠ 本批**提前结束**：{stopped_early}"
                     "（以下是已跑完的轮次，可直接 `--resume` 续跑）")
    if st["slowest"]:
        lines.append("- 最慢轮次：" + "；".join(
            f"{r['case']} #{r['round']} {r['ms']:.0f}ms" for r in st["slowest"]))
    problems = [p for s in st["by_case"].values() for p in s["problems"]]
    lines += ["", "## 未达标轮次明细（每轮都能定位到轮号与原因）", ""] + (problems or ["  （无）"])
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with RAW.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n".join(lines[:16]))
    print(f"\n报告：{REPORT}\n原始：{RAW}（{len(rows)} 轮）")


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
    ap.add_argument("--fresh", action="store_true",
                    help="丢掉已有 bench_raw.jsonl 从头跑（默认续跑：跳过已完成的轮次）")
    ap.add_argument("--redo", action="store_true",
                    help="忽略选定案例已完成的轮次，重跑它们（改了案例参数后用）")
    ap.add_argument("--max-total-min", type=float, default=0.0,
                    help="本批墙钟上限（分钟）；到点停止开新轮并立刻写报告，0=不限")
    ap.add_argument("--no-shots", action="store_true",
                    help="失败轮不存截图（默认存，便于事后定位）")
    ap.add_argument("--no-evidence", action="store_true",
                    help="不采集定位证据（默认采集：供 tune.py 离线调参）")
    ap.add_argument("--max-round-s", type=float, default=180.0,
                    help="单轮墙钟上限（秒，0=不限）；超时即判该轮失败，防止一轮磨掉几十分钟")
    ap.add_argument("--max-screen-fail", type=int, default=3,
                    help="连续多少轮「屏幕不可用」就停批（锁屏/独占全屏时继续跑没意义）")
    ap.add_argument("--watchdog-s", type=float, default=600.0,
                    help="单轮硬看门狗（秒，0=关闭）：到点直接退出进程，由外层续跑；"
                         "用来兜住「卡在系统调用里」这种连超时检查都轮不到的情况")
    args = ap.parse_args()

    if args.list:
        for name, c in CASES.items():
            has = (ASSETS / f"{c['asset']}.sgscript.json").exists()
            print(f"  {name:11s} enabled={int(c['enabled'])} 资产={'有' if has else '缺'}  {c['note']}")
        return 0
    if args.gen:
        capture.init_dpi_aware()
        return {"login_full": gen_login_full, "feed": gen_feed,
                "interference": gen_interference}.get(
            args.gen, lambda: (print(f"不支持的生成目标：{args.gen}"), 2)[1])()

    capture.init_dpi_aware()
    names = [c.strip() for c in args.case.split(",") if c.strip()] or [
        n for n, c in CASES.items() if c["enabled"]]
    if args.include_real and "real" not in names:
        names.append("real")
    rng = random.Random(args.seed)
    cfg = RunConfig(guard=True, calibrate_first_run=True)
    if args.fresh:
        for p in (RAW, EVIDENCE):
            if p.exists():
                p.unlink()
                print(f"已清空旧结果：{p}")
    raw_rows = load_dedup()
    # "屏幕不可用"的轮次不算完成：下次续跑要重跑它们（否则锁屏那几轮会永久污染基线）
    rows = [r for r in raw_rows.values() if not r.get("screen_error")]
    if args.redo:                                # 重跑选定案例：旧行丢掉，避免新旧混在一份报告里
        rows = [r for r in rows if r.get("case") not in names]
    done = {row_key(r) for r in rows}
    if raw_rows:
        dropped = len(raw_rows) - len(rows)
        print(f"续跑：已有 {len(raw_rows)} 轮记录，其中 {dropped} 轮会被重跑；"
              f"其余 {len(done)} 轮跳过")
    deadline = (time.time() + args.max_total_min * 60.0) if args.max_total_min > 0 else None
    stopped_early = ""
    cons_screen_fail = 0
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
        if all((name, i) in done for i in range(1, args.rounds + 1)):
            print(f"[{name}] {args.rounds} 轮都已完成，跳过")
            continue
        # 先关别的案例窗口，**再**拉起自己的：反过来的话，如果两者共用同一个 fixture
        # （login 与 login_full 就是），刚拿到的句柄会被自己关掉。
        closed = close_other_cases(name)
        if closed:
            print(f"[{name}] 先关掉其他案例的窗口：{', '.join(closed)}"
                  f"（避免互相遮挡/抢前台）")
        hwnd = ensure_case_window(case)
        if not hwnd:
            print(f"[{name}] 窗口拉起失败，跳过")
            continue
        print(f"[{name}] 开始 {args.rounds} 轮…")
        # 资产预备：动态页补静态锚并写回脚本（没锚的话每轮都要重新校准一次）
        try:
            _sg = schema.load(ASSETS / f"{case['asset']}.sgscript.json")
            _n = ensure_anchors(_sg, hwnd)
            if _n:
                _save(_sg, case["asset"])
                print(f"[{name}] 资产补了 {_n} 个静态锚（动态页靠它定位；已写回脚本）")
        except Exception as _e:
            print(f"[{name}] 补锚跳过：{_e!r}")
        case_rows: list[dict] = []
        for i in range(1, args.rounds + 1):
            if (name, i) in done:
                continue
            if deadline and time.time() > deadline:
                stopped_early = f"到达时间上限 {args.max_total_min:g} 分钟（停在 {name} #{i}）"
                break
            ev: list = [] if not args.no_evidence else None
            wd = _arm_round_watchdog(args.watchdog_s, name, i)
            try:
                row = run_round(name, case, hwnd, i, cfg, args.perturb, rng,
                                shot=not args.no_shots, evidence=ev,
                                max_round_s=args.max_round_s)
            finally:
                if wd is not None:
                    wd.cancel()
            append_raw(row)                     # 立刻落盘：中途被抢占也不丢机时
            for s in ev:                        # 定位证据单独存（tune.py --evidence 直接吃）
                append_raw(s, EVIDENCE)
            rows.append(row)
            case_rows.append(row)
            cons_screen_fail = cons_screen_fail + 1 if row.get("screen_error") else 0
            print(f"  #{i:02d} status={row['status']} 需人处理={row['prompts_manual']}"
                  f"{row['prompt_kinds']} 断言={row['assert_ok']} calib={row['calib']} "
                  f"{row['ms']:.0f}ms"
                  f"{'  err=' + str(row['error'])[:90] if row['error'] else ''}"
                  f"{'  shot=' + row['shot'] if row['shot'] else ''}")
            if cons_screen_fail >= max(1, args.max_screen_fail):
                stopped_early = (f"连续 {cons_screen_fail} 轮屏幕不可用"
                                 f"（抓屏被拒或本轮超时，常见于锁屏/独占全屏）——"
                                 f"停在 {name} #{i}")
                break
        n_clean = sum(1 for r in case_rows
                      if r.get("status") == "ok" and not r.get("prompts_manual"))
        n_notify = sum(1 for r in case_rows if r.get("prompt_kinds"))
        if case_rows:
            print(f"[{name}] 本批 {len(case_rows)} 轮：无人工介入 {n_clean}/{len(case_rows)}"
                  f"（其中 {n_notify} 轮有脚本自带的「提示我」）")
        if stopped_early:
            break
    if rows:
        write_report(rows, args, stopped_early=stopped_early)
        if EVIDENCE.exists() and not args.no_evidence:
            n_ev = sum(1 for _ in EVIDENCE.open(encoding="utf-8"))
            print(f"定位证据：{EVIDENCE}（{n_ev} 条）→ 可跑 "
                  f"python engine/scripts/tune.py --evidence {EVIDENCE.name}")
        if stopped_early:
            print(f"\n⚠ {stopped_early}；可稍后直接重跑同命令续跑。")
    # 因屏幕不可用而停批 → 用非 0 退出码，让外层循环知道"这不是跑完了，等屏幕好了再来"
    return 4 if (stopped_early and "屏幕不可用" in stopped_early) else 0


if __name__ == "__main__":
    sys.exit(main())
