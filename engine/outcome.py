# -*- coding: utf-8 -*-
"""
engine.outcome — 结果语义判定 D3（M3-WP3）。

分层的职责（别混在一起）：

  D2（本地，已有）：**预期结果出现了吗** → ok / 没看到。纯本地，零延迟。
  D3（本模块）    ：**没看到的话，是哪一类失败** → 密码错 / 验证码错 / 断网 / 没出现 / 不确定。

为什么要分两层：这两个问题的难度差了一个量级。"预期的文字在不在屏幕上"本地
OCR 就能答；"屏幕上这句『账号或密码有误』意味着凭据错误而不是网络问题"需要
读懂语义。把后者塞进本地规则，就会变成一张永远追不上真实文案的关键词表。

三条硬约束（来自既定架构边界）：
  1. **本地判得出时绝不调用云端**：D2 已确认成功 → 直接返回，一次网络请求都不发。
  2. **D3 只输出语义标签，绝不输出坐标**。坐标一律走本地三级定位，云端不参与。
  3. **云端不可用（未授权/失败/超时）时不能崩，也不能假装判出来了** ——
     降级为「没出现/不确定」，交给人看。

另外保留一个可选的**本地文字预判**（text_first）：很多失败页面上写着现成的话
（"网络连接失败"），本地 OCR 认一下就够，比上传截图去问云端更快也更省。它默认
开启，但它只是"省一次云端调用"，不是替代 D3 —— 认不出来的仍然问云端。
"""
from __future__ import annotations

import time

from engine import matcher

# --------------------------------------------------------------------- 标签集

KIND_OK = "ok"                       # 成功：预期结果确实出现了
KIND_PASSWORD = "password_wrong"     # 凭据错误（用户名/密码不对）
KIND_CAPTCHA = "captcha_wrong"       # 验证码错误或失效
KIND_NETWORK = "no_network"          # 断网 / 连不上 / 超时
KIND_NOT_FOUND = "not_found"         # 预期的东西没出现，原因看不出
KIND_UNKNOWN = "unknown"             # 判不出来（低置信，交给人）

KINDS = (KIND_OK, KIND_PASSWORD, KIND_CAPTCHA, KIND_NETWORK, KIND_NOT_FOUND, KIND_UNKNOWN)

KIND_LABEL = {
    KIND_OK: "成功",
    KIND_PASSWORD: "密码错",
    KIND_CAPTCHA: "验证码错",
    KIND_NETWORK: "断网",
    KIND_NOT_FOUND: "没出现",
    KIND_UNKNOWN: "不确定",
}

# 模型可能回中文、英文、带标点或包一层 JSON；这些写法都算数
KIND_ALIASES = {
    "成功": KIND_OK, "登录成功": KIND_OK, "ok": KIND_OK, "success": KIND_OK,
    "密码错": KIND_PASSWORD, "密码错误": KIND_PASSWORD, "凭据错误": KIND_PASSWORD,
    "账号或密码错误": KIND_PASSWORD, "password_wrong": KIND_PASSWORD,
    "wrong_password": KIND_PASSWORD, "invalid_credentials": KIND_PASSWORD,
    "验证码错": KIND_CAPTCHA, "验证码错误": KIND_CAPTCHA, "captcha_wrong": KIND_CAPTCHA,
    "断网": KIND_NETWORK, "网络异常": KIND_NETWORK, "网络错误": KIND_NETWORK,
    "网络连接失败": KIND_NETWORK, "no_network": KIND_NETWORK, "network_error": KIND_NETWORK,
    "没出现": KIND_NOT_FOUND, "未出现": KIND_NOT_FOUND, "not_found": KIND_NOT_FOUND,
    "不确定": KIND_UNKNOWN, "无法判断": KIND_UNKNOWN, "unknown": KIND_UNKNOWN,
    "other": KIND_UNKNOWN, "其他": KIND_UNKNOWN, "无法确定": KIND_UNKNOWN,
}

OUTCOME_PROMPT = """你在看一张电脑屏幕的截图，请判断"刚才那一步操作的结果"是哪一类。

这一步本来要做的事：{intent}

只回答**一个词**，从下面这些里选，不要解释、不要标点、不要多写：
成功 / 密码错 / 验证码错 / 断网 / 没出现 / 不确定

判断依据是屏幕上实际显示的提示文字和界面状态：
- 屏幕上写着账号或密码不对 → 密码错
- 屏幕上写着验证码不对或已失效 → 验证码错
- 屏幕上写着网络/连接/超时问题 → 断网
- 屏幕上明确显示操作成功了（如出现欢迎页、成功提示） → 成功
- 看不出明显的失败原因，也没看到成功 → 没出现
- 以上都拿不准 → 不确定"""


def local_verdict(ok: bool, reason: str = "", last=None) -> dict:
    """把本地 D2 的判定包成与 D3 **同构**的结构。

    调用方（执行器/界面）不必区分这次的判定来自本地还是云端——字段一样，
    多出来的 `source` 只是让人知道它是谁给的。
    """
    kind = KIND_OK if ok else KIND_NOT_FOUND
    return {"ok": bool(ok), "kind": kind, "label": KIND_LABEL[kind],
            "reason": reason or ("seen" if ok else "timeout"),
            "source": "local", "confidence": None, "note": "",
            "elapsed_ms": 0.0, "last": last}


# --------------------------------------------------------------------- 回复解析


def parse_kind_reply(reply) -> dict:
    """把模型回复解析成标签。宽容一点：中英文、带引号/句号/JSON 都能认。"""
    raw = str(reply or "").strip()
    if not raw:
        return {"ok": False, "reason": "empty"}
    text = raw.strip().lower()
    for ch in '`"\'。.，,：:!！?？\n\r\t[]{}':
        text = text.replace(ch, " ")
    text = " ".join(text.split())
    if text in KIND_ALIASES:
        return {"ok": True, "kind": KIND_ALIASES[text], "raw": raw[:60]}
    # 回复里夹了别的话：按"最长匹配优先"扫一遍，避免「密码错」被「错」之类误伤
    best = None
    for needle, kind in KIND_ALIASES.items():
        n = needle.lower()
        if n and n in text and (best is None or len(n) > best[0]):
            best = (len(n), kind)
    if best is not None:
        return {"ok": True, "kind": best[1], "raw": raw[:60]}
    return {"ok": False, "reason": "unparsable", "raw": raw[:60]}


# --------------------------------------------------------------------- 本地文字预判

# 顺序即优先级：验证码和凭据错误可能同页出现，先认更具体的
_TEXT_RULES = (
    (KIND_CAPTCHA, ("验证码错误", "验证码不正确", "验证码已失效", "验证码失效",
                    "captcha")),
    (KIND_PASSWORD, ("密码错误", "密码不正确", "密码有误", "用户名或密码", "账号或密码",
                     "账户或密码", "凭据无效", "password is incorrect",
                     "invalid credentials", "wrong password")),
    (KIND_NETWORK, ("网络连接失败", "网络异常", "无法连接", "连接失败", "请求超时",
                    "网络错误", "断网", "network error", "connection failed",
                    "timed out", "timeout")),
    (KIND_OK, ("登录成功", "操作成功", "提交成功", "已完成", "欢迎回来", "success")),
)


def kind_from_text(text_or_tokens) -> str:
    """屏幕上写着的字能不能直接说明结果类型？认不出返回 ""（交给云端）。"""
    if isinstance(text_or_tokens, str):
        blob = text_or_tokens
    else:
        blob = " ".join(str(t) for t in (text_or_tokens or []))
    blob = blob.lower()
    if not blob.strip():
        return ""
    for kind, needles in _TEXT_RULES:
        for n in needles:
            if n.lower() in blob:
                return kind
    return ""


# --------------------------------------------------------------------- 判定器


class OutcomeJudge:
    """D3 判定器：本地 D2 优先 → 本地文字预判 → 云端 glm-4.6v → 降级交给人。"""

    def __init__(self, vlm=None, text_first=True, notify=None):
        self.vlm = vlm
        self.text_first = bool(text_first)
        self.notify = notify or (lambda *a, **k: None)

    @property
    def ai_ready(self) -> bool:
        return bool(self.vlm is not None and getattr(self.vlm, "enabled", False))

    def judge(self, screen_bgr, intent: str = "", local=None, ocr_fn=None) -> dict:
        """判定一次操作的结果。

        screen_bgr : 当前屏幕/页面截图（BGR）
        intent     : 这一步本来想做什么（人话；模型靠它对齐预期）
        local      : 本地 D2 的判定结果（local_verdict 的形状）；缺省表示本地没判
        返回统一结构：{ok, kind, label, reason, source, confidence, note, elapsed_ms}
        """
        t0 = time.perf_counter()
        local = local or {}
        if local.get("ok") is True:
            out = local_verdict(True, local.get("reason") or "seen", local.get("last"))
            out["note"] = "本地已确认预期结果出现（不调用云端）"
            out["intent"] = intent
            return out

        reason = local.get("reason") or "missing"

        if self.text_first:
            kind = self._kind_from_screen(screen_bgr, ocr_fn)
            if kind:
                return self._mk(kind, "local_text", reason, intent, t0,
                                note=f"屏幕文字直接说明了「{KIND_LABEL[kind]}」（不调用云端）")

        if not self.ai_ready:
            out = self._mk(KIND_NOT_FOUND if reason else KIND_UNKNOWN, "local", reason,
                           intent, t0, note="本地判不出，云端不可用（未授权或未接通）")
            return out

        prompt = OUTCOME_PROMPT.format(intent=(intent or "（未说明）").strip())
        try:
            r = self.vlm.ask(prompt, [screen_bgr]) if screen_bgr is not None \
                else {"ok": False, "error": "no_screen"}
        except Exception as e:
            r = {"ok": False, "error": repr(e)}
        if not r.get("ok"):
            return self._mk(KIND_UNKNOWN, "ai", reason, intent, t0,
                            note=f"云端判定失败（{r.get('error')}），交给人看")
        p = parse_kind_reply(r.get("text"))
        if not p.get("ok"):
            return self._mk(KIND_UNKNOWN, "ai", reason, intent, t0,
                            note="云端回复无法解析，交给人看", reply=p.get("raw", ""))
        return self._mk(p["kind"], "ai", reason, intent, t0,
                        note="云端语义判定", reply=p.get("raw", ""),
                        elapsed_ms=r.get("elapsed_ms"))

    # ---- 内部

    def _kind_from_screen(self, screen_bgr, ocr_fn=None) -> str:
        if screen_bgr is None:
            return ""
        try:
            if ocr_fn is not None:
                txts = ocr_fn(screen_bgr)
            else:
                r = matcher.ocr_run(screen_bgr)
                if not r.get("ok"):
                    return ""
                txts = r.get("txts") or []
        except Exception:
            return ""
        return kind_from_text(txts)

    def _mk(self, kind, source, reason, intent, t0, note="", reply="",
            elapsed_ms=None) -> dict:
        ms = elapsed_ms if elapsed_ms is not None else round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": kind == KIND_OK, "kind": kind, "label": KIND_LABEL.get(kind, kind),
                "reason": reason, "source": source, "confidence": None,
                "note": note, "reply": reply, "elapsed_ms": ms, "intent": intent}
