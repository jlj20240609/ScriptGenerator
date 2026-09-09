# -*- coding: utf-8 -*-
"""
engine — ScriptGenerator M1 引擎（Python 侧，波次1：离线纵向切片）。

模块（对照《M1_引擎设计清单》§2）：
  schema     E8  .sgscript.json v1.0 读写/校验
  matcher    E3  多尺度模板 / OCR 条带 / 相似度 / data-url
  capture    E1  窗口/页面/静态锚采集（win32 惰性导入）
  locator    E2  页面定位（整窗模板→静态锚）/ 页内部件三级定位
  executor   E4  六动作 × 条件 × 循环解释执行 + L1/L2（ScreenDriver 可离线测试）
  logger     E6  定位 JSONL（tail 供校验模式调阅）
  errors        结构化错误码（开发过程文档 §7）
  cli           引擎 CLI（引擎可独立于 UI 运行）
待波次2：calibrator(E5) + ai(E7) + 真实窗口回归资产迁移。
"""
__version__ = "0.1.0"
__engine_spec__ = "M1-清单-v0.1 / 分析文档 v0.31 §11"
