# -*- coding: utf-8 -*-
"""
engine.record_session — 录制会话（M3-WP2 的引擎侧）。

把「录制器」包成界面要的那个东西：能开始、能停、能中途看进度、能把录到的积木
实时推给界面。与 IPC 解耦——只用一个 `notify(方法名, 参数)` 回调，所以不必起
Electron 就能测。

为什么要实时推：录制是个"看不见的过程"（用户在别的窗口里操作），界面必须能显示
"我已经记下你这几步了"，否则用户不知道录没录上、要不要重录。这也让"录到一半发现
错了"能立刻停，而不是录完一长串才发现全废。

为什么最后一步要等 OCR：积木（点一下/输入文字）是纯逻辑、毫秒级；但"你点的是
哪个部件"要 OCR 反查（单次 0.5s 级）。所以录制中只推积木，停止后才批量出步骤。
"""
from __future__ import annotations

import threading
import time

from engine import recorder as R


def _default_make_recorder():
    from engine import recorder_live as L
    return L.build_recorder()


class RecordSession:
    """一次录制会话。同一时刻只允许一次（第二次 start 会被拒）。"""

    def __init__(self, notify=None, make_recorder=None, poll_s=0.4):
        self._notify = notify or (lambda method, params: None)
        self._make = make_recorder or _default_make_recorder
        self._poll_s = float(poll_s)
        self._lock = threading.Lock()
        self._rec = None
        self._thread = None
        self._pushed = 0
        self._tail_text = ""
        self._result = None
        self._reason = ""

    # ---------------------------------------------------------------- 查询

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._rec is not None

    @property
    def result(self):
        with self._lock:
            return self._result

    def _blocks(self, rec):
        return R.blocks_from_events(rec.events)

    def _describe(self, blocks):
        return [{"index": i, "action": b.get("action"), "text": R.describe(b),
                 "at": list(b["at"]) if b.get("at") else None}
                for i, b in enumerate(blocks)]

    def status(self) -> dict:
        with self._lock:
            rec = self._rec
            result = self._result
        if rec is None:
            return {"ok": True, "recording": False, "blocks": [], "summary": {},
                    "elapsed_s": 0.0, "result": result}
        blocks = self._blocks(rec)
        return {"ok": True, "recording": True, "blocks": self._describe(blocks),
                "summary": R.summarize(blocks), "elapsed_s": rec.elapsed_s,
                "grab_stats": dict(getattr(rec, "grab_stats", {}) or {}),
                "result": result}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> dict:
        with self._lock:
            if self._rec is not None:
                return {"ok": False, "note": "已经在录制了"}
            try:
                rec = self._make()
            except Exception as e:
                return {"ok": False, "note": f"录制器起不来：{e!r}"}
            res = rec.start()
            if not res.get("ok"):
                return res
            self._rec = rec
            self._thread = None
            self._pushed = 0
            self._tail_text = ""
            self._result = None
            self._reason = ""
        self._notify("event.record.started", {"hotkey": self._hotkey_of(
            self._rec)})
        thread = threading.Thread(target=self._pump, daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return {"ok": True, "note": "开始录制"}

    @staticmethod
    def _hotkey_of(rec) -> str:
        """停止热键的人话写法。顺序要固定（frozenset 的遍历顺序不保证）。"""
        hook = getattr(rec, "hooker", None)
        mods = getattr(hook, "_stop_mods", None)
        key = getattr(hook, "_stop_key", "")
        if not key or not mods:
            return ""
        ordered = sorted(mods, key=lambda m: (R.MOD_ORDER.get(m, 9), m))
        return "+".join(list(ordered) + [key])

    def cancel(self) -> dict:
        with self._lock:
            rec = self._rec
            self._rec = None
        if rec is None:
            return {"ok": False, "note": "当前没有在录制"}
        res = rec.discard()
        self._notify("event.record.cancelled", {})
        return res

    def stop(self, reason: str = "") -> dict:
        """停止并出步骤（含 OCR 反查，可能要几秒）。重复调用返回上次结果。"""
        with self._lock:
            rec = self._rec
            if rec is None:
                if self._result is not None:
                    return self._result
                return {"ok": False, "note": "当前没有在录制"}
            self._rec = None
            if reason:
                self._reason = reason
        stopped = rec.stop()
        blocks = stopped.get("blocks") or []
        steps_res = rec.steps()          # 这里做 OCR 反查，是整条链里最慢的一步
        result = {
            "ok": True,
            "blocks": blocks,
            "summary": stopped.get("summary") or {},
            "steps": steps_res.get("steps") or [],
            "skipped": steps_res.get("skipped") or [],
            "notes": steps_res.get("notes") or [],
            "elapsed_s": stopped.get("elapsed_s", 0.0),
            "reason": self._reason,
        }
        with self._lock:
            self._result = result
        return result

    # ---------------------------------------------------------------- 实时推送

    def _pump(self) -> None:
        while True:
            time.sleep(self._poll_s)
            with self._lock:
                rec = self._rec
            if rec is None:
                return
            try:
                blocks = self._blocks(rec)
                self._push(blocks)
            except Exception:
                continue
            hook = getattr(rec, "hooker", None)
            stopped = getattr(hook, "stopped", None)
            if stopped is not None and stopped.is_set():
                # 用户按了停止热键（他人在别的窗口里，不会回来点按钮）
                res = self.stop(reason="hotkey")
                self._notify("event.record.done", {
                    "blocks": res.get("blocks") or [],
                    "summary": res.get("summary") or {},
                    "reason": "hotkey"})
                return

    def _push(self, blocks) -> None:
        """推新积木；最后一块在长（打字还在继续）时推一次更新。"""
        described = self._describe(blocks)
        while self._pushed < len(described):
            item = dict(described[self._pushed])
            item["update"] = False
            self._notify("event.record.block", item)
            self._pushed += 1
        if described:
            tail = described[-1]["text"]
            if tail != self._tail_text:
                self._tail_text = tail
                item = dict(described[-1])
                item["update"] = True
                self._notify("event.record.block", item)
