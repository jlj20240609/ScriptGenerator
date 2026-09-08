# -*- coding: utf-8 -*-
"""批量捕获 M0 五个类别目标（进程内顺序执行，复用 OCR 会话）"""
import argparse
import sys
import traceback
from types import SimpleNamespace

import m0lib
import m0_trial

SPECS = [
    # (name, cls, title 子串, widget 文字, 备注)
    ("login", "web-login", "M0 演示登录", "登录", "登录按钮"),
    ("erp", "erp-web", "M0 ERP 查询", "库存查询", "ERP 侧栏菜单"),
    ("dyn", "dynamic", "M0 动态监控台", "服务状态", "动态页卡片标题"),
    ("note", "desktop", "desktop-note.txt", "关键行", "记事本正文行"),
    ("icons", "icon-app", "icons - ", "项目资料", "文件夹图标标签"),
]


def main():
    m0lib.setup_utf8_stdio()
    m0lib.init_dpi_aware()
    ok_all = True
    for name, cls, title, text, note in SPECS:
        ns = SimpleNamespace(cmd="capture", name=name, cls=cls, text=text,
                             title=title, manual=False, dir=str(m0_trial.CAPTURE_DIR))
        print("\n======== capture %s (%s, %s) ========" % (name, note, title), flush=True)
        try:
            m0_trial.do_capture(ns)
            print("---- capture %s OK ----" % name, flush=True)
        except SystemExit as e:
            ok_all = False
            print("!! capture %s 失败: %s" % (name, e), flush=True)
        except Exception:
            ok_all = False
            traceback.print_exc()
    print("\n全部完成, 成功=%s" % ok_all)


if __name__ == "__main__":
    main()
