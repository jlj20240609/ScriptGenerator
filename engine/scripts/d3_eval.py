# -*- coding: utf-8 -*-
"""
engine/scripts/d3_eval.py — M3-WP3：D3 结果语义判定的准确率评测。

用法：
  python engine/scripts/d3_eval.py --dry            # 只跑本地路径，不联网
  python engine/scripts/d3_eval.py                  # 本地 + 云端（glm-4.6v）各跑一遍
  python engine/scripts/d3_eval.py --repeat 2       # 每例重复 2 次（看稳定性）

产出：
  docs/M3_D3验收数据.md        —— 准确率、混淆情况、延迟、失败样本（WP3 的交付物）
  smoke/data/d3_eval.jsonl     —— 逐例原始记录（含模型原话，便于复盘）

为什么用合成屏幕而不是真机截图：判定结果要可复现、可回归。合成屏幕里"屏幕上写着
什么"是确定的，所以"判错了"一定是判定的问题，不会和"截图时机/窗口位置"混在一起。
合成屏幕也方便**故意做成含蓄的**——真实失败页往往不写"密码错误"这种现成的词，
而是写"认证未通过"，那才是 D3 存在的理由，拿直白文案测等于白测。

为什么不把"直白文案"的用例删掉：它们是 D3 的**降级路径**（本地文字预判）的度量，
两类分开报，才能说清"什么时候没花云端的钱"。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import ai as AI              # noqa: E402
from engine import outcome as O          # noqa: E402
from engine.tests import support as S    # noqa: E402

DATA = ROOT / "smoke" / "data" / "d3_eval.jsonl"
DOC = ROOT / "docs" / "M3_D3验收数据.md"
INTENT_LOGIN = "打开系统的登录页，填好用户名和密码，点「登录」按钮"
INTENT_OK = "在首页点「订单管理」，看看能不能进去"

RED = (200, 40, 40)
GREEN = (20, 140, 70)
GRAY = (110, 118, 130)


def _login_page(msg="", msg_color=RED, fields=True):
    """画一个像样的登录页：标题栏 + 白色卡片 + 输入框 + 按钮（+ 可选提示语）。"""

    def deco(d, img):
        w = img.size[0]
        d.rectangle((60, 110, w - 60, 470), fill=(255, 255, 255), outline=(228, 232, 240))
        S.draw_text(img, 100, 150, "用户名", size=20, fill=GRAY)
        d.rectangle((200, 145, 620, 180), fill=(248, 250, 253), outline=(220, 226, 236))
        S.draw_text(img, 100, 215, "密码", size=20, fill=GRAY)
        d.rectangle((200, 210, 620, 245), fill=(248, 250, 253), outline=(220, 226, 236))
        d.rectangle((200, 275, 400, 320), fill=(60, 110, 200))
        S.draw_text(img, 268, 288, "登录", size=22, fill=(255, 255, 255))
        if fields:
            S.draw_text(img, 100, 265, "", size=18, fill=GRAY)
        if msg:
            S.draw_text(img, 200, 355, msg, size=22, fill=msg_color)

    return S.make_page(w=760, h=520, bg=(244, 246, 251), header_text="示例公司 · 员工系统",
                       deco=deco)


def _captcha_page(msg):
    def deco(d, img):
        w = img.size[0]
        d.rectangle((60, 110, w - 60, 470), fill=(255, 255, 255), outline=(228, 232, 240))
        S.draw_text(img, 100, 150, "用户名", size=20, fill=GRAY)
        d.rectangle((200, 145, 620, 180), fill=(248, 250, 253), outline=(220, 226, 236))
        S.draw_text(img, 100, 215, "密码", size=20, fill=GRAY)
        d.rectangle((200, 210, 620, 245), fill=(248, 250, 253), outline=(220, 226, 236))
        S.draw_text(img, 100, 275, "验证码", size=20, fill=GRAY)
        d.rectangle((200, 270, 380, 305), fill=(248, 250, 253), outline=(220, 226, 236))
        d.rectangle((400, 270, 520, 305), fill=(235, 238, 245), outline=(220, 226, 236))
        S.draw_text(img, 412, 278, "8F3K", size=20, fill=(70, 78, 92))
        d.rectangle((200, 335, 400, 380), fill=(60, 110, 200))
        S.draw_text(img, 268, 348, "登录", size=22, fill=(255, 255, 255))
        if msg:
            S.draw_text(img, 200, 405, msg, size=22, fill=RED)

    return S.make_page(w=760, h=520, bg=(244, 246, 251), header_text="示例公司 · 员工系统",
                       deco=deco)


def _home_page():
    """登录后的首页：**故意不写"成功/欢迎回来"**，只有一个已登录才可能看到的工作台。

    这才是 D3 存在的理由：屏幕上看不到任何"成功"字样，但页面本身说明了操作成功了。
    本地关键词表永远追不上这种表达，语义判定才能。
    """

    def deco(d, img):
        S.draw_text(img, 40, 82, "张三 · 员工", size=22, fill=(30, 40, 60))
        S.draw_text(img, 600, 82, "退出", size=18, fill=GRAY)
        for i, name in enumerate(["工作台", "订单管理", "库存查询", "我的审批"]):
            d.rectangle((40, 140 + i * 66, 320, 190 + i * 66),
                        fill=(255, 255, 255), outline=(228, 232, 240))
            S.draw_text(img, 60, 156 + i * 66, name, size=22, fill=(50, 60, 80))
        d.rectangle((360, 140, 700, 420), fill=(255, 255, 255), outline=(228, 232, 240))
        S.draw_text(img, 384, 162, "今日待办 3 项", size=20, fill=(50, 60, 80))
        S.draw_text(img, 384, 200, "· 采购单待审批", size=18, fill=GRAY)
        S.draw_text(img, 384, 232, "· 库存盘点待确认", size=18, fill=GRAY)

    return S.make_page(w=760, h=520, bg=(244, 246, 251), header_text="示例公司 · 员工系统",
                       deco=deco)


def cases():
    """用例集：每一类都既有**直白**（本地关键词能判）也有**含蓄**（只能靠语义）。"""
    return [
        # ---- 成功
        dict(name="成功/明确提示", truth=O.KIND_OK, intent=INTENT_LOGIN, local_hit=True,
             img=_login_page("登录成功，正在进入系统", GREEN)),
        dict(name="成功/含蓄（首页没有任何「成功」字样）", truth=O.KIND_OK,
             intent=INTENT_LOGIN, local_hit=False, img=_home_page()),
        # ---- 凭据错误
        dict(name="凭据错/直白", truth=O.KIND_PASSWORD, intent=INTENT_LOGIN, local_hit=True,
             img=_login_page("用户名或密码错误")),
        dict(name="凭据错/含蓄1", truth=O.KIND_PASSWORD, intent=INTENT_LOGIN, local_hit=False,
             img=_login_page("登录失败，请检查后重试")),
        dict(name="凭据错/含蓄2", truth=O.KIND_PASSWORD, intent=INTENT_LOGIN, local_hit=False,
             img=_login_page("认证未通过，请确认账号信息")),
        # ---- 验证码
        dict(name="验证码/直白", truth=O.KIND_CAPTCHA, intent=INTENT_LOGIN, local_hit=True,
             img=_captcha_page("验证码错误，请重新输入")),
        dict(name="验证码/含蓄", truth=O.KIND_CAPTCHA, intent=INTENT_LOGIN, local_hit=False,
             img=_captcha_page("图形校验未通过")),
        # ---- 断网
        dict(name="断网/直白", truth=O.KIND_NETWORK, intent=INTENT_LOGIN, local_hit=True,
             img=_login_page("网络连接失败，请检查网络")),
        dict(name="断网/含蓄1", truth=O.KIND_NETWORK, intent=INTENT_LOGIN, local_hit=False,
             img=_login_page("服务器开小差了，请稍后再试")),
        dict(name="断网/含蓄2", truth=O.KIND_NETWORK, intent=INTENT_LOGIN, local_hit=False,
             img=_login_page("连接被重置，请稍后重试")),
        # ---- 没出现（看不出原因）
        dict(name="没出现/什么都没发生", truth=O.KIND_NOT_FOUND, intent=INTENT_LOGIN,
             local_hit=True, img=_login_page("")),
        dict(name="没出现/表单校验提示", truth=O.KIND_NOT_FOUND, intent=INTENT_LOGIN,
             local_hit=True, img=_login_page("请先填写用户名")),
    ]


def _pct(n, d):
    return f"{n / d * 100:.0f}%" if d else "—"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="D3 结果语义判定评测")
    ap.add_argument("--dry", action="store_true", help="只跑本地路径（不联网、不花钱）")
    ap.add_argument("--repeat", type=int, default=1, help="每例重复几次（看稳定性）")
    ap.add_argument("--model", default=AI.DEFAULT_MODEL)
    ap.add_argument("--no-doc", action="store_true", help="不写验收文档")
    args = ap.parse_args(argv)

    items = cases()
    print(f"用例 {len(items)} 个，重复 {args.repeat} 次"
          f"{'（只跑本地，dry）' if args.dry else ''}\n")

    vlm = None
    if not args.dry:
        key = AI.ZhipuVLM().api_key
        if not key:
            print("× 没有可用的 API Key（smoke/.secrets/zhipu.env 或 ZHIPU_API_KEY），"
                  "改用 --dry 只跑本地路径。")
            return 2
        vlm = AI.ZhipuVLM(api_key=key, model=args.model)
        if not vlm.authorize(notify=lambda reason: True):
            print("× 未获授权，不调用云端。")
            return 2
        print(f"云端：{args.model}（已授权，会上传合成截图）\n")

    loc = O.OutcomeJudge(vlm=None, text_first=True)
    cloud = O.OutcomeJudge(vlm=vlm, text_first=False) if vlm else None

    rows = []
    loc_ok = loc_escalated = 0
    cloud_ok = cloud_n = 0
    t_cloud = []
    for c in items:
        lv = loc.judge(c["img"], c["intent"], local=O.local_verdict(False, "timeout"))
        l_hit = lv["kind"] == c["truth"]
        loc_ok += 1 if l_hit else 0
        if lv["source"] != "local_text":
            loc_escalated += 1
        row = {"name": c["name"], "truth": c["truth"], "intent": c["intent"],
               "local_kind": lv["kind"], "local_source": lv["source"],
               "local_hit": l_hit, "expect_local": bool(c["local_hit"])}
        if cloud is not None:
            hits, kinds, replies = 0, [], []
            for _ in range(max(1, args.repeat)):
                cv = cloud.judge(c["img"], c["intent"],
                                 local=O.local_verdict(False, "timeout"))
                kinds.append(cv["kind"])
                replies.append(cv.get("reply", ""))
                t_cloud.append(cv["elapsed_ms"])
                if cv["kind"] == c["truth"]:
                    hits += 1
            cloud_n += 1
            cloud_ok += 1 if hits * 2 > max(1, args.repeat) else 0
            row.update({"cloud_kinds": kinds, "cloud_replies": replies,
                        "cloud_hit": hits == max(1, args.repeat)})
            flag = "✓" if row["cloud_hit"] else "✗"
            print(f"  {flag} {c['name']:<28} 真值={O.KIND_LABEL[c['truth']]:<5} "
                  f"本地={O.KIND_LABEL[lv['kind']]:<5}({lv['source']}) "
                  f"云端={[O.KIND_LABEL[k] for k in kinds]} {replies[:1]}")
        else:
            flag = "✓" if l_hit else "✗"
            print(f"  {flag} {c['name']:<28} 真值={O.KIND_LABEL[c['truth']]:<5} "
                  f"本地={O.KIND_LABEL[lv['kind']]:<5}({lv['source']})")
        rows.append(row)

    n = len(items)
    text_hits = sum(1 for r in rows if r["local_source"] == "local_text"
                    and r["local_kind"] == r["truth"])
    print(f"\n本地路径（文字预判）：判对 {loc_ok}/{n} = {_pct(loc_ok, n)}")
    print(f"  · 其中靠屏幕现成文字判对 {text_hits} 例（这些一次云端调用都不花）")
    print(f"  · {loc_escalated} 例本地认不出来 → 真实场景会去问云端")
    if cloud is not None:
        print(f"云端路径（{args.model}，关掉文字预判）：判对 {cloud_ok}/{cloud_n} = "
              f"{_pct(cloud_ok, cloud_n)}；平均延迟 "
              f"{sum(t_cloud) / max(1, len(t_cloud)):.0f}ms")
        hard = [r for r in rows if not r["expect_local"]]
        h_ok = sum(1 for r in hard if r.get("cloud_hit"))
        print(f"其中**含蓄用例**（本地一定认不出、只能靠语义）：判对 {h_ok}/{len(hard)} = "
              f"{_pct(h_ok, len(hard))}")

    DATA.parent.mkdir(parents=True, exist_ok=True)
    with DATA.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if cloud is not None:
            f.write(json.dumps({"type": "summary", "model": args.model,
                                "local_ok": loc_ok, "local_n": n,
                                "cloud_ok": cloud_ok, "cloud_n": cloud_n,
                                "avg_ms": round(sum(t_cloud) / max(1, len(t_cloud)), 1),
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False) + "\n")
    print(f"\n逐例记录：{DATA.relative_to(ROOT)}")
    if not args.dry and not args.no_doc:
        write_doc(rows, args, loc_ok, n, loc_escalated, cloud_ok, cloud_n, t_cloud)
        print(f"验收数据：{DOC.relative_to(ROOT)}")
    return 0


def write_doc(rows, args, loc_ok, n, loc_escalated, cloud_ok, cloud_n, t_cloud):
    hard = [r for r in rows if not r["expect_local"]]
    h_ok = sum(1 for r in hard if r.get("cloud_hit"))
    lines = [
        "# M3 D3 结果语义判定 · 验收数据",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　模型：`{args.model}`　"
        f"每例重复：{args.repeat} 次",
        "",
        "## 这一层在做什么",
        "",
        "本地 D2 只回答「预期的东西出现了没有」；D3 回答「没出现的话，是哪一类失败」。",
        "两者输出**同构**（`{ok, kind, label, reason, source, ...}`），所以调用方不必",
        "区分这次判定来自本地还是云端。",
        "",
        "## 结果",
        "",
        "| 路径 | 判对 | 说明 |",
        "| --- | --- | --- |",
        f"| 本地文字预判 | {loc_ok}/{n} = {_pct(loc_ok, n)} | 屏幕上写着现成的话，不花云端调用 |",
        f"| 云端 {args.model}（关掉预判） | {cloud_ok}/{cloud_n} = {_pct(cloud_ok, cloud_n)} | "
        f"平均延迟 {sum(t_cloud) / max(1, len(t_cloud)):.0f}ms |",
        f"| 其中**含蓄用例**（本地必认不出） | {h_ok}/{len(hard)} = {_pct(h_ok, len(hard))} | "
        "真实失败页往往不写「密码错误」这种现成的词 |",
        "",
        "M0 的 D3 预研基线是 12 例纯文本 10/12（83%）。",
        "",
        "## 逐例",
        "",
        "| 用例 | 真值 | 本地 | 云端 | 模型原话 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        ck = r.get("cloud_kinds")
        cloud_cell = "—" if not ck else ("，".join(O.KIND_LABEL[k] for k in ck)
                                         + ("" if r.get("cloud_hit") else " ✗"))
        lines.append(f"| {r['name']} | {O.KIND_LABEL[r['truth']]} | "
                     f"{O.KIND_LABEL[r['local_kind']]}（{r['local_source']}） | "
                     f"{cloud_cell} | {(r.get('cloud_replies') or [''])[0][:24]} |")
    lines += [
        "",
        "## 架构边界（本层不越线）",
        "",
        "- **云端只出语义标签，不出坐标**：判定结果里没有任何坐标字段，也有测试钉住",
        "  （`engine/tests/test_outcome.py::ArchitectureBoundaryTest`）。",
        "  坐标一律走本地三级定位，这是既定边界。",
        "- **本地判得出时绝不调用云端**：D2 确认成功、或本地文字已说明原因时，",
        "  一次网络请求都不发（有测试钉住，不是靠自觉）。",
        "- **降级必须交给人**：云端未授权 / 超时 / 回复看不懂时，降级为",
        "  「没出现 / 不确定」，绝不假装判出来了。",
        "",
        "## 已知局限（如实记录）",
        "",
        "- 用例集是**合成屏幕**：可复现、可回归，但覆盖面有限；真实产品的失败页",
        "  文案千变万化，本表的数字是下限性质的参考，不是上线保证。",
        "- 分类里没有「账号被锁」「需要二次验证」等类别——现有标签集只覆盖",
        "  文档点名的几类；遇到集外情况模型会落到「不确定」，由人工接手。",
    ]
    DOC.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
