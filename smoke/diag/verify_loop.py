# -*- coding: utf-8 -*-
"""验证“多个步骤作为一个整体循环”的引擎语义（离线，合成屏，不碰真实桌面）。

用例：
 1) 3 个步骤放进一个“重复 2 次”→ 期望按 甲,乙,丙,甲,乙,丙 顺序整体跑两遍
 2) 循环之后的主流程步骤只跑一次
 3) 循环里再套循环（嵌套）→ 次数相乘、顺序正确
 4) 循环体里放“如果看到…就点它”（循环 + 条件的组合，真实场景）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.tests import support as S  # noqa: E402
from engine.tests.test_executor import Env  # noqa: E402


_seq = [0]


def notify(msg):
    _seq[0] += 1
    return S.action_step(f"n{_seq[0]}", "notify", None, {"message": msg})


def run(name, steps):
    env = Env()
    sg = S.script(name, steps)
    rep = env.run(sg)
    loops = [r for r in rep["steps"] if r.get("type") == "loop" and "iterations" in r]
    print(f"[{name}] status={rep['status']} 通知序列={env.human.notified} "
          f"循环迭代={[r['iterations'] for r in loops]}")
    return env, rep


print("① 三个步骤作为一个整体重复 2 次")
run("整体循环", [
    S.loop_step("l1", {"mode": "count", "count": 2},
                body=[notify("甲"), notify("乙"), notify("丙")]),
])

print("② 循环之后再接一个主流程步骤")
run("循环后接步骤", [
    S.loop_step("l1", {"mode": "count", "count": 2}, body=[notify("甲"), notify("乙")]),
    notify("尾"),
])

print("③ 循环里再套循环（2 × 2）")
run("嵌套循环", [
    S.loop_step("l1", {"mode": "count", "count": 2}, body=[
        notify("外"),
        S.loop_step("l2", {"mode": "count", "count": 2}, body=[notify("内")]),
    ]),
])

print("④ 循环体里放“如果看到按钮就点它”（条件在循环里：看到才点）")
env = Env()
sg = S.script("条件循环", [
    S.loop_step("l1", {"mode": "count", "count": 3}, body=[
        S.condition_step("c1", env.t_login("login_btn"), exists=True,
                         then=[S.action_step("a1", "click", env.t_login("login_btn"), None)]),
    ]),
])
rep = env.run(sg)
loops = [r for r in rep["steps"] if r.get("type") == "loop" and "iterations" in r]
print(f"[条件循环] status={rep['status']} 点击次数={len(env.driver.clicks)} "
      f"循环迭代={[r['iterations'] for r in loops]} "
      f"（点一下就跳到首页 → 之后两次“看不到”自然不点，语义正确）")

print("⑤ 批量填表骨架：重复 3 次 { 输入文字 → 提示我 }")
env = Env()
sg = S.script("批量填表", [
    S.loop_step("l1", {"mode": "count", "count": 3}, body=[
        S.action_step("b1", "type", env.t_login("pwd_box", text="密码"), {"text": "abc"}),
        S.action_step("b2", "notify", None, {"message": "存一次"}),
    ]),
])
rep = env.run(sg)
loops = [r for r in rep["steps"] if r.get("type") == "loop" and "iterations" in r]
print(f"[批量填表] status={rep['status']} 输入次数={rep['counters']['types']}（期望 3）"
      f" 通知={env.human.notified} 循环迭代={[r['iterations'] for r in loops]}")
