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

import ctypes
import ctypes.wintypes
import threading
import time

from engine import matcher, schema
from engine.errors import EngineError, ERRORS
from engine.logger import MemoryLogger

# 定位日志 event/method 词汇复用 locator 常量
from engine.locator import (M_ANCHOR, M_OCR_TEXT, M_PAGE_COORD, M_PAGE_TPL, M_TPL, M_UIA,
                            locate_page, locate_widget_on_screen)
from engine.locstats import summarize_detail

HUMAN_STOP = "stop"
HUMAN_SKIP = "skip"
HUMAN_CONTINUE = "continue"
# 用户知情选择："就按你记下来的位置点一次试试"（跳过点击安全闸的一次性降级）
HUMAN_COORD_ONCE = "coord_once"

# M2：失败提示里追加的"下一步建议"（白话；术语表左列词汇）
PROC_HINTS = {
    "page_not_found": "先确认那个窗口还开着，而且没有被别的窗口压住",
    "window_not_found": "这一步要操作的那个窗口现在不在屏幕上，先把它打开再运行",
    "no_page_spec": "先用“截图目标”把页面框一次，再运行",
    "widget_not_found": "如果这一块的样子变了，重新点“截图目标”框一次",
    "click_guard_failed": "这一块的内容已经和记下来的不一样了；确认没问题可以选“用记下来的位置点一次”",
}


# ---------------------------------------------------------------- 配置与协议

class RunConfig:
    def __init__(self, l1_retries=2, l1_retry_interval_s=1.0, l2_poll_interval_s=0.5,
                 l2_timeout_s=3.0, until_max=1000, repeat_max=1000, forever_max=None,
                 calibrate_first_run=False, guard=True):
        self.l1_retries = l1_retries          # L1 自动重试次数（§6.2；用户拍板改 2）
        self.l1_retry_interval_s = l1_retry_interval_s
        self.l2_poll_interval_s = l2_poll_interval_s   # 预期结果轮询间隔
        self.l2_timeout_s = l2_timeout_s               # 预期结果等待上限
        self.until_max = until_max                     # 条件循环次数上限（§5.4 默认 1000）
        self.repeat_max = repeat_max                   # 固定次数循环上限（M2：防手滑写 100000）
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

    def clear_text(self):
        """可选：清空当前输入框里的已有内容（不支持就空操作，不报错）。

        `输入文字` 动作会先调它：框里已经有字符时先清理再填入，避免变成追加
        （用户要求 2026-09-10）。
        """
        return None

    def sleep(self, seconds):
        raise NotImplementedError

    def bring_window_foreground(self, win_ctx):
        """可选：运行前把目标窗口带到前台（真实桌面联调用；默认空操作）。"""
        return None

    def raise_if_needed(self, context=None):
        """每步页面定位前的可选窗口置前（LiveDriver 内部按需节流）。

        context = 这一步记下的窗口上下文（process / title / class）。返回 None 表示"不做
        判断"，或返回 {"ok": bool, "hwnd": int, "want": str}。ok=False 表示这一步要操作的
        窗口现在不在屏幕上 —— 这种情况如果只报"识别失败"，用户完全查不出原因（2026-09-12
        的真实案例），所以单独区分出来。
        """
        return None

    def release_front(self):
        """运行结束的可选收尾：把运行期被置前的窗口恢复原状（默认空操作）。"""
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
            ans = input("选：继续 / 用记下来的位置点一次 / 跳过 / 停止 > ").strip()
            if ans in ("继续", "c", "continue", ""):
                return HUMAN_CONTINUE
            if ans in ("位置", "p", "coord"):
                return HUMAN_COORD_ONCE
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
    def __init__(self, sg, driver, cfg, loc_logger, human, calibrator, sink, stop_event=None,
                 judge=None):
        self.sg = sg
        self.driver = driver
        self.cfg = cfg
        self.log = loc_logger or MemoryLogger()
        self.human = human
        self.calibrator = calibrator          # E5 钩子（波次2接入）：(CalibRequest)->CalibResult
        self.sink = sink                      # 可选 on_row(row) 实时回调（UI 日志）
        self.stop_ev = stop_event or threading.Event()
        self.judge = judge                    # D3 结果语义判定（M3-WP3；None = 不判定）
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
                self.report["error"] = str(e) or None   # 记下原因（脚本主动"停止"时有用）
            else:
                self.report["status"] = "failed"
                self.report["error"] = str(e)
        except Exception as e:
            self.report["status"] = "failed"
            self.report["error"] = repr(e)
        finally:
            try:
                self.driver.release_front()   # 运行期置前过的窗口降回来，不长期置顶
            except Exception:
                pass
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
        # 这一步属于哪个窗口：按它自己记下的窗口上下文先把它调到前面。
        # 为什么必须"每一步"都做（2026-09-12 修）：以前只在运行前把"脚本第一步所属页面"
        # 调出来一次，跨程序的脚本（先操作 A 程序、再操作 B 程序）里 B 从头到尾没被调出来过，
        # 抓屏时看到的还是 A 的窗口 → 页面模板必然 0.0 → 用户只看到一句"识别失败"。
        front = None
        try:
            front = self.driver.raise_if_needed(spec.get("context"))
        except Exception:
            front = None
        if isinstance(front, dict) and front.get("ok") is False:
            self._loc_log(step_id, "raise_window", "window", 0.0,
                          extra={"ok": False, "reason": front.get("reason"),
                                 "want": front.get("want")})
            return {"ok": False, "reason": "window_not_found", "screen": None,
                    "want": front.get("want") or ""}
        if isinstance(front, dict) and front.get("ok") and front.get("switched"):
            # 真的换了窗口才记一笔（同窗口 2 秒内会被节流，不记）—— 这样"某一步把窗口调出来了"
            # 在日志里看得见，事后能自证修复有没有生效。
            self._loc_log(step_id, "raise_window", "window", 1.0,
                          extra={"ok": True, "want": front.get("want"),
                                 "hwnd": front.get("hwnd")})
        bgr, meta = self.driver.grab_screen()
        # 记下"目标窗口此刻怎么样了"：页面分 0.0（屏幕上根本没有它）和 0.3（窗口在、内容变了）
        # 是两种病，处置手段完全不同（调前台 vs 锚点/特征兜底），日志里必须分得开。
        try:
            from engine import capture as _cap
            ctx["win"] = _cap.window_state_for_context((spec or {}).get("context"))
        except Exception:
            ctx["win"] = None
        prev = ctx["page_rect"] if (ctx["page_rect"] and spec is ctx["page_spec"]) else None
        r = locate_page(bgr, spec, prev_hint=prev)
        self._loc_log(step_id, "locate_page", r["method"] if r["ok"] else M_PAGE_TPL,
                      r.get("confidence", 0.0), r.get("rect"), screen_meta=meta,
                      extra={"page_ok": r["ok"], "reused": r.get("reused", False),
                             "soft": bool(r.get("soft")), "sim": r.get("sim"),
                             "elapsed_ms": round(r["elapsed_ms"], 1),
                             "scale": r.get("scale", 1.0),
                             "why": self._why(r.get("detail")),
                             "win": ctx.get("win")})
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

    @staticmethod
    def _why(detail):
        """把定位器的 detail 压成"为什么"写进日志。

        为什么要记（2026-09-12）：以前只记 method/confidence/level，真实失败到底是
        "没认出这个词"、"模板分不够"还是"被旁边的文字门槛拒了"，事后完全看不出来，
        只能靠猜。这里提炼成一小段，统计脚本与界面都能直接用；出任何问题都不影响运行。
        """
        try:
            return summarize_detail(detail)
        except Exception as e:                  # 提炼失败绝不能让运行崩
            return {"reason": "summarize_error", "note": repr(e)}

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
        if act == "stop":
            # L3 显式条件常见用法："如果看到'用户名或密码错误' → 停止"（分析文档 §6.2）
            self._loc_log(step_id, "script_stop", "action", 0.0, extra={"reason": "stop"})
            raise EngineError("stop_requested", "脚本里设置了“停止”这一步")
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
            # 过程性失败（window / page / widget / guard）
            reason = attempt["reason"]
            want = attempt.get("want") or ""
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
            if reason == "window_not_found":
                msg = (f"这一步要操作的窗口（{want}）现在不在屏幕上，可能被关掉了"
                       if want else "这一步要操作的窗口现在不在屏幕上，可能被关掉了")
            elif reason == "no_page_spec":
                # 真实反馈（2026-09-12）：这一步压根没记住页面，却报成"没找到这个东西"，
                # 用户会去目标软件里到处找一个其实没必要找的东西。
                msg = f"这一步（“{text}”）没记住页面，不知道该在哪儿找它"
            elif reason == "page_not_found":
                msg = "没找到这个界面（操作页面），请确认窗口是否已打开"
            hint = PROC_HINTS.get(reason)
            if hint:                                # M2：除了"没找到"，再给一句下一步建议
                msg = f"{msg}（{hint}）"
            choice = self.human.prompt_not_found(msg, text)
            if choice == HUMAN_STOP:
                raise EngineError("stop_requested", ERRORS["stop_requested"])
            if choice == HUMAN_SKIP:
                return self._row(status="skipped", label=f"跳过：{msg}", **row_meta)
            if choice == HUMAN_COORD_ONCE:
                forced = self._attempt_by_coord(st, ctx, path, target, act, params)
                if forced is not None:
                    return forced
                # 没有位置信息 → 说清楚，再弹一次让用户重新选
                self.human.notify("这一步没记下位置（框选时没框住页面），没法按位置点")
                continue
            retries_left = self.cfg.l1_retries     # 用户处理后续跑（继续）

    def _clear_before_type(self):
        """输入前清空框内已有内容（用户要求：框里有字符时先清理再填入，而不是追加）。"""
        clear = getattr(self.driver, "clear_text", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass

    def _attempt_by_coord(self, st, ctx, path, target, act, params):
        """按"录下来的位置"直接点一次——**只**在用户弹窗里明确选择时才走。

        跳过点击安全闸：这是用户知情的降级（"这次先按位置点一下试试"），只发一次；
        失败/无效就回到弹窗，不会自动反复点。
        """
        pr, rip = ctx.get("page_rect"), target.get("rect_in_page")
        if not pr or not rip or len(rip) < 4:
            return None
        s = ctx.get("page_scale") or 1.0
        cx = pr[0] + int(round((rip[0] + rip[2] / 2) * s))
        cy = pr[1] + int(round((rip[1] + rip[3] / 2) * s))
        if not (pr[0] <= cx <= pr[0] + pr[2] and pr[1] <= cy <= pr[1] + pr[3]):
            return None
        step_id = st.get("id", path)
        self._loc_log(step_id, "coord_forced", "page_coord", 0.0, (cx, cy, 1, 1),
                      extra={"at": [cx, cy], "by": "user_choice"})
        if act == "type":
            self.driver.click(cx, cy)
            self._clear_before_type()                 # 同样先清理再填入
            self.driver.type_text(str(params.get("text", "")))
            self.report["counters"]["types"] += 1
            label = f"按记下来的位置点了一下并输入文字 “{params.get('text', '')}”"
        else:
            self.driver.click(cx, cy, dbl=(act == "dblclick"))
            self.report["counters"]["clicks"] += 1
            label = "按记下来的位置点了一下"
        return self._row(status="ok", label=label, method="page_coord", level=3,
                         confidence=0.0, forced=True, path=path, step_id=step_id,
                         type="action", action=act)

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
            return {"kind": "proc", "reason": page.get("reason") or "page_not_found",
                    "screen": page.get("screen"), "want": page.get("want")}
        pr = page["rect"]
        screen = page["screen"]
        lw = locate_widget_on_screen(screen, pr, target, page_scale=page["scale"],
                                     uia_provider=self._uia())
        lw_extra = {"ok": lw["ok"], "level": lw.get("level"),
                    "elapsed_ms": round(lw["elapsed_ms"], 1)}
        # 候选留痕（用户审查要求）：默认把前 3 个文字候选（盒/分数/距离/邻居）写进定位日志
        cands_top = ((lw.get("detail") or {}).get("l2_ocr") or {}).get("top3")
        if cands_top:
            lw_extra["top3"] = cands_top
        # 2️⃣ 失败/成功都说清"为什么"（哪层试过、被谁拦下、分数多少），供统计与事后复盘
        lw_extra["why"] = self._why(lw.get("detail"))
        lw_extra["win"] = ctx.get("win")      # 同一步的窗口状态（页面定位时取的）
        self._loc_log(step_id, "locate_widget", lw.get("method") or "none",
                      lw.get("confidence", 0.0), lw.get("box"), extra=lw_extra)
        if not lw["ok"]:
            return {"kind": "proc", "reason": "widget_not_found", "screen": screen}
        click_pt = lw["center"]
        elapsed_loc = lw["elapsed_ms"]
        # 点击安全闸（M0 实证：校验不过不点；防误点页面外/动态区域）
        if act in ("click", "dblclick", "type") and self.cfg.guard:
            g = self._click_guard(target, click_pt, pr, box=lw.get("box"))
            self._loc_log(step_id, "click_guard", g["method"], g.get("score", 0.0),
                          rect=tuple(g.get("patch") or (0, 0, 0, 0)),
                          extra={"ok": g["ok"]})
            if not g["ok"]:
                return {"kind": "proc", "reason": "click_guard_failed", "screen": screen,
                        "guard": g}
        else:
            g = {"ok": True, "method": "guard_off"}
        if act in ("click", "dblclick", "type"):
            if act == "type":
                self.driver.click(*click_pt)          # 聚焦输入框（点一下）
                self._clear_before_type()             # 框里已有字符 → 先清理再填入
                self.driver.type_text(str(params.get("text", "")))
                self.report["counters"]["types"] += 1
                label = f"输入文字 “{params.get('text', '')}”"
            else:
                self.driver.click(*click_pt, dbl=(act == "dblclick"))
                self.report["counters"]["clicks"] += 1
                label = "点两下" if act == "dblclick" else "点一下"
        else:
            label = ""
        # 第 3 层（页面内坐标）是"没认出目标、直接按记下来的位置点"的兜底路径。
        # 位置可能早就变了，必须在运行日志里看得见 —— 2026-09-12 用户的真实反馈：
        # 真实程序上它常常一声不响地按位置点空，界面上却只显示"点一下"。
        if lw.get("method") == M_PAGE_COORD:
            note = "（没认出目标，是按记下来的位置点的，位置可能已经变了）"
            label = f"{label}{note}" if label else note.strip("（）")
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

    def _click_guard(self, target, click_pt, page_rect, box=None):
        """点击前：在**部件框 + 边距**范围内复验目标特征（防状态已变/误点）。

        用户审查：原先固定取"点击点 ±210×60"，控件比这块大时只验到一部分；
        现在以定位得到的部件框为准（box 缺省时回落到点击点中心区域，兼容旧调用）。

        复验三道：① 目标文字 OCR ② 整块模板 ③ 环带模板（只比外圈边框/底色，
        输入框被填过数据后占位文字没了、整块图案也变了，但边框没变）。
        """
        if box and len(box) >= 4 and box[2] > 0 and box[3] > 0:
            pad_x = min(160, int(box[2] * 0.4) + 40)
            pad_y = min(60, int(box[3] * 0.6) + 24)
            gx, gy = max(0, box[0] - pad_x), max(0, box[1] - pad_y)
            gw = min(720, box[2] + 2 * pad_x)
            gh = min(320, box[3] + 2 * pad_y)
        else:
            gx, gy = max(0, click_pt[0] - 210), max(0, click_pt[1] - 60)
            gw, gh = 420, 120
        patch = self.driver.grab_rect((gx, gy, gw, gh))
        if patch is None:
            return {"ok": False, "method": "no_patch", "score": 0.0,
                    "patch": [gx, gy, gw, gh]}
        text = (target.get("text") or "").strip()
        if text:
            f = matcher.find_text_ocr(patch, matcher.text_needle_short(text), upsample=2)
            if f["ok"]:
                return {"ok": True, "method": "ocr_patch", "score": f["score"],
                        "matched": f.get("matched_text"), "patch": [gx, gy, gw, gh]}
        if target.get("image"):
            tpl = matcher.dataurl_to_bgr(target["image"])
            m = matcher.find_template(patch, tpl, scales=(1.0, 0.75), score_thr=0.55)
            if m["ok"]:
                return {"ok": True, "method": "tpl_fallback", "score": m["score"],
                        "patch": [gx, gy, gw, gh]}
            r = matcher.find_template_ring(patch, tpl, score_thr=0.60)
            if r["ok"]:
                return {"ok": True, "method": "tpl_ring", "score": r["score"],
                        "patch": [gx, gy, gw, gh]}
        return {"ok": False, "method": "ocr_patch", "score": 0.0, "patch": [gx, gy, gw, gh]}

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

    def _intent_of(self, st, eo) -> str:
        """把这一步"本来想做什么"说成人话，交给 D3 对齐预期。"""
        act = {"click": "点一下", "dblclick": "点两下", "type": "输入文字",
               "hotkey": "按快捷键"}.get(st.get("action"), str(st.get("action") or "操作"))
        tw = ((st.get("target") or {}).get("text") or "").strip()
        ow = (((eo or {}).get("target") or {}).get("text") or "").strip()
        parts = [f"{act}「{tw}」" if tw else act]
        if ow:
            parts.append(f"做完后应该看到「{ow}」")
        return "，".join(parts)

    def _semantic_hint(self, st, eo) -> str:
        """结果没出来时，问一句"这是哪一类失败"（D3）。

        失败提示原来只能说"做完后没看到预期结果"，用户还得自己看出到底是密码错
        还是断网。这里补一句人话原因；判不出来就**什么都不加**——宁可不说话，
        也不能给一个猜的原因（用户会照着它去修错的东西）。

        本地能判就不问云端（judge 内部保证），云端未授权时同样安静降级。
        """
        if self.judge is None:
            return ""
        try:
            screen = None
            g = self.driver.grab_screen()
            screen = g[0] if isinstance(g, tuple) else g
            v = self.judge.judge(screen, self._intent_of(st, eo),
                                 local={"ok": False, "reason": "timeout"})
        except Exception:
            return ""
        kind = v.get("kind")
        if kind in (None, "not_found", "unknown"):
            return ""                       # 说"没出现"等于没说，不如不说
        self._loc_log(st.get("id", "?"), "semantic_outcome", f"d3:{v.get('source')}",
                      extra={"kind": kind, "intent": v.get("intent", ""),
                             "ms": v.get("elapsed_ms")})
        return f"（看起来是：{v.get('label')}）"

    def _handle_outcome_fail(self, st, ctx, path, attempt, eo, row_meta):
        """结果性失败 → on_fail 策略（retry/notify/stop/skip；不触发校验入口，清单 §3）。"""
        of = (eo or {}).get("on_fail") or {}
        strategy = of.get("strategy", "notify")
        msg = of.get("message") or "做完后没看到预期结果"
        msg = msg + self._semantic_hint(st, eo)
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
        """条件目标所属页面还没出现时的分支选择。

        M2 修正：按条件语义决定，而不是一律走"否则"——
          · "如果没看到 X"：页面都没出现，那当然就是**没看到** → 走 then
            （这正是分析文档 §6.2 的用法："如果没看到'登录成功' → 重复 3 次点击登录"）
          · "如果看到 X"：还没出现 = 没看到 → 走 else
        如实记录（页面错由后续动作步提示）。
        """
        step_id = st.get("id", path)
        cond = st.get("condition") or {}
        want = cond.get("exists", True)
        branch = "else" if want else "then"
        row = self._row(path=path, step_id=step_id, type="condition",
                        status="fail", label=f"无法判断（界面未找到），走“{'就做' if branch == 'then' else '否则'}”",
                        error="page_not_found", branch=branch)
        self._exec_seq(st.get(branch) or [], ctx, f"{path}{step_id}▶{branch}")
        return row
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
            asked = n
            if self.cfg.repeat_max and n > self.cfg.repeat_max:
                n = int(self.cfg.repeat_max)       # 软上限：防手滑写成大数把电脑跑飞（M2）
                self._row(status="iter", iterations=0,
                          label=f"重复次数 {asked} 超过上限，按 {n} 次执行", **row_meta)
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
               stop_event=None, judge=None):
    """解释执行脚本。返回 RunReport dict（steps 含每行白话 label/状态）。
    - human=None 且触达提示/失败弹窗 → EngineError(stop_requested)（无界面无人值守默认停止）。
    - calibrator：E5 校验模式入口（波次2 接入）。
    - stop_event：外部停止标志（Ctrl+C/UI 停止按钮；runner.stop() 语义一致）。
    - judge：D3 结果语义判定（M3-WP3）；给定后失败提示会带上"看起来是哪一类失败"。
    """
    problems = schema.validate(sg)
    if problems:
        raise EngineError("schema_invalid", ERRORS["schema_invalid"], {"errors": problems})
    if human is None:
        human = _SilentHuman()
    runner = _Runner(sg, driver, cfg or RunConfig(), loc_logger, human, calibrator, sink,
                     stop_event=stop_event, judge=judge)
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


def _unicode_key_events(text):
    """生成"Unicode 直发"的按键事件序列 [(wScan, dwFlags)]（纯函数，便于测试）。

    KEYEVENTF_UNICODE 把字符直接交给窗口，**不经过输入法**；非 BMP 字符按代理对发。
    """
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    out = []
    for ch in str(text):
        code = ord(ch)
        if code <= 0xFFFF:
            units = [code]
        else:
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        for u in units:
            out.append((u, KEYEVENTF_UNICODE))
            out.append((u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return out


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    """必须留着：INPUT 是"三选一"的联合体，**大小由 MOUSEINPUT 决定**。

    x64 上 MOUSEINPUT 是 32 字节（末尾 dwExtraInfo 是 8 字节指针并对齐），
    所以 sizeof(INPUT) 应该是 40；只按 KEYBDINPUT（24 字节）算出来是 32，
    SendInput 会因为 cbSize 不对**直接返回 0**（曾因此静默回退到按键序列，
    于是中文输入法又把输入拦坏——表现为"输入 demo 变成拼音候选"）。
    """

    _fields_ = [("dx", ctypes.wintypes.LONG), ("dy", ctypes.wintypes.LONG),
                ("mouseData", ctypes.wintypes.DWORD), ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.wintypes.DWORD), ("wParamL", ctypes.wintypes.WORD),
                ("wParamH", ctypes.wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUTUNION)]


def send_unicode_text(text, per_char_delay=0.02) -> int:
    """用 SendInput(KEYEVENTF_UNICODE) 输入文字——绕开中文输入法（IME）。

    为什么不用 pynput 的按键序列（2026-09-10 真人实测）：中文输入法会把 "demo"
    拦成拼音组合，输入框里出现的是拼音/候选，**输入内容本身就是错的**；
    同时输入法候选框会浮在页面上，让整窗模板匹配掉到 0（页面"找不到"）。
    Unicode 直发把字符直接给窗口，不触发候选框，中文/符号/任意布局都能准确输入。
    """
    user32 = ctypes.windll.user32
    # 64 位下 INPUT 必须是 40 字节；不对的话 SendInput 直接返回 0（见 _MOUSEINPUT 注释）
    cb = ctypes.sizeof(_INPUT)
    sent = 0
    for scan, flags in _unicode_key_events(text):
        inp = _INPUT(type=1,  # INPUT_KEYBOARD
                     u=_INPUTUNION(ki=_KEYBDINPUT(0, scan, flags, 0, None)))
        if user32.SendInput(1, ctypes.byref(inp), cb) != 1:
            raise EngineError("input_failed", "输入文字失败（SendInput 被拒绝）")
        if scan and flags == 0x0004:
            sent += 1
    # 按字符节奏稍作停顿（有些输入框对瞬时连续字符会丢字）
    time.sleep(max(0.0, per_char_delay) * max(1, len(str(text))))
    return sent


class LiveDriver(ScreenDriver):
    """真实桌面驱动：mss 截图 + pynput 输入 + capture 窗口前置（m0lib 原语迁移）。"""

    def __init__(self, win_ctx=None):
        self.win_ctx = win_ctx           # {"hwnd": int} 或 {"title": 子串}：运行前窗口置前
        self._raised_at = 0.0
        self._front_hwnd = 0             # 运行期被我置前过的窗口（跑完降回来）
        self._front_key = None           # 上一次的窗口上下文，用于节流
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

    @staticmethod
    def _want_label(ctx) -> str:
        """给用户看的窗口名：优先标题（太长就截断），退而用进程名。"""
        title = str((ctx or {}).get("title") or "").strip()
        proc = str((ctx or {}).get("process") or "").strip()
        if title:
            return title if len(title) <= 24 else title[:24] + "…"
        return proc or "目标窗口"

    def ensure_front(self, context=None):
        """把这一步要操作的窗口调到前台（按该步记下的窗口上下文解析）。

        用进程名优先解析：浏览器内核应用的标题会随当前页面变（哔哩哔哩就是例子），
        只用标题找会找不到。同一个窗口 2 秒内不重复置前，避免每步都去抢前台。
        返回 None（没上下文，不做判断）或 {"ok", "hwnd", "want", ...}。
        """
        from engine import capture
        ctx = context if isinstance(context, dict) and context else self.win_ctx
        if not ctx:
            return None
        now = time.time()
        key = (str(ctx.get("process") or ""), str(ctx.get("class") or ""),
               str(ctx.get("title") or ""))
        if key == self._front_key and now - self._raised_at < 2.0:
            return {"ok": bool(self._front_hwnd), "hwnd": self._front_hwnd,
                    "want": self._want_label(ctx), "kept": True}
        hwnd = self._resolve_hwnd() if ctx is self.win_ctx else 0
        if not hwnd:
            hwnd = capture.find_window_for_context(ctx)
        self._front_key = key
        self._raised_at = now
        if not hwnd:
            self._front_hwnd = 0
            return {"ok": False, "hwnd": 0, "reason": "window_not_found",
                    "want": self._want_label(ctx)}
        prev, self._front_hwnd = self._front_hwnd, int(hwnd)
        capture.bring_to_foreground(hwnd)   # 内含 SW_RESTORE：最小化会还原
        return {"ok": True, "hwnd": int(hwnd), "want": self._want_label(ctx),
                "switched": prev not in (0, int(hwnd))}

    def _raise_window(self):
        self.ensure_front(None)

    def raise_if_needed(self, context=None):
        self.ensure_front(context)

    def release_front(self):
        """把自己运行期置前过的窗口降回来（初始窗口由 ipc 那边负责降）。"""
        from engine import capture
        hwnd, self._front_hwnd = getattr(self, "_front_hwnd", 0) or 0, 0
        if hwnd:
            try:
                capture.demote_window(hwnd)
            except Exception:
                pass

    def click(self, x, y, dbl=False):
        from pynput.mouse import Button, Controller
        m = Controller()
        m.position = (int(x), int(y))
        time.sleep(0.05)
        m.click(Button.left, 2 if dbl else 1)

    def type_text(self, text):
        """输入文字：优先 Unicode 直发（绕开中文输入法），失败再回退按键序列。"""
        text = str(text)
        try:
            send_unicode_text(text)
            return
        except Exception:
            pass
        from pynput.keyboard import Controller as K
        k = K()
        for ch in text:
            k.press(ch)
            k.release(ch)
            time.sleep(0.02)

    def clear_text(self, clear_key=None):
        """清空输入框已有内容：全选（Ctrl+A）后删除。

        `输入文字` 动作前调用（用户要求：框内有字符时先清理再填入）。用 Ctrl 组合键
        而不是退格循环：中文输入法不会拦 Ctrl 组合，也不受框内文字长度影响。
        """
        from pynput.keyboard import Controller as K, Key
        k = K()
        try:
            with k.pressed(Key.ctrl):
                k.press("a")
                k.release("a")
            time.sleep(0.05)
            k.press(Key.delete)
            k.release(Key.delete)
            time.sleep(0.05)
        except Exception:
            pass

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
