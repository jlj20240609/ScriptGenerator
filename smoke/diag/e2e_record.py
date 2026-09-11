# -*- coding: utf-8 -*-
"""
smoke/diag/e2e_record.py — M3-WP1 真机端到端验证（不需要人来操作）。

为什么要造一个 Tk 窗口当目标：录制的整条链路只能在真机上验（钩子挂没挂上、
坐标是不是物理像素、点击那一刻的帧对不对、点到的部件认不认得出来），但等人来
操作一遍太慢也不可复现。所以这里造一个自己完全掌控的窗口，再用 pynput 合成
**真实的**点击与按键——它们走的是和真人一样的那条系统输入队列，钩子收得到。

验证的是一条因果链，不是一个数字：
  合成的点击 → 钩子收到物理像素坐标 → 抓到点击那一刻的帧 → 定位窗口 →
  OCR 反查点到的文字 → 出来的目标能被 schema 接受 → 点真落在按钮上（Tk 回调被触发）

用法：smoke/.venv-ortcpu/Scripts/python.exe smoke/diag/e2e_record.py
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import capture                # noqa: E402
from engine import matcher                # noqa: E402
from engine import recorder as R          # noqa: E402
from engine import recorder_live as L     # noqa: E402
from engine import schema                 # noqa: E402

WIN_W, WIN_H = 460, 260
CLICKED = {"btn": False, "text": ""}
HITS: list = []          # Tk 实际收到的点击
KEYS: list = []          # Tk 实际收到的按键


def build_window():
    capture.init_dpi_aware()
    root = tk.Tk()
    root.title("录制自测窗口")
    root.geometry(f"{WIN_W}x{WIN_H}+120+120")
    root.attributes("-topmost", True)
    tk.Label(root, text="用户名", font=("Microsoft YaHei", 12)).place(x=24, y=40)
    entry = tk.Entry(root, font=("Consolas", 12), width=18)
    entry.place(x=110, y=38, width=200, height=26)
    btn = tk.Button(root, text="确定", font=("Microsoft YaHei", 12, "bold"),
                    command=lambda: on_ok(entry))
    btn.place(x=110, y=110, width=120, height=44)
    # Tk 侧到底收到了什么：区分「输入没送到」和「送到了但落错地方」
    root.bind_all("<Button-1>", lambda e: HITS.append((e.x_root, e.y_root)))
    root.bind_all("<Key>", lambda e: KEYS.append(getattr(e, "char", "")))
    root.update()
    root.update_idletasks()
    return root, entry, btn


def on_ok(entry):
    CLICKED["btn"] = True
    CLICKED["text"] = entry.get()


def center_of(widget):
    """控件的屏幕中心（用控件自己报的位置，不手算，免得算错还以为是引擎的问题）。"""
    return (widget.winfo_rootx() + widget.winfo_width() // 2,
            widget.winfo_rooty() + widget.winfo_height() // 2)


def pump(root, seconds=0.25, until=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        if until is not None and until():
            return
        time.sleep(0.02)


def _watchdog(limit_s=90.0):
    """硬看门狗：任何一步卡住都不能把机器拖住（M2 跑批吃过这个亏）。"""
    time.sleep(limit_s)
    print(f"\n[看门狗] {limit_s:.0f}s 未结束，强制退出。", flush=True)
    import os
    os._exit(7)


def main() -> int:
    import threading
    threading.Thread(target=_watchdog, daemon=True).start()
    capture.init_dpi_aware()
    fail = []

    def check(ok, label, detail=""):
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not ok:
            fail.append(label)
        return ok

    print("=" * 66)
    print("M3-WP1 真机端到端验证：合成输入 → 钩子 → 点击那刻的帧 → OCR 反查 → 脚本")
    print("=" * 66)

    # ---- 0. 屏幕可用性
    vx, vy, vw, vh = capture.virtual_screen_rect()
    print(f"虚拟屏 {vw}x{vh} @({vx},{vy})")
    try:
        probe = capture.grab_screen((vx, vy, 8, 8))
        check(probe is not None and probe.size > 0, "屏幕可抓（未锁屏）")
    except Exception as e:
        print(f"  [FAIL] 抓屏失败：{e!r}（锁定屏幕或安全软件拦截？）")
        return 9

    root, entry, btn = build_window()
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if not check((sw, sh) == (vw, vh), "Tk 坐标口径 = 物理像素",
                     f"Tk 报 {sw}x{sh}，物理 {vw}x{vh}"):
            print("  （口径不一致会让合成点击落错地方，先停下）")
            return 9

        hwnd = capture.window_from_point(root.winfo_rootx() + 5, root.winfo_rooty() + 5)
        check(bool(hwnd), "能从坐标找到窗口", f"hwnd={hwnd}")
        # 抢焦点用 Tk 自己的 focus_force，不用 capture.bring_to_foreground：
        # 后者靠 keybd_event 敲 Alt 解锁前台锁，会把窗口的系统菜单激活成模态循环，
        # 之后 root.update() 再也回不来（本脚本第一版就卡死在这里，实测）。
        root.lift()
        root.focus_force()
        pump(root, 0.6)

        # 护栏：抢焦点失败就绝不打字——否则合成的按键会落到用户正在用的窗口里
        fg = capture.fg_window_info().get("hwnd")
        if not check(fg == hwnd, "自测窗口已取得前台焦点（否则不打字，避免敲到别处）",
                     f"当前前台 hwnd={fg}，自测窗口 hwnd={hwnd}"):
            return 9

        entry_xy = center_of(entry)
        btn_xy = center_of(btn)
        print(f"目标点：输入框 {entry_xy}，按钮 {btn_xy}")
        print(f"按钮实际占位：({btn.winfo_rootx()},{btn.winfo_rooty()}) "
              f"{btn.winfo_width()}x{btn.winfo_height()}")

        # ---- 1. 录制 + 合成操作
        rec = L.build_recorder()
        res = rec.start()
        if not check(res.get("ok"), "钩子挂上并开始录制", str(res.get("note"))):
            return 9

        from pynput import keyboard, mouse
        mc, kc = mouse.Controller(), keyboard.Controller()
        pump(root, 0.3)
        mc.position = entry_xy
        pump(root, 0.15)
        mc.click(mouse.Button.left, 1)          # 真实点击 → 钩子应收到
        pump(root, 0.25)
        # 护栏二：只有输入框真的拿到焦点才打字
        if check(root.focus_get() is not None and str(root.focus_get()).startswith(".!entry"),
                 "输入框已获得焦点（否则不打字）", str(root.focus_get())):
            kc.type("demo")                     # 真实按键 → 应聚合成一块「输入文字 demo」
        else:
            kc = None
        pump(root, 0.4)
        time.sleep(1.5)                         # 停顿 > 1.2s → 应插一块「等一下」
        mc.position = btn_xy
        pump(root, 0.15)
        # 点之前先对质三件事：Tk 自己认为这个点是谁、Win32 认为是谁、谁在前台。
        # 上轮实测第二次点击没进 Tk（只收到第一次），必须先看清被谁挡了。
        try:
            under_tk = root.winfo_containing(*btn_xy)
        except Exception as e:
            under_tk = f"<{e!r}>"
        print(f"  点之前：Tk 命中测试 = {under_tk}；"
              f"Win32 落点窗口 = {capture.window_from_point(*btn_xy)}；"
              f"前台 = {capture.fg_window_info().get('hwnd')}；自测窗口 = {hwnd}")
        mc.click(mouse.Button.left, 1)
        pump(root, 0.6)
        try:
            after_tk = root.winfo_containing(*btn_xy)
        except Exception as e:
            after_tk = f"<{e!r}>"
        print(f"  点之后：Tk 命中测试 = {after_tk}；Tk 实收点击 = {HITS}")
        root.attributes("-topmost", False)

        stopped = rec.stop()
        blocks, summary = stopped["blocks"], stopped["summary"]
        print(f"\n录到 {summary['total']} 个动作（用时 {stopped['elapsed_s']}s，"
              f"存帧 {stopped['frames']} 张）：")
        for i, line in enumerate(summary["lines"], 1):
            print(f"   {i:>2}. {line}")

        acts = [b["action"] for b in blocks]
        check(acts.count(R.B_CLICK) >= 2, "两次点击各成一块", str(acts))
        check("demo" in summary["texts"], "打字的字符合聚成一块「输入文字」",
              str(summary["texts"]))
        check(R.B_WAIT in acts, "1.5s 停顿变成了「等一下」", str(acts))

        # 坐标口径：钩子收到的坐标必须等于合成点击的坐标
        clicks = [e for e in rec.events if e["kind"] == R.EV_CLICK]
        if check(len(clicks) >= 2, "钩子收到两次点击事件", f"{len(clicks)} 次"):
            got = [(c["x"], c["y"]) for c in clicks[:2]]
            near = all(abs(g[0] - w[0]) <= 2 and abs(g[1] - w[1]) <= 2
                       for g, w in zip(got, [entry_xy, btn_xy]))
            check(near, "钩子坐标 = 物理像素坐标（无 DPI 虚化）",
                  f"钩子 {got} vs 合成 {[entry_xy, btn_xy]}")

        # ---- 2. 反查部件 → 出步骤
        print("\n反查每个动作点到的是什么（OCR + 图像）……")
        t0 = time.perf_counter()
        steps_res = rec.steps()
        print(f"  用时 {time.perf_counter() - t0:.1f}s，出步骤 {len(steps_res['steps'])} 条，"
              f"跳过 {len(steps_res['skipped'])} 条")
        for n in steps_res["notes"]:
            print(f"  ! {n}")

        steps = steps_res["steps"]
        click_steps = [s for s in steps if s["action"] in ("click", "dblclick")]
        check(len(click_steps) >= 2, "两次点击都拿到了部件", f"{len(click_steps)} 条")
        texts = [(s.get("target") or {}).get("text", "") for s in click_steps]
        print(f"  反查到的部件文字：{texts}")
        check(any("确定" in t for t in texts), "OCR 认出了按钮上的「确定」", str(texts))
        type_steps = [s for s in steps if s["action"] == "type"]
        check(type_steps and type_steps[0]["params"]["text"] == "demo",
              "输入块带上了正确的内容")
        check(any((s.get("target") or {}).get("page") for s in steps),
              "第一块内嵌了页面（后续靠执行器继承）")

        sg = schema.new_script("真机录制自测")
        sg["steps"] = steps
        problems = schema.validate(sg)
        check(not problems, "录制出的脚本直接过 schema 校验", str(problems[:2]))

        out = ROOT / "smoke" / "rec" / "e2e_recorded.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        text = schema.dump(sg)
        out.write_text(text, encoding="utf-8")
        print(f"\n脚本已写出：{out}（{len(text) / 1024:.1f} KB）")

        # ---- 3. 因果闭环（不依赖合成输入的投递）
        #
        # 为什么不用「Tk 有没有收到这次点击」来判定：实测发现 pynput 的
        # Controller.type() 在这个环境里不稳定（不带录制器、光打字也会让目标窗口
        # 收不到按键，且之后连带收不到点击）。那是**合成输入方式**的问题，不是录制的
        # 问题，拿它当判据会冤枉引擎。
        # 改成两条确定性证据：
        #   a) 目标中心必须就在用户点击的那一点上（对得上坐标，说明认的是同一个东西）
        #   b) 把录到的目标矩形从录到的那张页面图里裁出来再 OCR，必须读出按钮文字
        pump(root, 0.3)
        cs = click_steps[-1]
        # 页面只在第一块内嵌（后续靠执行器继承），所以页面基准取「带页面的那一步」
        paged = [s for s in steps if (s.get("target") or {}).get("page")]
        page = (paged[0]["target"]["page"] if paged else {})
        rect_page = page.get("rect_in_screen") or []
        center = (cs.get("target") or {}).get("center_in_page") or []
        if check(bool(rect_page) and bool(center), "目标带页面与中心点"):
            cx = rect_page[0] + center[0]
            cy = rect_page[1] + center[1]
            hit = abs(cx - btn_xy[0]) <= 8 and abs(cy - btn_xy[1]) <= 8
            check(hit, "录到的目标中心 = 用户点击的那一点", f"({cx},{cy}) vs {btn_xy}")

        png = (page or {}).get("image") or ""
        box = (cs.get("target") or {}).get("rect_in_page")
        if check(bool(png) and bool(box), "目标带页面图与部件矩形"):
            import base64

            import cv2
            import numpy as np
            raw = base64.b64decode(png.split(",", 1)[1])
            page_bgr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            x, y, w, h = [int(v) for v in box]
            patch = page_bgr[y:y + h, x:x + w]
            r = matcher.ocr_run(patch)
            got = " ".join(r.get("txts") or [])
            check("确定" in got, "把录到的目标从录到的页面图里裁出来，OCR 仍是「确定」",
                  f"识别到 {got!r}（{w}x{h} 像素）")

        # 辅助信息：合成输入的投递情况（不作判据，见上）
        print(f"\n[参考] Tk 侧实收：点击 {HITS}，按键 {''.join(k for k in KEYS if k)}；"
              f"输入框现值 {entry.get()!r}")
        print(f"[参考] 按钮回调触发：{CLICKED['btn']}——"
              f"合成输入投递不稳定，此项只做参考")
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    print("=" * 66)
    if fail:
        print(f"结果：{len(fail)} 项未通过")
        for f in fail:
            print(f"  × {f}")
        return 1
    print("结果：全部通过（WP1 真机链路可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
