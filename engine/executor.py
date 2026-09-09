# -*- coding: utf-8 -*-
"""
engine.executor — E4：解释执行 .sgscript.json（六动作 × 条件 × 循环 + L1/L2）。

规格：《M1_引擎设计清单》§3 运行闭环状态机、§2/E4；《项目分析文档》v0.31 §5.2~5.4/§6.2~6.3。

语义要点（波次1 定稿，测试在 engine/tests/test_executor.py）：
  - 每步先定位页面（整窗模板→锚）再定位部件，全部操作只发生在页面范围内；
  - 过程性错误（页面/部件找不到、点击前校验不过）→ 若配置了校验钩子(calibrator=E5)
    则进校验入口；否则/校验后仍失败 → L1 自动重试 N 次 → 白话提示（继续/跳过/停止）；
  - 结果性失败（预期结果未出现）→ 走 expected_outcome.on_fail（重试/提示/停止/跳过），
    绝不触发校验入口；
  - 点击安全闸（M0 实证有效）：点击前在点击点周边做文字/模板复验，不过闸不发送点击；
  - 首次执行可配置进一次校验（cfg.calibrate_first_run，默认关，UI 产品侧开启）。

ScreenDriver 抽象：引擎与屏幕/输入解耦（合成屏可离线测；LiveDriver 接真实桌面）。
HumanIO 抽象：提示我 / L1 / L2 失败弹窗（CLI 终端实现；后续 Electron 用 IPC 实现）。
"""
from __future__ import annotations

import threading
import time

from engine import matcher, schema
from engine.errors import EngineError, ERRORS
from engine.logger import MemoryLogger

# 定位日志 event/method 词汇复用 locator 常量
from engine.locator import (M_ANCHOR, M_OCR_TEXT, M_PAGE_COORD, M_PAGE_TPL, M_TPL, M_UIA,
                            locate_page, locate_widget_on_screen)

HUMAN_STOP = "stop"
HUMAN_SKIP = "skip"
HUMAN_CONTINUE = "continue"


# ---------------------------------------------------------------- 配置与协议

class RunConfig:
    def __init__(self, l1_retries=3, l1_retry_interval_s=1.0, l2_poll_interval_s=0.5,
                 l2_timeout_s=3.0, until_max=1000, forever_max=None,
                 calibrate_first_run=False, guard=True):
        self.l1_retries = l1_retries          # L1 自动重试次数（§6.2 默认 3）
        self.l1_retry_interval_s = l1_retry_interval_s
        self.l2_poll_interval_s = l2_poll_interval_s   # 预期结果轮询间隔
        self.l2_timeout_s = l2_timeout_s               # 预期结果等待上限
        self.until_max = until_max                     # 条件循环次数上限（§5.4 默认 1000）
        self.forever_max = forever_max                 # 无限循环安全上限（None=仅停止标志）
        self.calibrate_first_run = calibrate_first_run
        self.guard = guard                             # 点击安全闸总开关


class ScreenDriver:
    """屏幕/输入驱动协议。live 实现在本模块底部；测试用 Fake 实现在 engine/tests。"""

    def grab_screen(self):
        """返回 (BGR ndarray 全屏, meta dict{width,height,monitor,dpi?})"""
        raise NotImplementedError

    def grab_rect(self, rect):
        """按屏幕矩形截图（默认实现：全屏裁剪）。"""
        bgr, _ = self.grab_screen()
        x, y, w, h = [int(v) for v in rect]
        hh, ww = bgr.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(ww, x + w), min(hh, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return bgr[y0:y1, x0:x1]

    def click(self, x, y, dbl=False):
        raise NotImplementedError

    def type_text(self, text):
        raise NotImplementedError

    def hotkey(self, keys):
        raise NotImplementedError

    def sleep(self, seconds):
        raise NotImplementedError

    def bring_window_foreground(self, win_ctx):
        """可选：运行前把目标窗口带到前台（真实桌面联调用；默认空操作）。"""
        return None

    def raise_if_needed(self):
        """每步页面定位前的可选窗口置前（LiveDriver 内部按需节流）。"""
        return None

    def uia_provider(self):
        """可选：UI 树提供者 uia_provider(text)->hits（①级）；None = 无 UIA 上下文。"""
        return None


class HumanIO:
    """用户交互抽象。产品词一律白话（术语表左列）。"""

    def notify(self, message: str) -> None:
        """“提示我”步骤：暂停等待用户处理，点继续后返回。"""
        raise NotImplementedError

    def prompt_not_found(self, message: str, target_text: str) -> str:
        """L1 兜底弹窗（没找到 X）→ continue / skip / stop。"""
        raise NotImplementedError

    def prompt_outcome_fail(self, message: str) -> str:
        """L2 失败（预期结果未出现，on_fail=提示）→ continue / stop。"""
        raise NotImplementedError


class ConsoleHuman(HumanIO):
    """终端实现（CLI 用）。选项用白话短词。"""

    def notify(self, message: str) -> None:
        print(f"【提示我】{message}")
        input("处理完成后按回车继续…")

    def prompt_not_found(self, message: str, target_text: str) -> str:
        print(f"【没找到】{message}")
        while True:
            ans = input("选：继续 / 跳过 / 停止 > ").strip()
            if ans in ("继续", "c", "continue", ""):
                return HUMAN_CONTINUE
            if ans in ("跳过", "s", "skip"):
                return HUMAN_SKIP
            if ans in ("停止", "t", "stop"):
                return HUMAN_STOP

    def prompt_outcome_fail(self, message: str) -> str:
        print(f"【做完后没看到】{message}")
        while True:
            ans = input("选：继续 / 停止 > ").strip()
            if ans in ("继续", "c", "continue", ""):
                return HUMAN_CONTINUE
            if ans in ("停止", "t", "stop"):
                return HUMAN_STOP


# ---------------------------------------------------------------- 运行器

class _Runner:
    def __init__(self, sg, driver, cfg, loc_logger, human, calibrator, sink, stop_event=None):
        self.sg = sg
        self.driver = driver
        self.cfg = cfg
        self.log = loc_logger or MemoryLogger()
        self.human = human
        self.calibrator = calibrator          # E5 钩子（波次2接入）：(CalibRequest)->CalibResult
        self.sink = sink                      # 可选 on_row(row) 实时回调（UI 日志）
        self.stop_ev = stop_event or threading.Event()
        self.report = {"script": sg.get("name", ""), "started_at": None, "finished_at": None,
                       "status": "ok", "steps": [], "calib": [], "counters":
                           {"clicks": 0, "types": 0, "hotkeys": 0, "notifies": 0,
                            "l1_prompts": 0, "loc_ms_total": 0.0}}
        self.ctx = {"page_spec": None, "page_rect": None, "page_scale": 1.0,
                    "page_method": None}
        self._seq = [0]

    # -------- 基础设施
    def stop(self):
        self.stop_ev.set()

    def _stopped(self) -> bool:
        return self.stop_ev.is_set()

    def _row(self, **kw) -> dict:
        row = {"seq": self._seq[0], **kw}
        self._seq[0] += 1
        self.report["steps"].append(row)
        if self.sink:
            try:
                self.sink(row)
            except Exception:
                pass
        return row

    def _loc_log(self, step_id, event, method, confidence=0.0, rect=None, extra=None,
                 screen_meta=None):
        if screen_meta is None:
            try:
                _, meta = self.driver.grab_screen()
            except Exception:
                meta = {}
        else:
            meta = screen_meta
        row = {"step_id": step_id, "event": event, "method": method,
               "confidence": round(float(confidence or 0), 4),
               "rect": list(rect) if rect else None,
               "screen_meta": {"width": meta.get("width"), "height": meta.get("height"),
                               "dpi": meta.get("dpi")}}
        if extra:
            row.update(extra)
        self.log.log_loc(row)

    # -------- 顶层
    def run(self) -> dict:
        self.report["started_at"] = _now_iso()
        try:
            if self.cfg.calibrate_first_run and self.calibrator:
                self._first_run_calib()
            if self._stopped():
                self.report["status"] = "stopped"
                return self.report
            self._exec_seq(self.sg["steps"], ctx=self.ctx, path="")
        except EngineError as e:
            if e.code == "stop_requested":
                self.report["status"] = "stopped"
            else:
                self.report["status"] = "failed"
                self.report["error"] = str(e)
        except Exception as e:
            self.report["status"] = "failed"
            self.report["error"] = repr(e)
        self.report["finished_at"] = _now_iso()
        return self.report

    def _first_run_calib(self):
        """首次执行必进一次校验（建立基线；清单 §3）。失败只记录，不阻断首次运行。"""
        try:
            res = self.calibrator({"reason": "first_run", "step": None,
                                   "target": None, "page_spec": self.ctx["page_spec"],
                                   "loc_rows": self.log.tail(None, 50),
                                   "ctx": dict(self.ctx)})
            self.report["calib"].append({"reason": "first_run", **res})
            if res.get("updated") and res.get("page_spec"):
                self._adopt_page(res["page_spec"])
        except Exception as e:
            self.report["calib"].append({"reason": "first_run", "ok": False, "error": repr(e)})

    def _adopt_page(self, spec):
        self.ctx["page_spec"] = spec
        self.ctx["page_rect"] = None
        self.ctx["page_scale"] = 1.0

    def _exec_seq(self, steps, ctx, path):
        for st in steps:
            if self._stopped():
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            self._exec_step(st, ctx, path)
        return True

    def _exec_step(self, st, ctx, path):
        sid = st.get("id", "?")
        here = f"{path}{sid}" if not path else f"{path}▶{sid}"
        typ = st.get("type")
        if typ == "action":
            return self._exec_action(st, ctx, here)
        if typ == "condition":
            return self._exec_condition(st, ctx, here)
        if typ == "loop":
            return self._exec_loop(st, ctx, here)
        raise EngineError("schema_invalid", f"未知步骤类型 {typ}")

    # -------- 页面解析/锁定
    def _page_spec_of(self, target, ctx):
        """步骤用页面 = target.page（显式）或当前沿用页（§5.1 页面自动沿用）。"""
        if isinstance(target, dict) and isinstance(target.get("page"), dict):
            return target["page"]
        return ctx["page_spec"]

    def _locate_step_page(self, target, ctx, step_id):
        """重新定位步骤页面；返回 {ok, rect, method, scale, confidence} 或抛/记录失败。"""
        spec = self._page_spec_of(target, ctx)
        if spec is None:
            return {"ok": False, "reason": "no_page_spec"}
        self.driver.raise_if_needed()
        bgr, meta = self.driver.grab_screen()
        prev = ctx["page_rect"] if (ctx["page_rect"] and spec is ctx["page_spec"]) else None
        r = locate_page(bgr, spec, prev_hint=prev)
        self._loc_log(step_id, "locate_page", r["method"] if r["ok"] else M_PAGE_TPL,
                      r.get("confidence", 0.0), r.get("rect"), screen_meta=meta,
                      extra={"page_ok": r["ok"], "reused": r.get("reused", False),
                             "elapsed_ms": round(r["elapsed_ms"], 1),
                             "scale": r.get("scale", 1.0)})
        self.report["counters"]["loc_ms_total"] += r["elapsed_ms"]
        if r["ok"]:
            ctx["page_spec"] = spec
            ctx["page_rect"] = r["rect"]
            ctx["page_scale"] = r.get("scale", 1.0)
            ctx["page_method"] = r["method"]
            return {"ok": True, "rect": r["rect"], "method": r["method"],
                    "scale": r.get("scale", 1.0), "confidence": r.get("confidence", 0.0),
                    "screen": bgr}
        return {"ok": False, "reason": "page_not_found", "screen": bgr,
                "detail": r.get("detail", {})}

    def _uia(self):
        """当前运行可用的 UI 树提供者（driver 支持且窗口上下文就绪时）。"""
        try:
            p = self.driver.uia_provider()
            return p if callable(p) else None
        except Exception:
            return None

    def _exists(self, target, ctx, step_id, screen_bgr=None):
        """目标是否存在（条件/循环/预期结果共用；③ 页面内坐标不参与“看到”）。"""
        spec = self._page_spec_of(target, ctx)
        if spec is None or ctx["page_rect"] is None:
            return {"exists": False, "reason": "no_page"}
        if screen_bgr is None:
            screen_bgr, _ = self.driver.grab_screen()
        r = locate_widget_on_screen(screen_bgr, ctx["page_rect"], target,
                                    exists=True, page_scale=ctx["page_scale"],
                                    uia_provider=self._uia())
        self._loc_log(step_id, "exists_check", r["method"] or "none",
                      r.get("confidence", 0.0), r.get("box"),
                      extra={"found": r["ok"], "level": r.get("level"),
                             "elapsed_ms": round(r["elapsed_ms"], 1)})
        return {"exists": r["ok"], "found": r, "reason": ""}

    # -------- action
    def _exec_action(self, st, ctx, path):
        act = st.get("action")
        params = st.get("params") or {}
        step_id = st.get("id", path)
        if act == "notify":
            msg = params.get("message") or "请处理当前情况"
            self.human.notify(msg)
            self.report["counters"]["notifies"] += 1
            row = self._row(path=path, step_id=step_id, type="action", action=act,
                            status="ok", label=msg)
            return row
        if act == "wait":
            secs = float(params.get("seconds", 1))
            self.driver.sleep(secs)
            return self._row(path=path, step_id=step_id, type="action", action=act,
                             status="ok", label=f"等一下 {secs} 秒")
        if act == "hotkey":
            keys = str(params.get("keys", ""))
            self.driver.hotkey(keys)
            self.report["counters"]["hotkeys"] += 1
            return self._row(path=path, step_id=step_id, type="action", action=act,
                             status="ok", label=f"按快捷键 {keys}")

        # 需要部件的动作：click / dblclick / type（点击类默认落在部件中心，§5.1）
        need_click = act in ("click", "dblclick", "type")
        st_row = self._run_need_target(st, ctx, path, need_click, act, params)
        return st_row

    def _run_need_target(self, st, ctx, path, need_click, act, params):
        """目标动作循环：正常路径 → 过程性失败(校验入口→L1 重试→提示) → 结果性失败(on_fail)。"""
        step_id = st.get("id", path)
        target = st.get("target") or {}
        row_meta = dict(path=path, step_id=step_id, type="action", action=act)
        retries_left = self.cfg.l1_retries
        calib_done = False
        while True:
            if self._stopped():
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            attempt = self._attempt_need_target(st, ctx, path, target, act, params)
            if attempt["kind"] == "ok":
                return self._row(status="ok", label=attempt["label"],
                                 method=attempt.get("method"),
                                 level=attempt.get("level"),
                                 confidence=attempt.get("confidence"),
                                 elapsed_ms=attempt.get("elapsed_ms"),
                                 verify=attempt.get("verify"), **row_meta)
            if attempt["kind"] == "outcome":
                eo = st.get("expected_outcome") or {}
                decision = self._handle_outcome_fail(st, ctx, path, attempt, eo, row_meta)
                return decision
            # 过程性失败（page/widget/guard）
            reason = attempt["reason"]
            self._loc_log(step_id, "procedural_fail", "ai", extra={"reason": reason})
            if self.calibrator and not calib_done:
                calib_done = True
                tgt = target if isinstance(target, dict) else {}
                tgt_page = tgt.get("page") if isinstance(tgt.get("page"), dict) else None
                res = self._call_calibrator({"reason": reason, "step": st,
                                             "target": tgt,
                                             "page_spec": tgt_page or ctx["page_spec"],
                                             "page_rect": ctx["page_rect"],
                                             "loc_rows": self.log.tail(step_id, 30),
                                             "ctx": dict(ctx)})
                if res.get("updated") and res.get("page_spec"):
                    self._adopt_page(res["page_spec"])
                    continue          # 校验写回 → 立即重试该步（不消耗 L1 重试）
            if retries_left > 0:
                retries_left -= 1
                self.driver.sleep(self.cfg.l1_retry_interval_s)
                continue
            # L1 白话提示（§6.2：重试仍失败 → 暂停提示）
            self.report["counters"]["l1_prompts"] += 1
            text = (target.get("text") or "该部件").strip() or "该部件"
            msg = f"没找到“{text}”，请确认屏幕上有没有这个东西"
            if reason == "page_not_found":
                msg = "没找到这个界面（操作页面），请确认窗口是否已打开"
            choice = self.human.prompt_not_found(msg, text)
            if choice == HUMAN_STOP:
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            if choice == HUMAN_SKIP:
                return self._row(status="skipped", label=f"跳过：{msg}", **row_meta)
            retries_left = self.cfg.l1_retries     # 用户处理后续跑（继续）

    def _call_calibrator(self, req) -> dict:
        try:
            res = self.calibrator(req) or {}
        except Exception as e:
            res = {"ok": False, "error": repr(e)}
        if res.get("updated"):
            # 校验写回：脚本内部 rev bump（同批写回标记，清单 §4；持久化由调用方决定）
            self.sg["targets_rev"] = int(self.sg.get("targets_rev", 0) or 0) + 1
        self.report["calib"].append({"reason": req["reason"], **res})
        return res

    def _attempt_need_target(self, st, ctx, path, target, act, params):
        step_id = st.get("id", path)
        t0 = time.perf_counter()
        page = self._locate_step_page(target, ctx, step_id)
        if not page["ok"]:
            return {"kind": "proc", "reason": "page_not_found", "screen": page.get("screen")}
        pr = page["rect"]
        screen = page["screen"]
        lw = locate_widget_on_screen(screen, pr, target, page_scale=page["scale"],
                                     uia_provider=self._uia())
        self._loc_log(step_id, "locate_widget", lw.get("method") or "none",
                      lw.get("confidence", 0.0), lw.get("box"),
                      extra={"ok": lw["ok"], "level": lw.get("level"),
                             "elapsed_ms": round(lw["elapsed_ms"], 1)})
        if not lw["ok"]:
            return {"kind": "proc", "reason": "widget_not_found", "screen": screen}
        click_pt = lw["center"]
        elapsed_loc = lw["elapsed_ms"]
        # 点击安全闸（M0 实证：校验不过不点；防误点页面外/动态区域）
        if act in ("click", "dblclick", "type") and self.cfg.guard:
            g = self._click_guard(target, click_pt, pr)
            self._loc_log(step_id, "click_guard", g["method"], g.get("score", 0.0),
                          rect=(max(0, click_pt[0] - 210), max(0, click_pt[1] - 60),
                                420, 120),
                          extra={"ok": g["ok"]})
            if not g["ok"]:
                return {"kind": "proc", "reason": "click_guard_failed", "screen": screen,
                        "guard": g}
        else:
            g = {"ok": True, "method": "guard_off"}
        if act in ("click", "dblclick", "type"):
            if act == "type":
                self.driver.click(*click_pt)          # 聚焦输入框（点一下）
                self.driver.type_text(str(params.get("text", "")))
                self.report["counters"]["types"] += 1
                label = f"输入文字 “{params.get('text', '')}”"
            else:
                self.driver.click(*click_pt, dbl=(act == "dblclick"))
                self.report["counters"]["clicks"] += 1
                label = "点两下" if act == "dblclick" else "点一下"
        else:
            label = ""
        # L2 预期结果校验（动作后应看到 X；§6.2）
        eo = st.get("expected_outcome")
        if eo:
            res = self._verify_outcome(st, ctx, path, eo)
            if not res["ok"]:
                return {"kind": "outcome", "screen": screen, "eo": res}
            return {"kind": "ok", "label": label, "method": lw["method"],
                    "level": lw.get("level"), "confidence": lw.get("confidence"),
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "verify": {**g, "outcome": "seen"}}
        return {"kind": "ok", "label": label, "method": lw["method"],
                "level": lw.get("level"), "confidence": lw.get("confidence"),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "verify": g}

    def _click_guard(self, target, click_pt, page_rect):
        """点击前：点击点周边（±210×120）应仍含目标文字/图像，防状态已变/误点。"""
        x0 = max(0, click_pt[0] - 210)
        y0 = max(0, click_pt[1] - 60)
        patch = self.driver.grab_rect((x0, y0, 420, 120))
        if patch is None:
            return {"ok": False, "method": "no_patch", "score": 0.0}
        text = (target.get("text") or "").strip()
        if text:
            f = matcher.find_text_ocr(patch, matcher.text_needle_short(text), upsample=2)
            if f["ok"]:
                return {"ok": True, "method": "ocr_patch", "score": f["score"],
                        "matched": f.get("matched_text")}
        if target.get("image"):
            tpl = matcher.dataurl_to_bgr(target["image"])
            m = matcher.find_template(patch, tpl, scales=(1.0, 0.75), score_thr=0.55)
            if m["ok"]:
                return {"ok": True, "method": "tpl_fallback", "score": m["score"]}
        return {"ok": False, "method": "ocr_patch", "score": 0.0}

    def _verify_outcome(self, st, ctx, path, eo):
        """动作后等待“预期结果出现”：轮询 exists（页面沿用/快速复验）。"""
        step_id = st.get("id", path)
        deadline = time.perf_counter() + self.cfg.l2_timeout_s
        otarget = eo.get("target") or {}
        # 预期结果页 = 动作目标页显式页 或沿用当前页
        page_spec = otarget.get("page") or self._page_spec_of(st.get("target"), ctx)
        seen = False
        last = None
        while True:
            if self._stopped():
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            screen, meta = self.driver.grab_screen()
            # 页面沿用：以当前页矩形快速定位（prev_hint 同矩形复验），失败则全量
            if page_spec is None or ctx["page_rect"] is None:
                last = {"exists": False, "reason": "no_page"}
                break
            pr = ctx["page_rect"]
            r = locate_page(screen, page_spec, prev_hint=pr)
            if not r["ok"]:
                r = locate_page(screen, page_spec)
                if r["ok"]:
                    ctx["page_rect"] = r["rect"]
                    pr = r["rect"]
                    ctx["page_scale"] = r.get("scale", 1.0)
                    ctx["page_spec"] = page_spec   # 页面已切换（如登录后跳转首页）
                else:
                    last = {"exists": False, "reason": "page_gone"}
                    self._loc_log(step_id, "outcome_check", M_PAGE_TPL,
                                  extra={"seen": False, "reason": "page_gone"})
                    if time.perf_counter() >= deadline:
                        break
                    self.driver.sleep(self.cfg.l2_poll_interval_s)
                    continue
            last = self._exists(otarget, ctx, step_id, screen_bgr=screen)
            if last["exists"]:
                seen = True
                break
            if time.perf_counter() >= deadline:
                break
            self.driver.sleep(self.cfg.l2_poll_interval_s)
        if not seen:
            # 超时后再确认一次（轮询间隔粒度导致的上限误差）
            self._loc_log(step_id, "outcome_check", "ocr_text" if otarget.get("text") else "tpl",
                          extra={"seen": False, "reason": last.get("reason", "timeout")})
            return {"ok": False, "last": last, "reason": last.get("reason", "timeout")}
        self._loc_log(step_id, "outcome_check", last.get("found", {}).get("method") or "tpl",
                      last.get("found", {}).get("confidence", 0.0), last.get("found", {}).get("box"),
                      extra={"seen": True})
        return {"ok": True, "last": last}

    def _handle_outcome_fail(self, st, ctx, path, attempt, eo, row_meta):
        """结果性失败 → on_fail 策略（retry/notify/stop/skip；不触发校验入口，清单 §3）。"""
        of = (eo or {}).get("on_fail") or {}
        strategy = of.get("strategy", "notify")
        msg = of.get("message") or "做完后没看到预期结果"
        if strategy == "retry":
            times = ((of.get("retry") or {}).get("times") or 3) - 1
            failed_at = 1
            for i in range(times):
                if self._stopped():
                    raise EngineError("stop_requested", ERRORS["stop_requested"])
                self.driver.sleep(self.cfg.l2_poll_interval_s)
                r = self._attempt_need_target(st, ctx, path, st.get("target") or {},
                                              st.get("action"), st.get("params") or {})
                failed_at = i + 2
                if r["kind"] == "ok":
                    return self._row(status="ok", label=f"{row_meta['action']}（重试后成功）",
                                     method=r.get("method"), level=r.get("level"),
                                     confidence=r.get("confidence"), **row_meta)
                if r["kind"] == "proc":
                    continue   # 重试轮的过程性失败按一次失败计数，继续下一轮
            # retry 耗尽 → 提示（白话告知），供用户决定
            choice = self.human.prompt_outcome_fail(
                f"{msg}（已尝试 {failed_at} 次仍没等到）")
            if choice == HUMAN_STOP:
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            return self._row(status="fail", label=msg, error="verify_failed", **row_meta)
        if strategy == "notify":
            choice = self.human.prompt_outcome_fail(msg)
            if choice == HUMAN_STOP:
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            return self._row(status="fail", label=msg, error="verify_failed", **row_meta)
        if strategy == "stop":
            self._row(status="fail", label=msg, error="verify_failed", **row_meta)
            raise EngineError("stop_requested", f"{ERRORS['verify_failed']}：{msg}")
        if strategy == "skip":
            return self._row(status="fail", label=msg, error="verify_failed", **row_meta)
        return self._row(status="fail", label=msg, error="verify_failed", **row_meta)

    # -------- condition / loop
    def _exec_condition(self, st, ctx, path):
        step_id = st.get("id", path)
        cond = st.get("condition") or {}
        target = cond.get("target") or {}
        want = cond.get("exists", True)
        page_res = self._locate_step_page(target, ctx, step_id)
        if not page_res["ok"]:
            return self._condition_escalate(st, ctx, path, page_res)
        chk = self._exists(target, ctx, step_id, screen_bgr=page_res["screen"])
        got = chk["exists"]
        branch = "then" if got == want else "else"
        row = self._row(path=path, step_id=step_id, type="condition", status="ok",
                        label=("如果看到" if want else "如果没看到") + f"“{target.get('text', '')}”",
                        branch=branch, exists=got)
        self._exec_seq(st.get(branch) or [], ctx, f"{path}{step_id}▶{branch}")
        return row

    def _condition_escalate(self, st, ctx, path, page_res):
        """条件页定位失败：无页面可查 → 走“否则”分支并如实记录（页面错由后续动作步提示）。"""
        step_id = st.get("id", path)
        row = self._row(path=path, step_id=step_id, type="condition",
                        status="fail", label="无法判断（界面未找到），走“否则”",
                        error="page_not_found", branch="else")
        self._exec_seq(st.get("else") or [], ctx, f"{path}{step_id}▶else")
        return row

    def _exec_loop(self, st, ctx, path):
        step_id = st.get("id", path)
        lp = st.get("loop") or {}
        mode = lp.get("mode")
        body = st.get("body") or []
        row_meta = dict(path=path, step_id=step_id, type="loop")
        if mode == "count":
            n = int(lp.get("count", 0))
            for i in range(n):
                if self._stopped():
                    raise EngineError("stop_requested", ERRORS["stop_requested"])
                self._row(status="iter", iteration=i, label=f"重复 {n} 次（第 {i + 1}/{n}）",
                          **row_meta)
                self._exec_seq(body, ctx, f"{path}{step_id}▶{i}")
            return self._row(status="ok", iterations=n, label=f"重复 {n} 次完成", **row_meta)
        if mode == "until":
            target = lp.get("target") or {}
            want = lp.get("exists", True)
            i = 0
            while i < self.cfg.until_max:
                if self._stopped():
                    raise EngineError("stop_requested", ERRORS["stop_requested"])
                pr = self._locate_step_page(target, ctx, step_id)
                seen = False
                if pr["ok"]:
                    chk = self._exists(target, ctx, step_id, screen_bgr=pr["screen"])
                    seen = chk["exists"] == want
                # 页面还没出现（如登录后跳转中）→ 视作“还没看到”，继续循环
                if seen:
                    return self._row(status="ok", iterations=i,
                                     label=f"等到“{target.get('text', '')}”出现（共 {i} 次）",
                                     **row_meta)
                self._exec_seq(body, ctx, f"{path}{step_id}▶{i}")
                i += 1
            self._row(status="fail", error="loop_limit", iterations=i,
                      label=f"一直重复到上限（{self.cfg.until_max} 次）还没等到",
                      **row_meta)
            return None
        if mode == "forever":
            i = 0
            while not self._stopped():
                if self.cfg.forever_max is not None and i >= self.cfg.forever_max:
                    break
                self._exec_seq(body, ctx, f"{path}{step_id}▶{i}")
                i += 1
            hit_max = not self._stopped() and self.cfg.forever_max is not None \
                and i >= self.cfg.forever_max
            self._row(status="stopped" if self._stopped() else "ok",
                      iterations=i,
                      label=("手动停止" if self._stopped()
                             else f"达到安全上限（{self.cfg.forever_max} 次）"),
                      **row_meta)
            if self._stopped():
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            return None
        raise EngineError("schema_invalid", f"未知循环模式 {mode}")


# ---------------------------------------------------------------- 对外 API

def run_script(sg, driver, cfg=None, loc_logger=None, human=None, calibrator=None, sink=None,
               stop_event=None):
    """解释执行脚本。返回 RunReport dict（steps 含每行白话 label/状态）。
    - human=None 且触达提示/失败弹窗 → EngineError(stop_requested)（无界面无人值守默认停止）。
    - calibrator：E5 校验模式入口（波次2 接入）。
    - stop_event：外部停止标志（Ctrl+C/UI 停止按钮；runner.stop() 语义一致）。
    """
    problems = schema.validate(sg)
    if problems:
        raise EngineError("schema_invalid", ERRORS["schema_invalid"], {"errors": problems})
    if human is None:
        human = _SilentHuman()
    runner = _Runner(sg, driver, cfg or RunConfig(), loc_logger, human, calibrator, sink,
                     stop_event=stop_event)
    return runner.run()


class _SilentHuman(HumanIO):
    """无界面兜底：任何需要用户的地方都按“停止”处理（无人值守安全默认）。"""

    def notify(self, message):
        raise EngineError("stop_requested", f"需要人工介入（{message}）但没有交互界面")

    def prompt_not_found(self, message, target_text):
        raise EngineError("stop_requested", f"需要人工确认（{message}）但没有交互界面")

    def prompt_outcome_fail(self, message):
        raise EngineError("stop_requested", f"需要人工确认（{message}）但没有交互界面")


# ---------------------------------------------------------------- Live 驱动（真实桌面）

_KEY_NAMES = {
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd",
    "esc": "esc", "enter": "enter", "return": "enter", "tab": "tab", "space": "space",
    "backspace": "backspace", "delete": "delete", "del": "delete", "up": "up",
    "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
    "pageup": "page_up", "pagedown": "page_down",
}
for _i in range(1, 13):
    _KEY_NAMES[f"f{_i}"] = f"f{_i}"
for _i in range(10):
    _KEY_NAMES[str(_i)] = f"{_i}"


def parse_hotkey(keys: str) -> list:
    """'ctrl+shift+s' → [按键...]（pynput 命名）。"""
    from pynput.keyboard import Key
    parts = [p.strip().lower() for p in str(keys).split("+") if p.strip()]
    out = []
    for p in parts:
        if p in _KEY_NAMES:
            name = _KEY_NAMES[p]
            out.append(getattr(Key, name, name))
        else:
            out.append(p)
    return out


class LiveDriver(ScreenDriver):
    """真实桌面驱动：mss 截图 + pynput 输入 + capture 窗口前置（m0lib 原语迁移）。"""

    def __init__(self, win_ctx=None):
        self.win_ctx = win_ctx           # {"hwnd": int} 或 {"title": 子串}：运行前窗口置前
        self._raised_at = 0.0
        self._hwnd = None

    def _resolve_hwnd(self):
        """解析窗口句柄（win_ctx.hwnd 优先，否则按标题查找；缓存）。"""
        if self._hwnd is not None:
            return self._hwnd
        from engine import capture
        hwnd = (self.win_ctx or {}).get("hwnd") or 0
        if not hwnd and self.win_ctx and self.win_ctx.get("title"):
            ws = capture.find_windows_by_title(self.win_ctx["title"])
            hwnd = ws[0] if ws else 0
        self._hwnd = hwnd or 0
        return self._hwnd

    def window_rect(self):
        """窗口矩形 (x,y,w,h)（校准 page_lost 需要页面原点）；无窗口上下文 → None。
        注意：GetWindowRect 返回 (x,y,x2,y2)，此处统一换算为 w/h 语义。"""
        import win32gui
        hwnd = self._resolve_hwnd()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return (int(l), int(t), int(r - l), int(b - t))

    def uia_provider(self):
        """UI 树提供者（①级）：窗口上下文就绪才可用。"""
        from engine import uia
        hwnd = self._resolve_hwnd()
        if not hwnd:
            return None
        return uia.make_provider(hwnd)

    def grab_screen(self):
        from engine.capture import dpi_of, grab_screen as gs
        bgr = gs()
        h, w = bgr.shape[:2]
        return bgr, {"width": w, "height": h, "dpi": dpi_of(), "monitor": "live"}

    def grab_rect(self, rect):
        from engine.capture import grab_screen as gs
        x, y, w, h = [int(v) for v in rect]
        mm = None
        import mss
        with mss.mss() as s:
            mm = s.monitors[0]
        l, t = mm["left"], mm["top"]
        rx = max(l, x); ry = max(t, y)
        rw = min(x + w, mm["left"] + mm["width"]) - rx
        rh = min(y + h, mm["top"] + mm["height"]) - ry
        if rw <= 0 or rh <= 0:
            return None
        return gs((rx, ry, rw, rh))

    def _raise_window(self):
        from engine import capture
        if not self.win_ctx:
            return
        now = time.time()
        if now - self._raised_at < 2.0:
            return
        hwnd = self._resolve_hwnd()
        if hwnd:
            capture.bring_to_foreground(hwnd)
            time.sleep(0.8)
            self._raised_at = now

    def raise_if_needed(self):
        self._raise_window()

    def click(self, x, y, dbl=False):
        from pynput.mouse import Button, Controller
        m = Controller()
        m.position = (int(x), int(y))
        time.sleep(0.05)
        m.click(Button.left, 2 if dbl else 1)

    def type_text(self, text):
        from pynput.keyboard import Controller as K
        k = K()
        for ch in str(text):
            k.press(ch)
            k.release(ch)
            time.sleep(0.02)

    def hotkey(self, keys):
        from pynput.keyboard import Controller as K
        k = K()
        combo = parse_hotkey(keys)
        for key in combo:
            k.press(key)
        for key in reversed(combo):
            k.release(key)

    def sleep(self, seconds):
        time.sleep(max(0.0, float(seconds)))


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="milliseconds")
