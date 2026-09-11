# -*- coding: utf-8 -*-
"""
engine.ai — E7：云端 VLM 语义确认适配层（默认智谱 glm-4.6v）。

规格：《M1_引擎设计清单》§2/E7、§7-2；《项目分析文档》v0.31 §7.3 校验模式 +
v0.29 深测结论（归一化提示 71% 框内/清晰目标 ≤10px → 只作"粗圈"，精定位必须本地）。
迁移自 M0：m0_vlm_geo（prompt 归一化 + 解析）、m0_calib_demo/bili（双图语义确认）。

契约：
  - ZhipuVLM：真实云端（env ZHIPU_API_KEY）；未配 key 时 enabled=False，
    调用会按 _unauthorized 行为抛 EngineError(ai_not_authorized)。
  - 授权钩子：authorize()（首次启用显式授权 + 知情提示回调）/ revoke() /
    authorized 状态；引擎默认不自动调用云端（M3 产品开启，联调用）。
  - SemanticStub：确定性本地桩（无 key/离线测试），语义匹配 = 旧文字关键词
    （prefix/包含）在页内文本条带中定位 —— 供 E5 离线回归与无网环境降级。

AI 输出只用于：确认"目标语义仍在/改名/移动"并给出归一化建议位置（粗圈 ±px），
绝不直接作为点击坐标（v0.29 结论）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

import cv2
import numpy as np

from engine import matcher
from engine.errors import EngineError, ERRORS

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4.6v"
MAX_W = 1024                      # 上传前缩放（M0 bili/demo 实测口径）


def local_api_key() -> str:
    """从本地未跟踪文件读 Key（环境变量没设时的兜底）。

    为什么需要：Windows 的 `setx` 只对**之后新启动的进程**生效，而本项目的引擎是被
    已经运行着的宿主（Electron/DSH）拉起来的，它继承的是旧环境 —— setx 形同没设
    （实测：设完再跑，进程里读到的仍是空串）。
    文件放在 smoke/.secrets/*.env，已在 .gitignore 里，**绝不入库**。
    """
    try:
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for p in (root / "smoke" / ".secrets" / "zhipu.env",
                  root / ".secrets" / "zhipu.env"):
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("ZHIPU_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""
NORM_PROMPT = (
    "这是软件界面截图。请找到“{semantic}”的中心点，输出其相对坐标：横向比例与纵向比例，"
    "取值0~1之间（图片左上角为0,0，右下角为1,1）。"
    "如果图中没有这个目标，只输出：没有\n只输出两个小数：cx cy")

ERROR_KEY = {
    "ai_not_authorized": "云端 AI 未授权或未配置（需 API key 且用户显式授权）",
    "ai_call_failed": "云端 AI 调用失败",
    "ai_reply_unparsable": "AI 回复无法解析",
    "ai_target_absent": "AI 确认目标不存在",
}


# ---------------------------------------------------------------- 授权/知情提示

class AiGate:
    """AI 使用闸：显式授权 + 知情提示（v0.17/7.5：上传截图属授权范围）。

    notify: 可选回调 on_authorize(api_name, 说明) 在首次授权时调用（UI 弹窗/CLI 提示）。
    """

    def __init__(self, notify=None):
        self._authorized = False
        self.notify = notify

    @property
    def authorized(self) -> bool:
        return self._authorized

    def authorize(self, reason="运行自校准需要上传当前屏幕截图到云端做语义确认") -> bool:
        """显式授权（用户确认后调用）。返回是否完成授权。"""
        if not self._authorized:
            if self.notify is not None:
                ok = self.notify(reason)
                if not ok:
                    return False
            self._authorized = True
        return True

    def revoke(self) -> None:
        self._authorized = False

    def check(self) -> None:
        if not self._authorized:
            raise EngineError("ai_not_authorized", ERROR_KEY["ai_not_authorized"])


# ---------------------------------------------------------------- 提示/解析（纯函数，可单测）

def norm_prompt(semantic: str) -> str:
    return NORM_PROMPT.format(semantic=semantic)


# 第二问（只在本地用旧词找不到时才问）：目标现在叫什么。
# 为什么不合并到第一问里：第一问的归一化坐标格式是实测调出来的（M0：框内 71%），
# 往里加字段会拿坐标可靠性冒险；而"改名"是少数情况，多一次往返换格式稳定值得。
RENAME_PROMPT = (
    "图1是一个按钮或菜单项**原来的样子**，它那时候叫「{semantic}」。\n"
    "图2是同一个界面**现在的样子**，这个按钮可能改了名字，也可能挪了位置。\n"
    "请找出图2里对应的那个按钮，只回答它**现在的文字**：\n"
    "不要解释、不要标点、不要引号，只写那几个字。\n"
    "如果图2里确实找不到对应项，只回答两个字：没有"
)

# 带候选词的版本（**首选**）：本地已经 OCR 出界面上有哪些字，让模型做选择题，
# 而不是凭空生成一个词。实测教训：让 glm-4.6v 自由回答"它现在叫什么"时，
# 它会答"目标不存在"（改名后的语义它认不出是同一个东西）；给它候选词之后
# 就变成了"从这几个里挑一个"——这正是它擅长的（D3 标签分类 92%）。
RENAME_PROMPT_OPTIONS = (
    "图1是一个按钮或菜单项**原来的样子**，它那时候叫「{semantic}」。\n"
    "图2是同一个界面**现在的样子**，同一个按钮的名字被改过了。\n"
    "图2界面上能看到的文字有：{options}\n"
    "请从上面这些文字里，挑出**图1那个按钮现在叫什么**，只回答那一个词：\n"
    "不要解释、不要标点、不要引号。\n"
    "如果上面确实没有对应的，只回答两个字：没有"
)


def rename_prompt(semantic: str, options=None) -> str:
    """候选词优先；没有候选词时退回自由回答版。"""
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if opts:
        return RENAME_PROMPT_OPTIONS.format(semantic=semantic,
                                            options="、".join(opts))
    return RENAME_PROMPT.format(semantic=semantic)


def parse_rename_reply(reply):
    """解析"现在叫什么"的回复。返回 {ok, text, reason}。

    取**最后一行够短的内容**：模型偶尔会先说一句"好的，我看看"再给答案，
    而答案通常在最后。够短（≤12 字）优先，避免把一句客套话当名字。
    """
    raw = str(reply or "").strip()
    if not raw:
        return {"ok": False, "text": "", "reason": "empty"}
    strip_chars = '"\'“”‘’`*#-—:：,，.。!！?？ \t「」『』《》'
    cands = []
    for line in raw.splitlines():
        t = line.strip().strip(strip_chars)
        if not t:
            continue
        if t in ("没有", "找不到", "无", "不存在", "未找到"):
            return {"ok": False, "text": "", "reason": "absent"}
        cands.append(t)
    if not cands:
        return {"ok": False, "text": "", "reason": "empty"}
    short = [t for t in cands if len(t) <= 12]
    if short:
        return {"ok": True, "text": short[-1], "reason": ""}
    last = cands[-1]
    if len(last) > 24:                       # 一整句话 → 不是我们要的答案
        return {"ok": False, "text": "", "reason": "too_long", "raw": last[:40]}
    return {"ok": True, "text": last, "reason": ""}


def parse_norm_reply(reply: str):
    """
    解析归一化回复。返回 {ok, xy(0~1), reason}；
    “没有”类否定 → ok False reason=absent；数字越界/缺失 → unparsable。
    """
    if reply is None:
        return {"ok": False, "xy": None, "reason": "unparsable"}
    text = str(reply).strip().replace(",", " ").replace("，", " ").replace("：", ":")
    if "没有" in text or "不存在" in text or "未找到" in text or "无法" in text:
        return {"ok": False, "xy": None, "reason": "absent"}
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if len(nums) < 2:
        return {"ok": False, "xy": None, "reason": "unparsable"}
    cx, cy = nums[0], nums[1]
    if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
        return {"ok": False, "xy": None, "reason": "unparsable"}
    return {"ok": True, "xy": (cx, cy), "reason": ""}


# ---------------------------------------------------------------- 云端用量计量

class CloudMeter:
    """云端用量计量：调用次数 / token / 延迟 / 失败。

    为什么要单独拎出来：验收口径要求把"云端调用次数与成本"计入指标（用户 2026-09-11 定），
    而这些数只有真实调用点知道。拎成一个类就能**离线测**（喂假响应即可），不必真发请求；
    也避免把统计逻辑散落进各处调用点。

    計数口径：**每一次真实发出的请求算一次调用**（含失败）——失败也要计入成本，
    因为它同样消耗了时间、也可能已经计费。
    """

    def __init__(self):
        self.calls = 0
        self.errors = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.ms_total = 0.0

    def record(self, ok: bool, usage=None, elapsed_ms=None) -> None:
        self.calls += 1
        if not ok:
            self.errors += 1
        u = usage or {}
        if isinstance(u, dict):
            self.prompt_tokens += int(u.get("prompt_tokens") or 0)
            self.completion_tokens += int(u.get("completion_tokens") or 0)
            self.total_tokens += int(u.get("total_tokens") or 0)
        if elapsed_ms:
            self.ms_total += float(elapsed_ms)

    def snapshot(self) -> dict:
        return {"calls": self.calls, "errors": self.errors,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "ms_total": round(self.ms_total, 1),
                "ms_avg": round(self.ms_total / self.calls, 1) if self.calls else 0.0,
                "tokens_avg": (round(self.total_tokens / self.calls, 1)
                               if self.calls else 0.0)}

    def reset(self) -> None:
        self.__init__()


# ---------------------------------------------------------------- 真实云端（智谱）

class ZhipuVLM:
    """智谱 glm-4.6v 适配：confirm_target(旧部件图 + 当前屏, semantic) → 归一化建议。"""

    def __init__(self, api_key=None, model=DEFAULT_MODEL, gate=None, timeout_s=240.0,
                 meter=None):
        self.api_key = api_key if api_key is not None else (
            os.environ.get("ZHIPU_API_KEY", "") or local_api_key())
        self.model = model
        self.gate = gate if gate is not None else AiGate()
        self.timeout_s = timeout_s
        self.meter = meter if meter is not None else CloudMeter()

    def stats(self) -> dict:
        """本次（或自上次 reset 起）的云端用量。"""
        return {"model": self.model, **self.meter.snapshot()}

    def reset_stats(self) -> None:
        self.meter.reset()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.gate.authorized

    def authorize(self, notify=None) -> bool:
        """授权（含知情提示）。notify 为 None 时用 gate 自带回调。"""
        if notify is not None:
            self.gate.notify = notify
        return self.gate.authorize()

    def revoke(self) -> None:
        self.gate.revoke()

    def _chat(self, prompt: str, images_bgr, max_w=MAX_W):
        """多图视觉请求。返回 {ok, text, elapsed_ms, error?}（m0 原语迁移）。"""
        content = [{"type": "text", "text": prompt}]
        for img in images_bgr:
            h, w = img.shape[:2]
            if w > max_w:
                img = cv2.resize(img, (max_w, int(h * max_w / w)))
            okb, buf = cv2.imencode(".png", img)
            if not okb:
                return {"ok": False, "text": "", "elapsed_ms": 0.0,
                        "error": "png_encode_failed"}
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," +
                                          base64.b64encode(buf.tobytes()).decode("ascii")}})
        body = {"model": self.model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.0}
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            ms = round((time.perf_counter() - t0) * 1000)
            usage = out.get("usage") or {}
            self.meter.record(True, usage, ms)      # 计量：次数/token/延迟
            return {"ok": True, "text": out["choices"][0]["message"]["content"],
                    "elapsed_ms": ms, "usage": usage}
        except Exception as e:
            detail = ""
            if hasattr(e, "read"):
                try:
                    detail = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
            ms = round((time.perf_counter() - t0) * 1000)
            self.meter.record(False, None, ms)      # 失败的调用也要计入（同样花时间/可能计费）
            return {"ok": False, "text": "", "elapsed_ms": ms,
                    "error": repr(e), "detail": detail}

    def ask(self, prompt: str, images_bgr, max_w=MAX_W) -> dict:
        """通用视觉问答：只负责把问题和图发出去、把回复原样带回来。

        提示词与回复解析留在调用方（如 engine/outcome.py 的结果语义判定）——
        这里是**传输层**，换模型/换接口时不必动语义逻辑。
        """
        if not self.enabled:
            raise EngineError("ai_not_authorized", ERROR_KEY["ai_not_authorized"])
        return self._chat(prompt, images_bgr, max_w=max_w)

    def ask_rename(self, old_widget_bgr, screen_bgr, semantic: str, options=None) -> dict:
        """第二问：这个目标在现在的界面上叫什么？（改名恢复用）

        options: 本地 OCR 出来的候选词。**给了就做选择题**（实测可靠得多），
                 没给就只能让模型自由回答（它经常答"目标不存在"）。
        返回 {ok, text(现在的文字), note, elapsed_ms, reply}；absent/解析不出 → ok False。
        只在本地拿旧词找不到时才调用，所以它慢一点没关系。
        """
        if not self.enabled:
            raise EngineError("ai_not_authorized", ERROR_KEY["ai_not_authorized"])
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        imgs = [img for img in (old_widget_bgr, screen_bgr) if img is not None]
        r = self._chat(rename_prompt(semantic, opts), imgs)
        if not r["ok"]:
            return {"ok": False, "text": "", "note": f"AI 调用失败 {r.get('error')}",
                    "elapsed_ms": r.get("elapsed_ms"), "reply": ""}
        p = parse_rename_reply(r["text"])
        if not p["ok"]:
            return {"ok": False, "text": "", "reason": p["reason"],
                    "note": f"没问出现在的名字（{p['reason']}）",
                    "elapsed_ms": r.get("elapsed_ms"), "reply": (r["text"] or "")[:120]}
        text = p["text"]
        if opts:                      # 回复必须落在候选词里，不能凭空造
            best = max(opts, key=lambda o: matcher.text_similar(text, o))
            if matcher.text_similar(text, best) >= 0.6:
                text = best
        return {"ok": True, "text": text,
                "note": f"AI 说它现在叫「{text}」",
                "elapsed_ms": r.get("elapsed_ms"), "reply": (r["text"] or "")[:120]}

    def confirm_target(self, old_widget_bgr, screen_bgr, semantic: str,
                       hint_xy=None) -> dict:
        """
        双图语义确认（图1=旧部件截图；图2=当前屏幕/页面截图）。
        hint_xy: 可选页内录点（桩/本地优先邻域用；云端模型忽略）。
        返回 {ok, xy(原图像素，屏幕坐标), norm(0~1), conf?, elapsed_ms, note}
        AI 只作粗圈（v0.29：71% 框内）；ok=False + reason 供低置信人工兜底。
        """
        if not self.enabled:
            raise EngineError("ai_not_authorized", ERROR_KEY["ai_not_authorized"])
        prompt = norm_prompt(semantic)
        r = self._chat(prompt, [old_widget_bgr, screen_bgr])
        if not r["ok"]:
            raise EngineError("ai_call_failed", f"{ERROR_KEY['ai_call_failed']} {r.get('error')}")
        p = parse_norm_reply(r["text"])
        if not p["ok"]:
            if p["reason"] == "absent":
                return {"ok": False, "xy": None, "norm": None, "reason": "absent",
                        "note": "AI 语义确认：目标不存在", "elapsed_ms": r["elapsed_ms"],
                        "reply": (r["text"] or "")[:120]}
            return {"ok": False, "xy": None, "norm": None, "reason": "unparsable",
                    "note": "AI 回复无法解析", "elapsed_ms": r["elapsed_ms"],
                    "reply": (r["text"] or "")[:120]}
        h, w = screen_bgr.shape[:2]
        xy = (p["xy"][0] * w, p["xy"][1] * h)
        return {"ok": True, "xy": xy, "norm": p["xy"],
                "note": "AI 语义确认 + 归一化粗圈", "elapsed_ms": r["elapsed_ms"],
                "reply": (r["text"] or "")[:120]}


# ---------------------------------------------------------------- 确定性桩（离线/无 key 降级）

_ROW_BAND_H = 150
_ROW_BAND_STEP = 90
_HINT_RADIUS = (200, 120)


class SemanticStub:
    """
    本地语义桩：不联网的"语义确认 + 粗圈"替代。

    区域化扫描：每个区域（录点邻域圈 → 必要时整图行带）只 OCR 一次，
    再把区域内全部文字 token 与候选词集（旧名 + aliases）统一匹配——
    避免“逐词逐带”的重复 OCR（波次2 实测：逐词全页带扫可达 100s+/次）。

    语义改名恢复（库存查询→库存中心）依赖外部候选词 aliases（真实产品由云端
    AI 给出；桩=确定性模拟，用于离线测试与无网演示）。
    """

    def __init__(self, aliases=None):
        self.aliases = list(aliases or [])

    @staticmethod
    def _match(tokens, needles, thr=0.75):
        """tokens: [(box_xywh, txt, conf)]; needles: [词…]。返回 best 或 None。"""
        best = None
        for box, txt, _sc in tokens:
            for needle in needles:
                sim = matcher.text_similar(txt, needle)
                if sim >= thr and (best is None or sim > best["score"]):
                    best = {"score": sim, "needle": needle, "box": box,
                            "matched": txt}
        return best

    def _regions(self, img_bgr, hint_xy=None):
        """产出 (region_bgr, origin_xy) 序列：hint 圈 → 整图行带。"""
        h, w = img_bgr.shape[:2]
        if hint_xy is not None:
            cx, cy = hint_xy
            rx, ry = max(0, int(cx - _HINT_RADIUS[0])), max(0, int(cy - _HINT_RADIUS[1]))
            rw = min(_HINT_RADIUS[0] * 2, w - rx)
            rh = min(_HINT_RADIUS[1] * 2, h - ry)
            if rw >= 60 and rh >= 40:
                yield img_bgr[ry:ry + rh, rx:rx + rw, :], (rx, ry)
        y0 = 0
        while y0 < h:
            bh = min(_ROW_BAND_H, h - y0)
            if bh >= 40:
                yield img_bgr[y0:y0 + bh, :, :], (0, y0)
            y0 += _ROW_BAND_STEP

    def ask_rename(self, old_widget_bgr, screen_bgr, semantic: str, options=None) -> dict:
        """桩的"改名"回答：从 aliases（外加候选词）里挑一个出现在屏幕上的。"""
        text = (semantic or "").strip()
        needles = [a for a in list(self.aliases) + list(options or [])
                   if a and a != text]
        if not needles:
            return {"ok": False, "text": "", "reason": "absent",
                    "note": "语义桩：没有候选改名", "elapsed_ms": 0.0, "reply": ""}
        for region, (ox, oy) in self._regions(screen_bgr):
            r = matcher.ocr_run(region)
            if not r.get("boxes"):
                continue
            tokens = [(bx, txt, sc) for bx, txt, sc in
                      zip(r["boxes"], r["txts"], r["scores"])]
            hit = self._match(tokens, needles)
            if hit:
                return {"ok": True, "text": hit["matched"],
                        "note": f"语义桩：它现在叫「{hit['matched']}」",
                        "elapsed_ms": 0.0, "reply": ""}
        return {"ok": False, "text": "", "reason": "absent",
                "note": "语义桩：屏幕上没找到候选改名", "elapsed_ms": 0.0, "reply": ""}

    def confirm_target(self, old_widget_bgr, screen_bgr, semantic: str,
                       hint_xy=None) -> dict:
        text = (semantic or "").strip()
        needles = [text] + self.aliases
        needles = [n for n in needles if n]
        h, w = screen_bgr.shape[:2]
        errs = 0
        regions = 0
        for region, (ox, oy) in self._regions(screen_bgr, hint_xy=hint_xy):
            regions += 1
            r = matcher.ocr_run(region)
            if r.get("error"):
                errs += 1
                continue                            # 超时/熔断：跳过该区
            if not r["boxes"]:
                r2 = matcher.ocr_run(region)        # det 偶发空 → 重试一次
                if r2.get("error") or not r2["boxes"]:
                    if r2.get("error"):
                        errs += 1
                    continue
                r = r2
            tokens = [(bx, txt, sc) for bx, txt, sc in
                      zip(r["boxes"], r["txts"], r["scores"])]
            hit = self._match(tokens, needles)
            if hit:
                bx = hit["box"]
                xy = (ox + bx[0] + bx[2] // 2, oy + bx[1] + bx[3] // 2)
                return {"ok": True, "xy": xy,
                        "norm": (xy[0] / w, xy[1] / h),
                        "matched": hit["matched"],
                        "note": f"语义桩命中 {hit.get('matched')!r}",
                        "elapsed_ms": 0.0, "reply": ""}
        return {"ok": False, "xy": None, "norm": None, "reason": "absent",
                "note": f"语义桩：未找到任何候选词（OCR 错误 {errs}/{regions} 区）",
                "elapsed_ms": 0.0, "reply": ""}
