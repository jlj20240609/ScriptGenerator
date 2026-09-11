# -*- coding: utf-8 -*-
"""
smoke/diag/e2e_replay.py — M3 ① 的收口验证：**录一遍 → 真的回放一遍**。

为什么要做成闭环：录出来的脚本"能不能过 schema 校验"只是形状对，真正的验收是
"照着它跑一遍，事情真的发生了"。这个脚本造一个自己掌控的窗口，先用合成输入走一遍
操作把它录下来，然后**清空现场**，再用执行器把录到的脚本真跑一遍，看窗口状态是否
又变成了同样的结果。

两个刻意的选择：
- 录制阶段的输入用**引擎自己的 send_unicode_text / 真实鼠标事件**，不用 pynput 的
  Controller.type（实测它在这个环境下有时送不进目标窗口，会把"录到了"和"送到了"
  搅在一起，结论就不可信了）。
- 回放阶段的 human=None → 任何"需要人"的地方都会直接失败。无人值守能跑通才算数。
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture                # noqa: E402
from engine import executor as X          # noqa: E402
from engine import recorder as R          # noqa: E402
from engine import recorder_live as L     # noqa: E402
from engine import schema                 # noqa: E402

WIN_W, WIN_H = 460, 260
TITLE = "录制回放自测窗口"
OUT = ROOT / "smoke" / "rec" / "e2e_replayed.json"
STATE = {"btn": 0, "text": ""}


def build_window():
    capture.init_dpi_aware()
    root = tk.Tk()
    root.title(TITLE)
    root.geometry(f"{WIN_W}x{WIN_H}+140+140")
    root.attributes("-topmost", True)
    tk.Label(root, text="用户名", font=("Microsoft YaHei", 12)).place(x=24, y=40)
    entry = tk.Entry(root, font=("Consolas", 12), width=18)
    entry.place(x=110, y=38, width=200, height=26)

    def on_ok():
        STATE["btn"] += 1
        STATE["text"] = entry.get()

    btn = tk.Button(root, text="确定", font=("Microsoft YaHei", 12, "bold"),
                    command=on_ok)
    btn.place(x=110, y=110, width=120, height=44)
    root.update()
    root.update_idletasks()
    root.lift()
    root.focus_force()
    return root, entry, btn


def center_of(w):
    return (w.winfo_rootx() + w.winfo_width() // 2,
            w.winfo_rooty() + w.winfo_height() // 2)


def pump(root, seconds=0.25):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.02)


def click_at(xy):
    """真实鼠标点击（走系统输入队列，与真人一致）。"""
    import win32api
    import win32con
    win32api.SetCursorPos((int(xy[0]), int(xy[1])))
    time.sleep(0.12)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


class _TracingDriver(X.LiveDriver):
    """记录执行器真正点在哪、输入了什么——"报告成功但窗口没反应"时必须靠这个定位。"""

    def __init__(self, win_ctx=None):
        super().__init__(win_ctx)
        self.clicks = []
        self.types = []

    def click(self, x, y, dbl=False):
        self.clicks.append((int(x), int(y), bool(dbl)))
        return super().click(x, y, dbl)

    def type_text(self, text):
        self.types.append(str(text))
        return super().type_text(text)


def main() -> int:
    capture.init_dpi_aware()
    fail = []

    def check(ok, label, detail=""):
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not ok:
            fail.append(label)
        return ok

    print("=" * 68)
    print("M3 ① 收口验证：录一遍 → 清空现场 → 真回放一遍 → 结果应一致")
    print("=" * 68)

    root, entry, btn = build_window()
    try:
        entry_xy, btn_xy = center_of(entry), center_of(btn)
        hwnd = capture.window_from_point(*entry_xy)
        fg = capture.fg_window_info().get("hwnd")
        if not check(fg == hwnd, "自测窗口已取得前台焦点（否则不打字，避免敲到别处）",
                     f"前台={fg} 自测窗口={hwnd}"):
            return 9

        # ---------------- 第一段：录制 ----------------
        print("\n[1/3] 录制：点输入框 → 打 demo → 点「确定」")
        rec = L.build_recorder()
        if not check(rec.start().get("ok"), "录制器启动"):
            return 9
        click_at(entry_xy)
        pump(root, 0.35)
        check(X.send_unicode_text("demo") == 4, "输入 4 个字符",
              f"输入框现值={entry.get()!r}")
        pump(root, 0.4)
        time.sleep(1.6)                          # 停顿 → 应录成「等一下」
        click_at(btn_xy)
        pump(root, 0.6)
        stopped = rec.stop()
        blocks = stopped["blocks"]
        print("  录到的积木：" + " → ".join(stopped["summary"]["lines"]))
        check(STATE["btn"] == 1, "录制阶段：按钮真的被按到了（说明输入确实送到了窗口）",
              f"回调次数={STATE['btn']} 输入框={STATE['text']!r}")

        steps_res = rec.steps()
        steps = steps_res["steps"]
        print(f"  反查出 {len(steps)} 步" + (f"，跳过 {len(steps_res['skipped'])} 步"
                                           if steps_res["skipped"] else ""))
        for n in steps_res["notes"]:
            print(f"  ! {n}")
        if not check(len(steps) >= 3, "至少 3 步（点一下/输入文字/点一下）", str(len(steps))):
            return 1
        sg = schema.new_script("录制回放自测")
        sg["steps"] = steps
        problems = schema.validate(sg)
        if not check(not problems, "录到的脚本过 schema 校验", str(problems[:2])):
            return 1
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(schema.dump(sg), encoding="utf-8")

        # ---------------- 第二段：清空现场 ----------------
        print("\n[2/3] 清空现场：输入框清掉、按钮计数归零")
        entry.delete(0, "end")
        STATE["btn"] = 0
        STATE["text"] = ""
        pump(root, 0.3)
        check(entry.get() == "" and STATE["btn"] == 0, "现场已复位",
              f"输入框={entry.get()!r} 回调次数={STATE['btn']}")

        # ---------------- 第三段：回放 ----------------
        print("\n[3/3] 回放：用执行器把录到的脚本原样跑一遍（human=None，无人值守）")
        print(f"  回放前：前台窗口={capture.fg_window_info().get('hwnd')}（自测窗口={hwnd}）")
        t0 = time.perf_counter()
        driver = _TracingDriver({"hwnd": hwnd})

        # 回放跑在**后台线程**里，主线程继续转 Tk 事件循环。
        # 为什么必须这样：Tk 只在 update()/mainloop() 里处理事件；如果让 run_script
        # 在主线程同步跑完，窗口收到的点击和按键会一直排在队列里没人处理，
        # 看起来就是"执行器报告成功、窗口却毫无反应"（第一次跑就踩了这个坑）。
        box = {}

        def _replay():
            try:
                box["rep"] = X.run_script(sg, driver, cfg=X.RunConfig(), human=None)
            except Exception as e:
                box["err"] = repr(e)

        th = threading.Thread(target=_replay, daemon=True)
        th.start()
        while th.is_alive():
            pump(root, 0.15)
        pump(root, 0.8)
        secs = time.perf_counter() - t0
        if "err" in box:
            check(False, "回放执行未抛异常", box["err"])
            return 1
        rep = box["rep"]
        print(f"  运行结果 status={rep['status']}，用时 {secs:.1f}s")
        for row in rep.get("steps", []):
            print(f"    · [{row.get('status')}] {row.get('label') or row.get('step_id')}")
        print(f"  执行器实际点击坐标：{driver.clicks}")
        print(f"  期望坐标：输入框 {entry_xy}，按钮 {btn_xy}")
        print(f"  执行器实际输入：{driver.types}")

        check(rep["status"] == "ok", "回放整体成功（无人值守跑完，没有卡在人工确认）",
              rep.get("error", ""))
        check(STATE["btn"] == 1, "回放后按钮被按到了（**这一步是闭环的关键**）",
              f"回调次数={STATE['btn']}")
        check(STATE["text"] == "demo", "回放后输入框里是 demo（录到的字真的打进去了）",
              f"实际={STATE['text']!r}")

        # ---------------- 第四段：再放一遍，看可重复性 ----------------
        print("\n[4/4] 再回放一遍（可重复性：同一份脚本放两次应得到同样结果）")
        entry.delete(0, "end")
        STATE["btn"] = 0
        STATE["text"] = ""
        pump(root, 0.3)
        driver2 = _TracingDriver({"hwnd": hwnd})
        box2 = {}

        def _replay2():
            try:
                box2["rep"] = X.run_script(sg, driver2, cfg=X.RunConfig(), human=None)
            except Exception as e:
                box2["err"] = repr(e)

        th2 = threading.Thread(target=_replay2, daemon=True)
        th2.start()
        while th2.is_alive():
            pump(root, 0.15)
        pump(root, 0.8)
        rep2 = box2.get("rep") or {}
        # 比第一遍多按了一次？不，第二次回放只应产生一次点击回调
        check(rep2.get("status") == "ok", "第二遍回放也成功", box2.get("err", ""))
        check(STATE["btn"] == 1 and STATE["text"] == "demo",
              "第二遍结果与第一遍一致（可重复）",
              f"回调次数={STATE['btn']} 输入框={STATE['text']!r}")

        print("\n脚本：", OUT.relative_to(ROOT))
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    print("=" * 68)
    if fail:
        print(f"结果：{len(fail)} 项未通过")
        for f in fail:
            print("  × " + f)
        return 1
    print("结果：全部通过（① 录制回放闭环成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
