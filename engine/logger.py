# -*- coding: utf-8 -*-
"""
engine.logger — E6：定位/运行 JSONL 日志（引擎侧，不入脚本文件）。

规格：《M1_引擎设计清单》§4 定位日志行
  {ts, step_id, method(page_tpl|anchor|uia|ocr_text|tpl|page_coord|ai|...), confidence,
   rect, dev_px?, screen_meta, elapsed_ms, detail?}
用法：运行期逐事件 log_loc()；校验模式（E5）按 step_id 调 tail() 收集最近日志。
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


class LocLogger:
    """追加式 JSONL；tail 按需回读。线程安全（锁内写）。"""

    def __init__(self, path=None):
        if path is None:
            path = Path("engine_loc.jsonl")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_loc(self, row: dict) -> dict:
        """写一行定位/事件日志；自动补 ts/session 等公共字段，返回实际写入行。"""
        full = {"ts": _now_iso(), **row}
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")
        return full

    def tail(self, step_id=None, n=50) -> list:
        """回读最近 n 行；step_id 给定则只返回该步骤的行（按写入序）。"""
        rows = []
        with _LOCK:
            if not self.path.exists():
                return []
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if step_id is None or r.get("step_id") == step_id:
                        rows.append(r)
        return rows[-n:]

    def clear(self) -> None:
        with _LOCK:
            self.path.write_text("", encoding="utf-8")


class MemoryLogger:
    """测试/进程内用途：不落盘，行为与 LocLogger 一致。"""

    def __init__(self):
        self._rows = []

    def log_loc(self, row: dict) -> dict:
        full = {"ts": _now_iso(), **row}
        self._rows.append(full)
        return full

    def tail(self, step_id=None, n=50) -> list:
        rows = self._rows if step_id is None else [r for r in self._rows if r.get("step_id") == step_id]
        return rows[-n:]

    def clear(self) -> None:
        self._rows = []
