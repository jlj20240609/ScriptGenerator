# -*- coding: utf-8 -*-
"""
engine.ipc — M1 引擎 IPC 服务端（Electron UI ↔ Python 引擎，stdio JSON-Lines）。

契约单一来源：`docs/M1_IPC契约.md` v0.1（JSON-RPC 2.0 子集；坐标物理像素）。
启动：`python -m engine serve`（stdout 只走协议，日志走 stderr）。

要点：
  - 双截图采集：page.capture（框页面 → 窗口上下文 + 页面截图）→ widget.capture
    （框部件 → OCR 文字 + UIA hit-test + 部件模板），返回可直接组步骤的 Target；
  - 运行：script.run 异步（事件推送 event.step/log/run_done），script.stop 停止；
  - 校验模式：options.calibrate → Calibrator（ai=off/stub/cloud；cloud 需 ai.authorize
    显式授权），校准过程发 event.calibrate；
  - 人工确认（HumanIO 落点）：引擎发 event.confirm_request（白话），UI 回 confirm.reply；
    超时按“停止”处理（无人值守安全默认）。

可测试性：driver 工厂 / 屏幕抓取 / UIA hit-test / 确认应答 均可注入（见 engine/tests/test_ipc.py）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from engine import ai as ai_mod
from engine import capture, locator, matcher, schema, uia
from engine.calibrator import Calibrator
from engine.errors import EngineError
from engine.executor import LiveDriver, RunConfig, run_script
from engine.logger import LocLogger

PROTOCOL_VERSION = "1"
CONFIRM_TIMEOUT_S = 120.0

RPC_PARSE_ERROR = -32700
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_ENGINE_ERROR = 4001
RPC_BUSY = 4002
RPC_UNAUTHORIZED = 4003


class _Error(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def _err(code, message, data=None):
    return _Error(code, message, data)


class IpcServer:
    """JSON-Lines RPC 服务端。serve() 阻塞读 stdin；亦可用 handle_line() 单测。"""

    def __init__(self, driver_factory=None, grab=None, hit_test=None,
                 context_from_point=None, loc_log_path=None,
                 confirm_timeout_s=CONFIRM_TIMEOUT_S, activate=None, demote=None):
        self._driver_factory = driver_factory or (lambda win_ctx: LiveDriver(win_ctx))
        self._grab = grab or capture.grab_screen
        self._hit_test = hit_test or uia.hit_test
        self._context_from_point = context_from_point or capture.context_from_point
        # 运行前“把目标页面切到前台”；测试注入记录器，默认走真实 WinAPI。
        # 运行期间保持置顶（别的置顶窗压着会把点击送到错误的窗口），跑完降回。
        self._activate = activate or (lambda title: self._m_window_activate(
            {"title": title, "topmost_keep": True}))
        self._demote = demote or capture.demote_window
        self._out_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closing = False
        self._seq = 0
        self._page = None            # 最近一次 page.capture 的内部上下文
        self._run = None             # 当前运行 {id, stop, thread, report}
        self._vlm = None             # 云端 AI（ai.authorize 后可用）
        self._pending_confirm = {}   # request_id -> {"event": Event, "choice": None}
        self._confirm_auto = None    # 测试钩子：callable(kind,message,options)->choice
        self._confirm_timeout_s = confirm_timeout_s
        self._loc_log_path = Path(loc_log_path) if loc_log_path else \
            Path(tempfile.gettempdir()) / "m1_ui_loc.jsonl"
        self._methods = {
            "ping": self._m_ping,
            "window.find": self._m_window_find,
            "window.activate": self._m_window_activate,
            "script.new": self._m_script_new,
            "script.load": self._m_script_load,
            "script.save": self._m_script_save,
            "page.capture": self._m_page_capture,
            "widget.capture": self._m_widget_capture,
            "page.find": self._m_page_find,
            "widget.locate": self._m_widget_locate,
            "input.click": self._m_input_click,
            "input.type": self._m_input_type,
            "input.hotkey": self._m_input_hotkey,
            "script.run": self._m_script_run,
            "script.stop": self._m_script_stop,
            "ai.authorize": self._m_ai_authorize,
            "confirm.reply": self._m_confirm_reply,
            "engine.shutdown": self._m_shutdown,
        }

    # ---------------------------------------------------------------- 输出

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def _log(self, text: str, level: str = "info") -> None:
        self.notify("event.log", {"level": level, "text": text, "ts": _now()})

    # ---------------------------------------------------------------- 主循环

    def serve(self) -> int:
        capture.init_dpi_aware()
        try:
            sys.stdin.reconfigure(encoding="utf-8-sig", errors="replace")
        except Exception:
            pass
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            resp = self.handle_line(line)
            if resp is not None:
                self.send(resp)
            if self._closing:
                break
        self._stop_run()
        return 0

    def handle_line(self, line: str):
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": RPC_PARSE_ERROR, "message": f"JSON 解析失败: {e}"}}
        return self.handle(req)

    def handle(self, req: dict):
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not method:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": RPC_INVALID_PARAMS, "message": "缺 method"}}
        fn = self._methods.get(method)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": RPC_METHOD_NOT_FOUND,
                              "message": f"未知方法 {method}"}}
        try:
            result = fn(params) or {}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except _Error as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": e.code, "message": e.message, "data": e.data}}
        except EngineError as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": RPC_ENGINE_ERROR, "message": str(e),
                              "data": {"code": e.code, "details": e.details}}}
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            sys.stderr.write(tb)
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": RPC_ENGINE_ERROR,
                              "message": f"{type(e).__name__}: {e}"}}

    # ---------------------------------------------------------------- 基础方法

    def _m_ping(self, p):
        return {"ok": True, "engine_version": _version(), "protocol": PROTOCOL_VERSION,
                "spec": "M1-清单-v0.1 / 分析文档 v0.31 §11", "dpi": capture.dpi_of()}

    def _m_window_find(self, p):
        """按标题子串列出顶层窗口（物理像素 rect = 左上宽高）。

        UI 选区需要知道目标窗口在屏幕上的物理位置，才能把鼠标框选的
        DIP 矩形换算成引擎用的物理像素矩形。
        """
        title = (p.get("title") or "").strip()
        if not title:
            raise _err(RPC_INVALID_PARAMS, "缺 title（标题子串）")
        vx, vy, vw, vh = capture.virtual_screen_rect()
        out = []
        for hwnd in capture.find_windows_by_title(title):
            try:
                l, t, r, b = capture.window_rect(hwnd)
            except Exception:
                continue
            if r - l < 8 or b - t < 8:
                continue
            minimized = capture.is_iconic(hwnd)
            # 最小化窗口落在屏幕外（如 -25600），无法框选 → 不作为可选目标
            if r <= vx or b <= vy or l >= vx + vw or t >= vy + vh:
                continue
            out.append({
                "hwnd": int(hwnd),
                "rect": [int(l), int(t), int(r - l), int(b - t)],
                "title": capture.window_title(hwnd),
                "class": capture.window_class(hwnd),
                "process": capture.process_name_of(hwnd),
                "minimized": minimized,
            })
        return {"ok": True, "windows": out}

    def _m_window_activate(self, p):
        """把目标窗口切到前台（运行/截图前用：点按落在被遮挡窗口上会点错地方）。

        hwnd 或 title 二者其一；找不到窗口返回 ok=False（不报错，交给 L1 处理）。
        topmost_keep=True 时保持置顶（运行期间用，跑完由调用方降回），否则借完前台
        立刻取消置顶。返回里带 occluded/top_title：窗口中心点上压着的还是别的窗口
        （例如另一个置顶窗）时，调用方能给出"被挡住"的白话提示。
        """
        hwnd = int(p.get("hwnd") or 0)
        keep = bool(p.get("topmost_keep", False))
        if not hwnd:
            title = (p.get("title") or "").strip()
            if not title:
                raise _err(RPC_INVALID_PARAMS, "缺 hwnd 或 title")
            cands = [h for h in capture.find_windows_by_title(title)
                     if not capture.is_iconic(h)]
            if not cands:
                return {"ok": False, "hwnd": 0, "reason": "not_found"}
            hwnd = cands[0]
        ok = bool(capture.bring_to_foreground(hwnd))
        if ok and not keep:
            time.sleep(0.3)
            capture.demote_window(hwnd)      # 只借前台，不长期置顶
        res = {"ok": ok, "hwnd": int(hwnd)}
        try:
            r = capture.window_rect(hwnd)
            top = capture.window_from_point(r[0] + (r[2] - r[0]) // 2,
                                            r[1] + (r[3] - r[1]) // 2)
            top = capture.root_window(int(top)) if top else 0      # 子窗口归一到顶层
            if top and top != int(hwnd):
                res["occluded"] = True
                res["top_title"] = capture.window_title(top)
                res["top_class"] = capture.window_class(top)
                res["top_process"] = capture.process_name_of(top)
        except Exception:
            pass
        return res

    def _m_script_new(self, p):
        sg = schema.new_script(name=(p.get("name") or "未命名脚本").strip() or "未命名脚本")
        return {"script": sg}

    def _m_script_load(self, p):
        path = _req(p, "path")
        return {"script": schema.load(path)}

    def _m_script_save(self, p):
        path = _req(p, "path")
        sg = p.get("script")
        if not isinstance(sg, dict):
            raise _err(RPC_INVALID_PARAMS, "缺 script 对象")
        schema.check(sg)
        schema.dump(sg, path)
        return {"ok": True, "path": str(path),
                "targets_rev": int(sg.get("targets_rev", 0) or 0)}

    # ---------------------------------------------------------------- 双截图采集

    # 输入法候选框的窗口类/进程特征（它没有标题，是最常见的"挡住页面"的元凶）
    _IME_HINTS = ("IME", "MSCTFIME UI", "Windows.UI.Core.CoreWindow", "TextInputHost")

    def _occlusion_note(self, hwnd) -> None:
        """页面中心压着别的窗口时给一条白话提醒（输入法候选框最常见）。"""
        try:
            r = capture.window_rect(hwnd)
            top = capture.window_from_point(r[0] + (r[2] - r[0]) // 2,
                                            r[1] + (r[3] - r[1]) // 2)
            top = capture.root_window(int(top)) if top else 0
            if not top or top == int(hwnd):
                return
            cls = capture.window_class(top) or ""
            title = capture.window_title(top) or ""
            proc = capture.process_name_of(top) or ""
            if (not title) or any(h in cls for h in self._IME_HINTS) \
                    or any(h in proc for h in self._IME_HINTS):
                self._log("屏幕上有个输入法候选框挡着，先按 Esc 关掉它再试一次", "warn")
            else:
                self._log('操作页面上方压着别的窗口（"%s"），点按可能会点错地方' % title,
                          "warn")
        except Exception:
            pass

    def _borrow_front(self, rect):
        """把"占这块区域最多"的窗口借到前台（返回 hwnd 供调用方用完取消置顶）。

        屏幕抓取看到的是压在最上面的窗口：这块区域要是被别的窗口盖着（小屏上构建器
        压住演示页很常见），定位/截图都会拿到别的窗口的画面。只在真实采集路径下做
        （测试注入时跳过）。
        """
        if self._context_from_point is not capture.context_from_point:
            return 0
        try:
            hwnd, cover = capture.window_for_rect(rect)
            if not hwnd or cover < 0.3:
                return 0
            if int(capture.fg_window_info().get("hwnd") or 0) != hwnd:
                capture.bring_to_foreground(hwnd)
                time.sleep(0.35)
            if cover < 0.85:
                self._log("这段区域有 %d%% 被别的窗口压着，识别可能不准"
                          % int(round((1 - cover) * 100)), "warn")
            self._occlusion_note(hwnd)
            return int(hwnd)
        except Exception:
            return 0

    @staticmethod
    def _return_front(hwnd):
        if not hwnd:
            return
        try:
            capture.demote_window(hwnd)      # 借完就还，不长期置顶
        except Exception:
            pass

    def _m_page_capture(self, p):
        rect = _rect_param(p, "rect")
        borrowed = self._borrow_front(rect) if p.get("activate", True) else 0
        try:
            return self._page_capture_inner(p, rect)
        finally:
            self._return_front(borrowed)

    def _page_capture_inner(self, p, rect):
        x, y, w, h = rect
        ctx = self._context_from_point(x + w // 2, y + h // 2)
        way = (p.get("grab") or "screen").lower()
        if way == "window" and ctx.get("hwnd"):
            bgr = capture.grab_window_content(ctx["hwnd"])
            if bgr is None:
                bgr = self._grab(rect)
        else:
            bgr = self._grab(rect)
        if bgr is None or getattr(bgr, "size", 0) == 0:
            raise _err(RPC_ENGINE_ERROR, "页面截图失败（区域越界或被遮挡）")
        spec = capture.make_page_spec(
            bgr=bgr, rect_in_screen=rect,
            context={"process": ctx.get("process", ""), "title": ctx.get("title", ""),
                     "class": ctx.get("class", "")},
            dpi=capture.dpi_of(ctx.get("hwnd") or None))
        with self._state_lock:
            self._page = {"spec": spec, "bgr": bgr, "rect": rect,
                          "hwnd": ctx.get("hwnd", 0), "context": ctx}
        self._log(f"已框住操作页面（{w}×{h}）")
        return {"ok": True, "page": spec, "hwnd": ctx.get("hwnd", 0)}

    def _m_widget_capture(self, p):
        with self._state_lock:
            page = self._page
        if page is None:
            raise _err(RPC_ENGINE_ERROR, "请先框住操作页面（page.capture）")
        wrect = _rect_param(p, "rect_in_page")
        ph, pw = page["bgr"].shape[:2]
        x, y, w, h = wrect
        if x < 0 or y < 0 or x + w > pw or y + h > ph:
            raise _err(RPC_INVALID_PARAMS, "部件区域越出页面范围")
        crop = capture.crop_rect(page["bgr"], wrect)
        if crop is None or crop.size == 0:
            raise _err(RPC_INVALID_PARAMS, "部件区域越出页面范围")
        # OCR 识别文字（回显“已识别：xx”；小块自动放大，避免把“登录”切成单字）
        text = (p.get("text") or "").strip()
        ocr = matcher.ocr_run_auto(crop)
        others = [str(t).strip() for t in ocr.get("txts", []) if str(t).strip()]
        if not text and others:
            text = max(others, key=len)
        # UIA hit-test（点=页面原点+部件中心）
        uia_info = {}
        px = page["rect"][0] + wrect[0] + wrect[2] // 2
        py = page["rect"][1] + wrect[1] + wrect[3] // 2
        try:
            hit = self._hit_test(px, py) or {}
        except Exception:
            hit = {}
        if hit.get("name") or hit.get("automation_id"):
            uia_info = {"name": hit.get("name", ""),
                        "automation_id": hit.get("automation_id", "")}
        x, y, w, h = wrect
        # 相对锚点（邻居文字）：录制时把目标四周的静态文字一起记下来，运行时用于消歧
        nearby = _collect_nearby(page["bgr"], wrect, text)
        target = schema.widget_target(
            image_dataurl=matcher.bgr_to_dataurl(crop), text=text, match="auto",
            rect_in_page=[x, y, w, h], center_in_page=[x + w // 2, y + h // 2],
            uia=uia_info or None, nearby=nearby or None)
        if page["context"].get("title"):
            self._log(f"已识别：{text or '（无文字）'}")
        if nearby:
            self._log(f"顺手记住旁边 {len(nearby)} 处文字（以后用来认准它）")
        return {"ok": True, "target": target, "page": page["spec"],
                "ocr_others": others[:8], "nearby": nearby}

    # ---------------------------------------------------------------- 定位/输入（调试）

    def _m_page_find(self, p):
        page = p.get("page") or (self._page or {}).get("spec")
        if not isinstance(page, dict):
            raise _err(RPC_INVALID_PARAMS, "缺 page")
        # 定位也是"看屏幕"：先把页面窗口借到前面，否则看到的是压在它上面的窗口
        pr = page.get("rect_in_screen")
        borrowed = self._borrow_front(pr) if (p.get("activate", True) and pr) else 0
        try:
            screen, _meta = self._grab(), None
            r = locator.locate_page(screen, page)
        finally:
            self._return_front(borrowed)
        return {"ok": r["ok"], "rect": r.get("rect"), "method": r.get("method"),
                "confidence": r.get("confidence"), "scale": r.get("scale"),
                "elapsed_ms": round(r.get("elapsed_ms", 0), 1)}

    def _m_widget_locate(self, p):
        page_rect = _rect_param(p, "page_rect")
        target = p.get("target")
        if not isinstance(target, dict):
            raise _err(RPC_INVALID_PARAMS, "缺 target")
        borrowed = self._borrow_front(page_rect) if p.get("activate", True) else 0
        try:
            screen = self._grab()
            r = locator.locate_widget_on_screen(screen, tuple(page_rect), target,
                                                exists=bool(p.get("exists")))
        finally:
            self._return_front(borrowed)
        return {"ok": r["ok"], "level": r.get("level"), "method": r.get("method"),
                "box": r.get("box"), "center": r.get("center"),
                "confidence": r.get("confidence"),
                "elapsed_ms": round(r.get("elapsed_ms", 0), 1)}

    def _live(self):
        if not hasattr(self, "_live_driver"):
            self._live_driver = self._driver_factory(None)
        return self._live_driver

    def _m_input_click(self, p):
        self._live().click(int(p.get("x", 0)), int(p.get("y", 0)),
                           dbl=bool(p.get("dbl")))
        return {"ok": True}

    def _m_input_type(self, p):
        self._live().type_text(str(p.get("text", "")))
        return {"ok": True}

    def _m_input_hotkey(self, p):
        self._live().hotkey(str(p.get("keys", "")))
        return {"ok": True}

    # ---------------------------------------------------------------- 运行

    def _m_script_run(self, p):
        with self._state_lock:
            if self._run and self._run["thread"].is_alive():
                raise _err(RPC_BUSY, "已有脚本在运行")
            if isinstance(p.get("script"), dict):
                sg = p["script"]
            elif p.get("path"):
                sg = schema.load(p["path"])
            else:
                raise _err(RPC_INVALID_PARAMS, "缺 script 或 path")
            schema.check(sg)
            opts = p.get("options") or {}
            self._seq += 1
            run_id = f"r{self._seq}"
            stop_ev = threading.Event()
            self._run = {"id": run_id, "stop": stop_ev, "thread": None,
                         "report": None}
        thread = threading.Thread(target=self._run_script, args=(run_id, sg, opts),
                                  daemon=True)
        with self._state_lock:
            self._run["thread"] = thread
        thread.start()
        return {"run_id": run_id}

    def _m_script_stop(self, p):
        self._stop_run()
        return {"ok": True}

    def _stop_run(self):
        with self._state_lock:
            run = self._run
        if run and run["stop"] is not None:
            run["stop"].set()

    def _run_script(self, run_id: str, sg: dict, opts: dict):
        with self._state_lock:
            run = self._run
        stop_ev = run["stop"]
        human = _IpcHuman(self, run_id)
        driver = self._driver_factory(None)
        cfg = RunConfig(guard=bool(opts.get("guard", True)))
        calibrator = None
        if opts.get("calibrate"):
            ai_obj = self._make_ai(opts.get("ai", "stub"), run_id)
            calibrator = _Calibrated(self, Calibrator(
                driver, ai=ai_obj, window_rect=driver.window_rect), run_id)
            cfg.calibrate_first_run = bool(opts.get("first_run_calibrate", True))
        logger = LocLogger(opts.get("loc_log") or self._loc_log_path)
        self.notify("event.run_state", {"run_id": run_id, "state": "running"})
        self._log("开始运行脚本")
        activated = self._activate_target(sg, opts)

        def sink(row):
            self.notify("event.step", {
                "run_id": run_id, "seq": row.get("seq"), "step_id": row.get("step_id"),
                "path": row.get("path"), "status": row.get("status"),
                "label": row.get("label", ""), "method": row.get("method"),
                "level": row.get("level"), "confidence": row.get("confidence"),
                "elapsed_ms": row.get("elapsed_ms")})

        try:
            rep = run_script(sg, driver, cfg=cfg, loc_logger=logger, human=human,
                             calibrator=calibrator, sink=sink, stop_event=stop_ev)
        except EngineError as e:
            rep = {"status": "stopped", "steps": [], "counters": {}, "error": str(e),
                   "calib": []}
        except Exception as e:
            sys.stderr.write(traceback.format_exc(limit=4))
            rep = {"status": "failed", "steps": [], "counters": {}, "error": repr(e),
                   "calib": []}
        with self._state_lock:
            if self._run and self._run["id"] == run_id:
                self._run["report"] = rep
        if activated:
            try:
                self._demote(activated)      # 跑完把页面降回普通层，不长期霸屏
            except Exception:
                pass
        counters = rep.get("counters", {}) or {}
        status = rep.get("status", "failed")
        self.notify("event.run_state", {"run_id": run_id,
                                        "state": "done" if status == "ok" else status})
        self.notify("event.run_done", {
            "run_id": run_id, "status": status,
            "steps": len(rep.get("steps", [])),
            "clicks": counters.get("clicks", 0), "types": counters.get("types", 0),
            "notifies": counters.get("notifies", 0),
            "targets_rev": int(sg.get("targets_rev", 0) or 0),
            "error": rep.get("error")})
        self._log({"ok": "脚本运行完成", "stopped": "运行已停止",
                   "failed": "运行失败"}.get(status, "运行结束"),
                  "info" if status == "ok" else "warn")

    def _activate_target(self, sg: dict, opts: dict):
        """运行前把脚本第一步所属页面切到前台（返回 hwnd 供跑完降回置顶）。

        点按落在被遮挡的窗口上会点错地方（M0 实测），所以“运行”这一步由引擎负责
        把页面调到前面；找不到窗口不算错误（交给 L1 提示）。
        """
        if opts.get("activate") is False:
            return 0
        title = ""
        for st in (sg.get("steps") or []):
            for tgt in _targets_of(st):
                ctx = ((tgt.get("page") or {}).get("context") or {})
                if ctx.get("title"):
                    title = str(ctx["title"])
                    break
            if title:
                break
        if not title:
            return 0
        try:
            r = self._activate(title) or {}
        except Exception:
            return 0
        if r.get("ok"):
            self._log("已把操作页面切到前面")
            if r.get("occluded"):
                self._log("操作页面上方还压着别的窗口（“%s”），点按可能会点错地方"
                          % (r.get("top_title") or "未命名窗口"), "warn")
            return int(r.get("hwnd") or 0)
        self._log("没找到要操作的页面窗口，先按现在屏幕上的样子试一次", "warn")
        return 0

    def _make_ai(self, mode, run_id):
        mode = (mode or "stub").lower()
        if mode == "off":
            return ai_mod.SemanticStub()
        if mode == "cloud":
            if self._vlm is None:
                self._vlm = ai_mod.ZhipuVLM()
            if not self._vlm.enabled:
                choice = self._confirm(run_id, "ai_authorize",
                                       "自动校准需要把当前屏幕截图上传到云端做语义确认，是否同意？",
                                       ["同意上传", "使用本地识别"], default="使用本地识别")
                if choice == "同意上传":
                    self._vlm.authorize()
                if not self._vlm.enabled:
                    return ai_mod.SemanticStub()
            return self._vlm
        return ai_mod.SemanticStub()

    def _m_ai_authorize(self, p):
        agree = bool(p.get("agree"))
        if self._vlm is None:
            self._vlm = ai_mod.ZhipuVLM()
        if agree:
            self._vlm.authorize()
        else:
            self._vlm.revoke()
        return {"ok": True, "authorized": self._vlm.enabled}

    # ---------------------------------------------------------------- 人工确认

    def _confirm(self, run_id, kind, message, options, default=None):
        with self._state_lock:
            self._seq += 1
            rid = f"c{self._seq}"
            slot = {"event": threading.Event(), "choice": None}
            self._pending_confirm[rid] = slot
        self.notify("event.confirm_request", {
            "run_id": run_id, "request_id": rid, "kind": kind,
            "message": message, "options": list(options),
            "default": default or options[0]})
        if self._confirm_auto is not None:      # 测试/自动化钩子：事件照发，直接作答
            with self._state_lock:
                self._pending_confirm.pop(rid, None)
            return self._confirm_auto(kind, message, options)
        got = slot["event"].wait(self._confirm_timeout_s)
        with self._state_lock:
            self._pending_confirm.pop(rid, None)
        if not got:
            self._log("等待确认超时，按停止处理", "warn")
            return "停止"
        return slot["choice"] or (default or options[0])

    def _m_confirm_reply(self, p):
        rid = _req(p, "request_id")
        choice = str(p.get("choice", ""))
        with self._state_lock:
            slot = self._pending_confirm.get(rid)
        if slot is None:
            return {"ok": False, "note": "该确认请求已失效"}
        slot["choice"] = choice
        slot["event"].set()
        return {"ok": True}

    # ---------------------------------------------------------------- 关闭

    def _m_shutdown(self, p):
        self._stop_run()
        with self._state_lock:
            self._run = None
        self._closing = True
        self._log("引擎收到退出指令")
        return {"ok": True}


class _IpcHuman:
    """HumanIO 落点：通过 confirm_request/reply 与 UI 交互（白话文案）。"""

    def __init__(self, server: IpcServer, run_id: str):
        self.server = server
        self.run_id = run_id

    def notify(self, message):
        self.server._confirm(self.run_id, "notify", str(message), ["继续"])

    def prompt_not_found(self, message, target_text):
        choice = self.server._confirm(self.run_id, "not_found", str(message),
                                      ["继续", "用记下来的位置点一次", "跳过", "停止"])
        return {"继续": "continue", "用记下来的位置点一次": "coord_once",
                "跳过": "skip", "停止": "stop"}.get(choice, "stop")

    def prompt_outcome_fail(self, message):
        choice = self.server._confirm(self.run_id, "outcome_fail", str(message),
                                      ["继续", "停止"])
        return {"继续": "continue", "停止": "stop"}.get(choice, "stop")


class _Calibrated:
    """校准器包装：把结果以 event.calibrate 推给 UI（同时保留 note 供日志）。"""

    def __init__(self, server: IpcServer, cal: Calibrator, run_id: str):
        self.server = server
        self.cal = cal
        self.run_id = run_id

    def __call__(self, req: dict) -> dict:
        res = self.cal(req) or {}
        self.server.notify("event.calibrate", {
            "run_id": self.run_id, "reason": req.get("reason"),
            "ok": bool(res.get("ok")), "updated": bool(res.get("updated")),
            "note": res.get("note", "")})
        return res


# ---------------------------------------------------------------- 工具

def _collect_nearby(page_bgr, wrect, self_text, pad_x=180, pad_y=60, max_n=4):
    """采集目标四周的静态文字，作为"相对锚点"（邻居文字）。

    运行时的用途：同页出现多个相同文字（同名列、重复按钮、两个一样的占位提示）时，
    用"邻居对不对得上"来消歧（用户审查提出）。offset 相对目标中心，随目标一起移动。
    """
    if page_bgr is None:
        return []
    x, y, w, h = [int(v) for v in wrect]
    ph, pw = page_bgr.shape[:2]
    x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
    x1, y1 = min(pw, x + w + pad_x), min(ph, y + h + pad_y)
    crop = page_bgr[y0:y1, x0:x1]
    if crop is None or crop.size == 0 or crop.shape[0] < 24 or crop.shape[1] < 24:
        return []
    r = matcher.ocr_run(crop)
    if r.get("error"):
        return []
    cx, cy = x + w // 2, y + h // 2
    out = []
    for txt, box in zip(r.get("txts", []), r.get("boxes", [])):
        t = str(txt).strip()
        if len(t) < 2 or (self_text and t == self_text):
            continue
        bx, by, bw, bh = box[0] + x0, box[1] + y0, box[2], box[3]
        inside = not (bx + bw <= x or bx >= x + w or by + bh <= y or by >= y + h)
        if inside:                       # 目标框内的文字属于部件自己，不算邻居
            continue
        out.append({"text": t, "rect_in_page": [bx, by, bw, bh],
                    "offset": [bx + bw // 2 - cx, by + bh // 2 - cy]})
        if len(out) >= max_n:
            break
    return out


def _req(p, key):
    v = p.get(key)
    if v in (None, ""):
        raise _err(RPC_INVALID_PARAMS, f"缺参数 {key}")
    return v


def _rect_param(p, key):
    v = p.get(key)
    if not (isinstance(v, (list, tuple)) and len(v) == 4):
        raise _err(RPC_INVALID_PARAMS, f"参数 {key} 需为 [x,y,w,h]")
    try:
        x, y, w, h = [int(round(float(t))) for t in v]
    except Exception:
        raise _err(RPC_INVALID_PARAMS, f"参数 {key} 需为数字")
    if w <= 0 or h <= 0:
        raise _err(RPC_INVALID_PARAMS, f"参数 {key} 宽高需 >0")
    return [x, y, w, h]


def _targets_of(step, depth=0):
    """递归取出一个步骤里所有 target（含条件/循环/预期结果与子步骤）。"""
    if not isinstance(step, dict) or depth > 12:
        return
    for key in ("target",):
        t = step.get(key)
        if isinstance(t, dict):
            yield t
    for holder in ("condition", "loop", "expected_outcome"):
        h = step.get(holder)
        if isinstance(h, dict) and isinstance(h.get("target"), dict):
            yield h["target"]
    for branch in ("then", "else", "body"):
        for sub in (step.get(branch) or []):
            yield from _targets_of(sub, depth + 1)


def _now():
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def _version():
    try:
        from engine import __version__
        return __version__
    except Exception:
        return "0"


def main(argv=None) -> int:
    """`python -m engine serve`（或 CLI 子命令 serve）入口。"""
    import argparse
    ap = argparse.ArgumentParser(prog="engine serve")
    ap.add_argument("--loc-log", default="", help="定位日志 JSONL 路径（默认系统临时目录）")
    ap.add_argument("--confirm-timeout", type=float, default=CONFIRM_TIMEOUT_S)
    args = ap.parse_args(argv or [])
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    srv = IpcServer(loc_log_path=args.loc_log or None,
                    confirm_timeout_s=args.confirm_timeout)
    try:
        return srv.serve()
    except KeyboardInterrupt:
        return 0
