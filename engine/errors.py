# -*- coding: utf-8 -*-
"""
engine.errors — 引擎结构化错误（对应《项目开发过程文档》§7：引擎抛结构化错误码）。
UI 层把 code 映射为白话提示（术语表左列），引擎自身不负责文案。
"""
from __future__ import annotations


class EngineError(Exception):
    """带错误码的引擎异常。code 取值见 ERROR 常量说明。"""

    def __init__(self, code: str, message: str = "", details: dict | None = None):
        super().__init__(message or code)
        self.code = code
        self.details = details or {}

    def __str__(self) -> str:
        base = super().__str__()
        if self.details:
            return f"[{self.code}] {base} {self.details}"
        return f"[{self.code}] {base}"


# 错误码清单（新增需同步本注释与开发过程文档 §7）
ERRORS = {
    "schema_invalid": "脚本 JSON 不符合规格",       # E8 校验失败（details.errors 为条目列表）
    "script_not_found": "脚本文件不存在",
    "page_not_found": "页面定位失败",               # 过程性错误（校验入口）
    "widget_not_found": "部件定位失败",             # 过程性错误（校验入口）
    "click_guard_failed": "点击前校验未通过",       # 过程性错误（不发送点击）
    "loop_limit": "循环达到次数上限",               # 结果性失败（不触发校验）
    "verify_failed": "预期结果未出现",              # 结果性失败（走 on_fail，不触发校验）
    "stop_requested": "运行被停止",
    "driver_error": "屏幕/输入驱动错误",
    "human_skipped": "用户选择跳过该步",
    "not_implemented": "尚未实现（后续波次）",
}
