# -*- coding: utf-8 -*-
"""M0 desktop 类目标：自绘 Tk 窗口（模拟无 UIA 的自绘桌面客户端，如 QQ/游戏界面）。
窗口内容为白底中文文本 + 一个“按钮区”，标题含唯一标识以便按标题定位。
用法: python smoke/m0_tk_target.py   （保持运行；Ctrl+C 或关窗退出）
"""
import sys
import time
import tkinter as tk

LINES = [
    "M0桌面验证客户端（自绘 UI，无辅助功能树）",
    "┈" * 38,
    "模块一：任务中心",
    "   任务流水号：A-2026-0913",
    "   状态：M0桌面验证标记行-等待执行",
    "   负责人：脚本构建器测试",
    "模块二：执行参数",
    "   重试次数：3    间隔：2 秒",
    "模块三：运行记录",
    "   上次运行：通过    耗时：1.2 秒",
    "说明：此窗口用于定位/点击测试，点击文本不会改变状态。",
]


def main():
    win = tk.Tk()
    win.title("M0 Tk 桌面客户端")
    win.configure(bg="white")
    # 900x580 逻辑尺寸 → 物理 ×1.25；位置避开屏幕正中央
    win.geometry("900x580+960+60")
    canvas = tk.Canvas(win, bg="#eceff3", highlightthickness=0)
    canvas.pack(fill="both", expand=True, padx=0, pady=0)
    W = 900
    # 顶部深色标题栏
    canvas.create_rectangle(0, 0, W, 46, fill="#16365c", outline="")
    canvas.create_text(16, 23, text="M0 桌面业务客户端 - 任务中心", anchor="w",
                       fill="white", font=("Microsoft YaHei", 14, "bold"))
    canvas.create_text(W - 16, 23, text="连接正常 · 2026-09", anchor="e",
                       fill="#9fc3e8", font=("Microsoft YaHei", 10))
    # 左侧导航栏
    canvas.create_rectangle(0, 46, 150, 580, fill="#243a52", outline="")
    for i, item in enumerate(["任务中心", "执行记录", "参数配置", "运行日志", "关于系统"]):
        y = 78 + i * 52
        fill = "#2f7fd1" if i == 0 else "#243a52"
        canvas.create_rectangle(0, y - 20, 150, y + 22, fill=fill, outline="")
        canvas.create_text(40, y, text=item, anchor="w", fill="white",
                           font=("Microsoft YaHei", 11))
    # 右侧内容区
    canvas.create_text(176, 66, text="当前任务队列", anchor="w", fill="#16365c",
                       font=("Microsoft YaHei", 13, "bold"))
    rows = [
        ("A-2026-0913", "状态：M0桌面验证标记行-等待执行", "张伟", "通过"),
        ("A-2026-0912", "导出月度报表", "李娜", "通过"),
        ("A-2026-0911", "同步库存快照", "王芳", "运行中"),
    ]
    x0, y0, x1 = 176, 96, 880
    canvas.create_rectangle(x0, y0, x1, y0 + 116, fill="white", outline="#c9d4e0")
    for j, head in enumerate(["任务号", "任务内容", "负责人", "状态"]):
        hx = x0 + 8 + j * 175
        canvas.create_text(hx, y0 + 14, text=head, anchor="w", fill="#5a6b7f",
                           font=("Microsoft YaHei", 10, "bold"))
    for i, (no, desc, owner, st) in enumerate(rows):
        ry = y0 + 38 + i * 26
        canvas.create_text(x0 + 8, ry, text=no, anchor="w", fill="#222",
                           font=("Consolas", 10))
        canvas.create_text(x0 + 8 + 175, ry, text=desc, anchor="w", fill="#222",
                           font=("Microsoft YaHei", 11))
        canvas.create_text(x0 + 8 + 350, ry, text=owner, anchor="w", fill="#222",
                           font=("Microsoft YaHei", 11))
        canvas.create_text(x0 + 8 + 525, ry, text=st, anchor="w", fill="#1e6fff",
                           font=("Microsoft YaHei", 11))
    for k in range(1, 4):
        canvas.create_line(x0 + k * 175, y0, x0 + k * 175, y0 + 116, fill="#dfe7ef")
    canvas.create_line(x0, y0 + 34, x1, y0 + 34, fill="#dfe7ef")
    for k in range(1, 3):
        canvas.create_line(x0, y0 + 34 + k * 26, x1, y0 + 34 + k * 26, fill="#f0f4f8")
    # 底部操作条
    canvas.create_rectangle(150, 520, W, 580, fill="#ffffff", outline="#c9d4e0")
    canvas.create_rectangle(176, 534, 300, 566, fill="#1e6fff", outline="")
    canvas.create_text(238, 550, text="M0执行按钮-立即运行", fill="white",
                       font=("Microsoft YaHei", 11))
    canvas.create_text(330, 550, text="窗口底部提示栏：点击安全区不会产生副作用",
                       anchor="w", fill="#8a97a5", font=("Microsoft YaHei", 9))
    print("Tk 目标窗口已启动", flush=True)
    win.mainloop()


if __name__ == "__main__":
    main()
