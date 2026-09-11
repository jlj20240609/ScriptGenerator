# -*- coding: utf-8 -*-
"""
smoke/diag/e2e_replay.py — M3 ① 的收口验证：**录一遍 → 真的回放一遍**（多场景）。

为什么要做成闭环：录出来的脚本"能过 schema 校验"只说明形状对，真正的验收是
"照着它跑一遍，事情真的发生了"。这里造一个自己掌控的窗口，用合成输入把几段
不同结构的操作录下来，**清空现场**，再用执行器把录到的脚本真跑一遍，看窗口状态
是否又变成了同样的结果。

为什么要是多场景：单个场景只能证明"闭环成立"，给不出任何比例。三个结构的场景
（带停顿的、不带停顿的、点两次的）才能说明"这套推断+回放不是只在一种情况下对"。

两个刻意的选择：
- 录制阶段的输入用**引擎自己的 send_unicode_text / 真实鼠标事件**，不用 pynput 的
  Controller.type（实测它在这个环境下有时送不进目标窗口，会把"录到了"和"送到了"
  搅在一起，结论就不可信了）。
- 回放阶段 human=None → 任何"需要人"的地方都会直接失败。无人值守能跑通才算数。
- 回放跑在**后台线程**、主线程继续转 Tk 事件循环：Tk 只在 update()/mainloop() 里
  处理事件，让 run_script 在主线程同步跑完的话，点击和按键会一直排在队列里没人处理
  ——看起来就是"执行器报告成功、窗口却毫无反应"（第一次跑就踩了这个坑）。
"""
from __future__ import annotations

import json
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
OUTDIR = ROOT / "smoke" / "rec"
STATE = {"btn": 0, "text": ""}

# 场景 = 一段"用户会做的操作"。录进去、放出来，结果应当一致。
SCENARIOS = [
    {
        "name": "点输入框 → 打字 → 停顿 → 点确定",
        "acts": [("click", "entry"), ("type", "demo"), ("sleep", 1.6), ("click", "btn")],
        "want_btn": 1, "want_text": "demo",
        "why": "含停顿：应录出「等一下」，且回放要真的等",
    },
    {
        "name": "点输入框 → 打字 → 立刻点确定",
        "acts": [("click", "entry"), ("type", "order-42"), ("click", "btn")],
        "want_btn": 1, "want_text": "order-42",
        "why": "无停顿：不该冒出多余的「等一下」积木",
    },
    {
        "name": "打字 → 点确定 → 停顿 → 再点确定",
        "acts": [("click", "entry"), ("type", "x1"), ("click", "btn"),
                 ("sleep", 1.4), ("click", "btn")],
        "want_btn": 2, "want_text": "x1",
        "why": "两次点击间隔 1.4s：不该被并成双击，且两次都要真的点到",
    },
]


# ---------------------------------------------------------------- 窗口

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


def reset_state(entry, root):
    entry.delete(0, "end")
    STATE["btn"] = 0
    STATE["text"] = ""
    pump(root, 0.3)


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


def replay_in_background(root, sg, hwnd):
    """回放跑后台线程，主线程继续 pump Tk —— 否则窗口根本收不到输入。

    sink 逐行打印执行进度：卡死时"最后打印出来的那一行"就是卡住的那一步
    （只看结果日志的话，录一遍放一遍全挤在 run_scenario 里，看不出卡在哪）。
    """
    driver = _TracingDriver({"hwnd": hwnd})
    box = {}

    def _sink(row):
        print(f"    · [{row.get('status')}] {row.get('label') or row.get('step_id')}",
              flush=True)

    def _run():
        try:
            box["rep"] = X.run_script(sg, driver, cfg=X.RunConfig(), human=None,
                                      sink=_sink)
        except Exception as e:
            box["err"] = repr(e)

    t0 = time.perf_counter()
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    while th.is_alive():
        pump(root, 0.15)
    pump(root, 0.8)
    return box, driver, time.perf_counter() - t0


# ---------------------------------------------------------------- 单场景

def run_scenario(root, entry, btn, sc, idx):
    """录一段 → 清空现场 → 回放 → 核对。返回该场景的证据。"""
    entry_xy, btn_xy = center_of(entry), center_of(btn)
    hwnd = capture.window_from_point(*entry_xy)
    out = {"name": sc["name"], "why": sc["why"], "phase": "start", "ok": False,
           "detail": "", "blocks": [], "steps": 0, "clicks": [], "types": [],
           "want_btn": sc["want_btn"], "want_text": sc["want_text"]}

    root.lift()
    root.focus_force()
    pump(root, 0.4)
    if capture.fg_window_info().get("hwnd") != hwnd:
        out["detail"] = "自测窗口没拿到前台焦点（不打字，避免敲到别处）"
        return out

    # ---- 录制
    out["phase"] = "录制"
    rec = L.build_recorder()
    if not rec.start().get("ok"):
        out["detail"] = "录制器启动失败"
        return out
    print("    （录制中…）", flush=True)
    for act, val in sc["acts"]:
        if act == "click":
            click_at(entry_xy if val == "entry" else btn_xy)
            pump(root, 0.35)
        elif act == "type":
            X.send_unicode_text(val)
            pump(root, 0.4)
        elif act == "sleep":
            time.sleep(float(val))
            pump(root, 0.05)
    pump(root, 0.6)
    rec.stop()
    print("    （录制结束，反查部件中…）", flush=True)
    steps_res = rec.steps()
    print("    （反查完成）", flush=True)
    out["blocks"] = [R.describe(b) for b in R.blocks_from_events(rec.events)]
    steps = steps_res["steps"]
    out["steps"] = len(steps)
    if not steps:
        out["detail"] = "没录出任何步骤"
        return out
    sg = schema.new_script(f"录制回放-{idx}")
    sg["steps"] = steps
    problems = schema.validate(sg)
    if problems:
        out["detail"] = f"脚本没过 schema：{problems[:1]}"
        return out
    out["steps_obj"] = steps          # 留着给主流程dump成样本

    # ---- 清空现场
    reset_state(entry, root)
    if entry.get() != "" or STATE["btn"] != 0:
        out["detail"] = "现场没复位"
        return out

    # ---- 回放
    out["phase"] = "回放"
    box, driver, secs = replay_in_background(root, sg, hwnd)
    out["clicks"] = driver.clicks
    out["types"] = driver.types
    out["secs"] = round(secs, 1)
    if "err" in box:
        out["detail"] = f"回放抛异常：{box['err']}"
        return out
    rep = box.get("rep") or {}
    out["status"] = rep.get("status")
    out["rows"] = [f"[{r.get('status')}] {r.get('label') or r.get('step_id')}"
                   for r in rep.get("steps", [])]
    if rep.get("status") != "ok":
        out["detail"] = f"回放 status={rep.get('status')}"
        return out
    if STATE["btn"] != sc["want_btn"]:
        out["detail"] = f"按钮回调 {STATE['btn']} 次，期望 {sc['want_btn']}"
        return out
    if STATE["text"] != sc["want_text"]:
        out["detail"] = f"输入框 {STATE['text']!r}，期望 {sc['want_text']!r}"
        return out
    out["ok"] = True
    out["detail"] = f"回放 {secs:.1f}s，按钮 {STATE['btn']} 次、输入框 {STATE['text']!r}"
    return out


def _write_result(idx, out):
    """把单场景结果落盘给父进程汇总。

    出错**必须喊出来**：这里原来写的是 `except Exception: pass`，于是 `import json`
    漏了导致的 NameError 被静静吞掉，父进程看到的是"子进程退出码 0 但没留下结果"，
    白查一轮。静默 except 藏过 bug 不止一次了（collect_evidence 那次也是）。
    """
    path = ROOT / "smoke" / "rec" / f"replay_s{idx}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"  ! 结果落盘失败（{e!r}）", flush=True)


def run_suite() -> int:
    """逐场景起**独立子进程**跑，最后汇总比例。

    为什么不是一个进程里连跑三个场景（这是踩过的坑）：同一个进程里跑完第一个场景后，
    第二个场景的**回放**会让主线程 forever 卡在 `tkinter.update()` 里（faulthandler 的
    全线程栈显示只剩主线程，回放线程早已结束）——那是 Tk 事件循环 + 合成输入在
    同一进程里反复折腾的脆弱点，与引擎无关（第二次录制与反查都正常、回放单独跑也正常）。
    一个场景一个进程既绕开了它，也更接近真实用法。
    """
    import subprocess
    print("=" * 72)
    print("M3 ① 收口验证：录一遍 → 清空现场 → 真回放一遍（每场景一个独立进程）")
    print("=" * 72)
    results = []
    for i, sc in enumerate(SCENARIOS, 1):
        print(f"\n[场景 {i}/{len(SCENARIOS)}] {sc['name']}")
        print(f"  为什么要有这个场景：{sc['why']}")
        log = ROOT / "smoke" / "diag" / f"replay_child_{i}.log"
        try:
            with log.open("w", encoding="utf-8") as fh:
                cp = subprocess.run([sys.executable, "-u", str(Path(__file__)),
                                     "--only", str(i)],
                                    stdout=fh, stderr=subprocess.STDOUT, timeout=300)
            code = cp.returncode
        except subprocess.TimeoutExpired:
            code = 124
        res = {}
        try:
            res = json.loads((ROOT / "smoke" / "rec" / f"replay_s{i}.json")
                             .read_text(encoding="utf-8"))
        except Exception:
            res = {"name": sc["name"], "ok": False,
                   "detail": f"子进程没留下结果（退出码 {code}，见 {log.name}）"}
        results.append(res)
        if res.get("blocks"):
            print(f"  录出的积木：{' → '.join(res['blocks'])}")
        if res.get("rows"):
            print("  回放：" + "；".join(res["rows"]))
        if res.get("clicks"):
            print(f"  实际点击坐标：{res['clicks']}　实际输入：{res.get('types')}")
        print(f"  {'✓ 通过' if res.get('ok') else '✗ 未通过'}：{res.get('detail')}",
              flush=True)

    okn = sum(1 for r in results if r.get("ok"))
    print("\n" + "=" * 72)
    print(f"结果：录制→回放闭环 {okn}/{len(results)} = {okn / len(results) * 100:.0f}%")
    for r in results:
        print(f"  {'✓' if r.get('ok') else '✗'} {r.get('name')}　{r.get('detail')}")
    print("\n说明：三个场景覆盖「带停顿 / 不带停顿 / 点两次」三种结构，各跑一遍。"
          "这能说明闭环在多种结构下都成立，但仍不是统计意义上的成功率"
          "（要 ≥90% 那种口径得做多轮跑批，见 docs/M3_收口记录.md）。")
    return 0 if okn == len(results) else 1


def main() -> int:
    if "--only" not in sys.argv:
        return run_suite()
    # 硬看门狗：卡住超过 N 秒就把**所有线程的调用栈**写进文件再退出。
    # 为什么必须有（M2 跑批吃过亏，这次第一版脚本又漏了）：真机上 OCR/mss 存在
    # 概率性挂死；卡住时没有栈信息就只能靠猜，而"猜"会浪费一整轮。
    # 写文件而不是 stderr：单线程的 stderr 会被 shell 交错打断，看全不了。
    import faulthandler
    stackfile = ROOT / "smoke" / "diag" / "replay_stacks.txt"
    try:
        _stacks = stackfile.open("w", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_stacks)
        faulthandler.dump_traceback_later(180, exit=True, file=_stacks)
    except Exception:
        faulthandler.dump_traceback_later(180, exit=True)
    capture.init_dpi_aware()
    print("=" * 72)
    print("M3 ① 收口验证：录一遍 → 清空现场 → 真回放一遍（多场景）")
    print("=" * 72)

    root, entry, btn = build_window()
    results = []
    try:
        entry_xy = center_of(entry)
        hwnd = capture.window_from_point(*entry_xy)
        if capture.fg_window_info().get("hwnd") != hwnd:
            print("× 自测窗口没拿到前台焦点，先停下（不打字，避免敲到别处）")
            return 9
        print(f"自测窗口 hwnd={hwnd}（已在前台）\n")

        saved = None
        todo = list(enumerate(SCENARIOS, 1))
        if "--only" in sys.argv:
            raw = sys.argv[sys.argv.index("--only") + 1]
            want = {int(x) for x in raw.split(",") if x.strip()}
            todo = [(i, sc) for i, sc in todo if i in want]
        for i, sc in todo:
            print(f"[场景 {i}/{len(SCENARIOS)}] {sc['name']}")
            print(f"  为什么要有这个场景：{sc['why']}")
            res = run_scenario(root, entry, btn, sc, i)
            results.append(res)
            print(f"  录出的积木：{' → '.join(res['blocks']) or '（空）'}")
            if res.get("rows"):
                print("  回放：" + "；".join(res["rows"]))
            if res.get("clicks"):
                print(f"  实际点击坐标：{res['clicks']}　实际输入：{res['types']}")
            print(f"  {'✓ 通过' if res['ok'] else '✗ 未通过'}：{res['detail']}", flush=True)
            if res["ok"] and saved is None:
                saved = res          # 留第一个成功场景的脚本当样本
            _write_result(i, res)
            print()

        if saved is not None:
            OUTDIR.mkdir(parents=True, exist_ok=True)
            out = OUTDIR / "e2e_replayed.json"
            sg = schema.new_script("录制回放自测·" + saved["name"])
            sg["steps"] = saved["steps_obj"]
            out.write_text(schema.dump(sg), encoding="utf-8")
            print(f"样本脚本：{out.relative_to(ROOT)}（来自「{saved['name']}」）\n")

        okn = sum(1 for r in results if r["ok"])
        print("=" * 72)
        print(f"结果：录制→回放闭环 {okn}/{len(results)} = {okn / len(results) * 100:.0f}%")
        for r in results:
            print(f"  {'✓' if r['ok'] else '✗'} {r['name']}　{r['detail']}")
        print("")
        print("说明：三个场景覆盖了「带停顿 / 不带停顿 / 点两次」三种结构，各跑一遍；"
              "这能说明闭环在多种结构下都成立，但仍不是统计意义上的成功率"
              "（要 ≥90% 那种口径得做多轮跑批，见 docs/M3_收口记录.md）。")
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    okn = sum(1 for r in results if r["ok"])
    return 0 if results and okn == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
