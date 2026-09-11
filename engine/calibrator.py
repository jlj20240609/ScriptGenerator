# -*- coding: utf-8 -*-
"""
engine.calibrator — E5：校验模式子流程（自动重校准，写回前自检）。

规格：《M1_引擎设计清单》§3 校验入口 / §7；《项目分析文档》v0.31 §7.3 校验模式数据流：
  定位日志 → 失败触发 → AI 确认（旧部件图+当前屏，归一化建议）→ 本地精定位圈区 →
  更新 Target 全量 → 写回前自检（复用正常路径试定位）→ 通过才写回并继续；
  自检失败 / AI 低置信 → 人工兜底（引擎侧返回 ok=False，由 executor L1 白话提示；
  Electron 弹窗 confirm 在 UI 里程碑按 HumanIO 契约接入）。

本实现是对 executor 波次1 留的钩子契约为真的引擎模块：
  calibrator(CalibRequest) -> CalibResult
  CalibRequest{reason(first_run|page_not_found|widget_not_found|click_guard_failed),
               step, target, page_spec, page_rect, loc_rows, ctx, script}
  返回 {ok, updated, page_spec?, note?, detail}

恢复策略（reason 分支）：
  widget_not_found / click_guard_failed（页面已锁定）：
    AI/语义桩 双图粗圈 → 圈区本地找字（旧词/别名/短前缀）或模板 →
    命中：重采集部件模板 + 页内矩形/中心/文字写回（页对象就地刷新）→ 自检 → updated
  page_not_found（整窗失配，动态/改版）：
    AI/语义桩 全屏粗圈找 widget 语义 → 圈区找字/模板 → 命中后若有窗口矩形
    (window_rect 提供者) 则整窗重采集页面模板刷新 page.image/rect/ts → 自检
  first_run：存在性基线（定位成功 → 无需写回；失败 → 走 widget 恢复）

依赖注入（合成屏/真实桌面同代码）：
  driver：grab_screen()/grab_rect()（ScreenDriver 协议）
  ai：confirm_target(old_widget_or_None, screen, semantic)（ZhipuVLM / SemanticStub）
  window_rect：可选 callable() -> (x,y,w,h)（真窗=窗口矩形；合成场景可注入页面矩形）
  anchor_dt_s：锚跨帧复验间隔（规格 ≥2s；测试可为 0）
"""
from __future__ import annotations

import time

from engine import ai as ai_mod
from engine import capture, locator, matcher, schema
from engine.errors import EngineError

# 本地精定位圈区半径（AI 归一化粗圈 → 圈区 → 本地 OCR/模板精定位；v0.29 结论）
TEXT_RADIUS = (220, 140)
# 动态页锚采集（§7.3：跨帧间隔 ≥2s、自匹配 ≥0.85 才采纳）
ANCHOR_STABLE_MIN = 0.85
ANCHOR_PAD_X = 200
ANCHOR_PAD_Y = 22
# 多锚采集（M2-WP2）：只录一个锚时，它误命中了没有任何东西能纠正；录 2~3 个**互相分散**的锚，
# 运行时用它们互相印证（engine/locator.anchor_consensus）。候选按"横条带"取：顶栏/工具条/
# 状态栏这类横带最稳定，越窄越不容易把下面的动态内容卷进来。
ANCHOR_MAX = 3                 # 锚数量上限（运行时要按锚数做整屏多尺度搜索，3 个已够印证）
ANCHOR_MIN_STD = 10.0          # 锚块纹理下限（纯色/空白块在整屏搜索时会到处匹配）
ANCHOR_DUP_SIM = 0.97          # 两块内容几乎相同 → 只留一个（重复图案提供不了独立证据）
ANCHOR_OVERLAP_MAX = 0.35      # 与已选锚的重叠面积占比上限（重叠太多 → 同样没有独立证据）
ANCHOR_BAND_MIN = (120, 24)    # 候选条带最小尺寸
ANCHOR_BAND_MAX_H = 64         # 候选条带高度上限
TPL_RADIUS_MUL = 5          # 模板圈区 = 部件尺寸 × 倍数（下限 160x90）
PAD = 10                    # 重采集部件外扩
ALIAS_SEARCH = ("", )       # 预留：AI 候选词


class Calibrator:
    def __init__(self, driver, ai=None, window_rect=None, anchor_dt_s=2.0,
                 semantic_aliases=None, loc_log=None):
        self.driver = driver
        self.ai = ai if ai is not None else ai_mod.SemanticStub(semantic_aliases)
        self.window_rect = window_rect          # callable() -> rect | None
        self.anchor_dt_s = anchor_dt_s
        self.loc_log = loc_log
        self.hangs = 0

    # ---------------------------------------------------------- 公共入口

    def __call__(self, request: dict) -> dict:
        reason = request.get("reason", "")
        target = request.get("target")
        page_spec = request.get("page_spec") or (target or {}).get("page")
        page_rect = request.get("page_rect")
        try:
            if reason == "first_run":
                return self._on_first_run(target, page_spec, page_rect)
            if reason in ("widget_not_found", "click_guard_failed"):
                return self._on_widget_lost(target, page_spec, page_rect)
            if reason == "page_not_found":
                return self._on_page_lost(target, page_spec)
            return {"ok": False, "updated": False, "note": f"未知 reason={reason}"}
        except EngineError as e:
            return {"ok": False, "updated": False, "note": str(e)}
        except Exception as e:      # 校准失败不应让运行崩溃 → 人工兜底
            return {"ok": False, "updated": False, "note": f"校准异常: {e!r}"}

    # ---------------------------------------------------------- first_run

    def _on_first_run(self, target, page_spec, page_rect):
        """首次执行进一次校验（建立基线）：目标存在 → 无需写回；失效 → 走部件恢复。"""
        if not target:
            return {"ok": True, "updated": False, "note": "首次基线：无可校验目标"}
        screen, _ = self._grab()
        if page_spec is not None:
            r = locator.locate_page(screen, page_spec)
            if r["ok"]:
                page_rect = r["rect"]
        if page_rect is None:
            return self._on_page_lost(target, page_spec)     # 基线失效 → 恢复
        chk = locator.locate_widget_on_screen(screen, page_rect, target, exists=True)
        if chk["ok"]:
            return {"ok": True, "updated": False,
                    "note": f"首次基线 OK（{chk['method']}）"}
        return self._on_widget_lost(target, page_spec, page_rect)

    # ---------------------------------------------------------- widget 恢复

    def _on_widget_lost(self, target, page_spec, page_rect):
        """部件丢失（页内找不到 / 点击闸不过）→ AI 粗圈 + 本地精定位 + 重采集写回。"""
        if not target:
            return {"ok": False, "updated": False, "note": "无目标可校准"}
        screen, meta = self._grab()
        # 旧部件图（AI 双图图1）
        old_widget = None
        if target.get("image"):
            try:
                old_widget = matcher.dataurl_to_bgr(target["image"])
            except Exception:
                old_widget = None
        # 语义确认用“部件词”而非整句描述（桩按词匹配；云端 prompt 亦清晰）
        semantic = (target.get("text") or target.get("semantic") or "").strip()
        if not semantic:
            return {"ok": False, "updated": False, "note": "无目标可校准"}
        # 页面已锁定（widget 失败发生在页内）→ 直接用 executor 提供的 page_rect
        page_img = screen
        hint = None
        if page_rect is not None:
            x, y, w, h = [int(v) for v in page_rect]
            page_img = screen[y:y + h, x:x + w]
            rx = target.get("rect_in_page")
            if rx:
                hint = (rx[0] + rx[2] // 2, rx[1] + rx[3] // 2)
        conf = self._ai_coarse(old_widget, page_img, semantic, hint_xy=hint)
        candidates = []                     # 页面内圈心 (px)
        if page_rect is not None:
            if conf and conf.get("ok") and conf.get("xy"):
                x0, y0, _, _ = [int(v) for v in page_rect]
                candidates.append((conf["xy"][0] - x0, conf["xy"][1] - y0))
            rx = target.get("rect_in_page")
            if rx:
                candidates.append((rx[0] + rx[2] // 2, rx[1] + rx[3] // 2))
            candidates.append((page_img.shape[1] // 2, page_img.shape[0] // 2))
        # 2) 本地精定位：圈区内找旧词 → 短前缀/AI 确认词；模板兜底
        found = self._localize(target, page_img, candidates,
                               extra_needles=[(conf or {}).get("matched")])
        rename = None
        if not found["ok"]:
            # 旧词在圈区里找不到 → 很可能是**界面改了名**（库存查询→存货台账）。
            # 这时才问第二句"它现在叫什么"：多一次往返，但只在真正需要时花。
            # 把本地刚 OCR 到的词当**候选选项**交过去（让模型做选择题）——
            # 实测放开自由回答时它会答"目标不存在"，给选项后才是它擅长的题。
            rename = self._ai_rename(old_widget, page_img, semantic,
                                     options=found.get("seen"))
            if rename and rename.get("ok") and rename.get("text"):
                found = self._localize(target, page_img, candidates,
                                       extra_needles=[rename["text"]])
        if not found["ok"]:
            return {"ok": False, "updated": False,
                    "note": found.get("note", "本地精定位失败（低置信 → 人工兜底）"),
                    "detail": {"ai": (conf or {}).get("note"),
                               "rename": (rename or {}).get("note"),
                               "local": found}}
        box, method, matched = found["box"], found["method"], found.get("matched")
        # 3) 重采集部件模板 + 全量写回（页内矩形/中心/文字）
        self._rewrite_widget(target, page_img, box, matched)
        # 4) 页面就地刷新（部件级恢复不动整窗模板；仅刷新元数据）
        if page_rect is not None and page_spec is not None:
            self._touch_page_meta(page_spec, page_rect, meta)
        # 5) 写回前自检（复用正常路径）
        if not self._selfcheck(target, screen, page_rect):
            return {"ok": False, "updated": False,
                    "note": "重采集后自检失败（已保留旧值 → 人工兜底）",
                    "detail": {"local": found}}
        return {"ok": True, "updated": True, "page_spec": target.get("page") or page_spec,
                "note": f"已自动重采集（{method}，文字 {matched or '-'}）并自检通过",
                "detail": {"local": found, "ai": (conf or {}).get("note"),
                           "rename": (rename or {}).get("note")}}

    # ---------------------------------------------------------- page 恢复

    def _on_page_lost(self, target, page_spec):
        """
        整窗模板失配（改版/动态）→ 语义粗圈找 widget → 恢复并写回。
        静态改版：整窗重采集（现有路径）；
        持续动态页（bili 型）：整窗必然再失配 → 在部件行带采集“静态锚”并写回
        page.anchors（跨帧复验 ≥0.85 才采纳）——之后运行由锚定位页面（v0.31 §7.3）。
        """
        screen, meta = self._grab()
        old_widget = None
        if target and target.get("image"):
            try:
                old_widget = matcher.dataurl_to_bgr(target["image"])
            except Exception:
                pass
        semantic = ""
        if target:
            semantic = (target.get("text") or target.get("semantic") or "").strip()
        if not semantic:
            return {"ok": False, "updated": False,
                    "note": "页面失配且无部件词/语义，需人工"}
        # 页面未锁定：窗口矩形提供页面原点（真窗/注入场景）——先取窗口再调语义确认
        wrect = self.window_rect() if callable(self.window_rect) else self.window_rect
        if wrect is None:
            return {"ok": False, "updated": False,
                    "note": "页面失配恢复需要窗口矩形上下文（window_rect 未提供）"}
        x0, y0, ww, wh = [int(v) for v in wrect]
        page_img = screen[y0:y0 + wh, x0:x0 + ww]
        if page_img.size == 0:
            return {"ok": False, "updated": False, "note": "窗口矩形越出屏幕"}
        page_rect = (x0, y0, ww, wh)
        # 语义确认输入=窗口区域图（整屏过大/带其他窗口会干扰行带扫描与上传成本）
        hint = None
        rx = target.get("rect_in_page") if target else None
        if rx:
            hint = (rx[0] + rx[2] // 2, rx[1] + rx[3] // 2)
        conf = self._ai_coarse(old_widget, page_img, semantic, hint_xy=hint)
        if not conf or not conf.get("ok") or not conf.get("xy"):
            return {"ok": False, "updated": False,
                    "note": f"语义确认未通过（{(conf or {}).get('note', 'AI 不可用')}）→ 人工兜底"}
        # 圈区内找 widget（页面内坐标；extra=AI 确认词 → 改名恢复候选）
        candidates = [tuple(conf["xy"]), (ww // 2, wh // 2)]
        found = self._localize(target, page_img, candidates,
                               extra_needles=[conf.get("matched")])
        if not found["ok"]:
            return {"ok": False, "updated": False,
                    "note": found.get("note", "页面内未找到部件 → 人工兜底"),
                    "detail": {"ai": conf.get("note")}}
        box, matched = found["box"], found.get("matched")
        spec = page_spec if page_spec is not None else (target or {}).get("page")
        # 部件全量写回
        self._rewrite_widget(target, page_img, box, matched)
        # 动态页锚采集：主锚 + 分散补充锚，跨帧复验 → page.anchors 写回（供整窗持续失配场景）
        new_anchors = self._collect_anchors(page_img, box, page_rect, meta)
        anchor = new_anchors[0] if new_anchors else None
        # 整窗重采集：**只在页面确实静态时**才把当前帧写成页面模板。
        # 动态页上写它有害无益——下一轮又是新画面 → 又失配 → 又校准（实测：feed 案例每轮
        # 2 次定位、锚一次都没用上，真机上每次校准都要打断用户，这正是 M0 "强动态页只有 3%"
        # 的机制）。动态页该做的是留下锚，让锚定位接管。
        old_image, old_rect = None, None
        kept_old = False
        if spec is not None:
            old_image, old_rect = spec.get("image"), spec.get("rect_in_screen")
            self._rewrite_page(spec, page_img, wrect, meta)
            if anchor and not self._page_stable(wrect):
                if old_image:
                    spec["image"] = old_image
                    spec["rect_in_screen"] = old_rect
                kept_old = True
        added = 0
        if new_anchors and spec is not None:
            anchors = spec.setdefault("anchors", [])
            for a in new_anchors:
                if any(_overlap_ratio(a["rect_in_page"], ex.get("rect_in_page") or [0, 0, 0, 0])
                       >= 0.8 for ex in anchors):
                    continue                      # 同一个地方的老锚不重复写
                anchors.append({k: v for k, v in a.items() if k != "_stable"})
                added += 1
            spec["capture_meta"]["calib"] = ("page_recapture+anchor"
                                            + ("(dynamic)" if kept_old else ""))
        note_anchor = ""
        if anchor:
            note_anchor = "；动态内容已写回静态锚 %d 个（最稳 %.2f）" % (
                added or len(new_anchors), anchor["_stable"])
        detail_anchor = [{k: v for k, v in a.items() if k != "image"}
                         for a in new_anchors[:ANCHOR_MAX]]
        ok = self._selfcheck(target, screen, page_rect)
        if ok:
            note = "页面已重采集并恢复部件" + note_anchor
            return {"ok": True, "updated": True, "page_spec": spec, "note": note,
                    "detail": {"local": found, "ai": conf.get("note"),
                               "anchors": detail_anchor}}
        if anchor and spec is not None:
            # 整窗自检失败（动态页常态）但锚已复验稳定 → 按锚写回判定成功
            return {"ok": True, "updated": True, "page_spec": spec,
                    "note": "整窗自检未过（动态内容），已写回静态锚" + note_anchor,
                    "detail": {"local": found, "ai": conf.get("note"),
                               "anchors": detail_anchor}}
        return {"ok": False, "updated": False,
                "note": "重采集后自检失败且无稳定锚（旧值保留 → 人工兜底）",
                "detail": {"local": found, "ai": conf.get("note")}}

    def _page_stable(self, wrect, dt=2.2, thr=0.90) -> bool:
        """整窗在 dt 秒内是否稳定——决定"该不该把当前帧写成页面模板"。

        动态页整窗一直变：留下新模板会让下一轮又失配、又校准（实测每轮都校准一次，锚永远
        用不上）。判断不了时返回 True（保守：保持原有行为，不乱动用户数据）。

        dt 必须**大于页面的重排周期**：动态 fixture 每 1.2s 重排一次，dt=1.0 时两次抓屏常常
        落在同一次重排之内，于是"一直在变"被误判成"稳定"（实测踩到，白改一版）。
        """
        import engine.capture as _cap
        try:
            a, _ = self._grab()
            time.sleep(max(0.0, dt))
            b, _ = self._grab()
        except Exception:
            return True
        x, y, w, h = [int(v) for v in wrect]
        pa, pb = a[y:y + h, x:x + w], b[y:y + h, x:x + w]
        if getattr(pa, "size", 0) == 0 or pa.shape != pb.shape:
            return True
        return _cap.static_score(pa, pb) >= thr

    def _anchor_bands(self, page_img, box):
        """候选锚条带（纯几何 + 纹理过滤，可离线单测）。

        主锚＝部件行带（沿用 M1 几何，与目标最相关）；补充锚＝页面上按 3 行 × 3 列铺开的
        窄横条带，用"与已选锚垂直距离最远"贪心挑选，把锚摊到顶/中/底（分散的锚才能互相
        印证；挤在一起的锚等于只有一个锚）。
        与已选锚重叠过多、或纹理过低的候选直接丢掉。
        返回 [rect...]（最多 ANCHOR_MAX 个）。
        """
        ph, pw = page_img.shape[:2]
        bx, by, bw, bh = [int(v) for v in box]
        mx0, my0 = max(0, bx - ANCHOR_PAD_X), max(0, by - ANCHOR_PAD_Y)
        mx1, my1 = min(pw, bx + bw + ANCHOR_PAD_X), min(ph, by + bh + ANCHOR_PAD_Y)
        raw = [(mx0, my0, mx1 - mx0, my1 - my0)]
        band_h = int(max(ANCHOR_BAND_MIN[1], min(ANCHOR_BAND_MAX_H, ph // 16)))
        band_w = int(pw / 3 * 0.94)
        for row in (0, 1, 2):                      # 顶 / 中 / 底
            y = int((ph - band_h) * row / 2)
            for col in (0, 1, 2):                  # 左 / 中 / 右
                x = int((pw - band_w) * col / 2)
                raw.append((x, y, band_w, band_h))
        cands = [r for r in raw if r[2] >= ANCHOR_BAND_MIN[0] and r[3] >= ANCHOR_BAND_MIN[1]]
        cands = [r for r in cands
                 if matcher.gray_std(page_img[r[1]:r[1] + r[3], r[0]:r[0] + r[2]])
                 >= ANCHOR_MIN_STD]
        if not cands:
            return []
        picked = [cands[0]]                        # 主锚先入选
        rest = cands[1:]
        while rest and len(picked) < ANCHOR_MAX:
            rest = [r for r in rest
                    if max(_overlap_ratio(r, p) for p in picked) <= ANCHOR_OVERLAP_MAX]
            if not rest:
                break

            def _vgap(r, _picked=picked):
                cy = r[1] + r[3] / 2
                return min(abs(cy - (p[1] + p[3] / 2)) for p in _picked)

            rest.sort(key=_vgap, reverse=True)
            picked.append(rest.pop(0))
        return picked

    def _collect_anchors(self, page_img, box, page_rect, meta):
        """多锚采集（M2-WP2）：主锚 + 分散补充锚，**一次抓帧**复验全部候选。

        与 M1 单锚版的区别：① 候选是多个分散条带；② 只抓一次第二帧（原实现每个锚各抓一次，
        锚越多越慢）；③ 跨帧复验不过（动态区）/内容重复的候选丢掉。
        返回 anchor list（0~ANCHOR_MAX 个，含临时 _stable 字段供 note）。
        """
        import engine.capture as _cap
        bands = self._anchor_bands(page_img, box)
        if not bands:
            return []
        px0, py0 = int(page_rect[0]), int(page_rect[1])
        try:
            time.sleep(max(0.0, self.anchor_dt_s))
            screen2, _ = self._grab()
        except Exception:
            return []
        out = []
        for rect in bands:
            x, y, w, h = [int(v) for v in rect]
            frame_a = page_img[y:y + h, x:x + w]
            frame_b = screen2[py0 + y:py0 + y + h, px0 + x:px0 + x + w]
            if frame_b.size == 0 or frame_a.shape != frame_b.shape:
                continue
            score = _cap.static_score(frame_a, frame_b)
            if score < ANCHOR_STABLE_MIN:
                continue
            dup = False
            for q in out:                          # 与已选锚内容几乎相同 → 不提供独立证据
                qx, qy, qw, qh = [int(v) for v in q["rect_in_page"]]
                if matcher.pixel_sim(frame_a, page_img[qy:qy + qh, qx:qx + qw],
                                     size=(240, 140), thr=12.0) >= ANCHOR_DUP_SIM:
                    dup = True
                    break
            if dup:
                continue
            out.append({"image": matcher.bgr_to_dataurl(frame_a),
                        "rect_in_page": list(rect), "stable_at": _now_iso(),
                        "_stable": round(score, 4)})
            if len(out) >= ANCHOR_MAX:
                break
        return out

    # ---------------------------------------------------------- 子步骤

    def _grab(self):
        return self.driver.grab_screen()

    def _ai_rename(self, old_widget, screen_bgr, semantic, options=None):
        """第二问：目标现在叫什么？异常/桩不支持 → 不阻断（照旧走人工兜底）。"""
        ask = getattr(self.ai, "ask_rename", None)
        if ask is None:
            return None
        try:
            return ask(old_widget, screen_bgr, semantic, options=options)
        except TypeError:                    # 旧式实现没有 options 参数
            try:
                return ask(old_widget, screen_bgr, semantic)
            except Exception as e:
                return {"ok": False, "note": f"AI 调用异常 {e!r}"}
        except EngineError:
            return {"ok": False, "note": "AI 未授权/未配置"}
        except Exception as e:
            return {"ok": False, "note": f"AI 调用异常 {e!r}"}

    def _ai_coarse(self, old_widget, screen_bgr, semantic, hint_xy=None):
        """AI 双图语义确认（异常/不可用 → 不阻断，转本地录点邻域）。"""
        try:
            return self.ai.confirm_target(old_widget, screen_bgr, semantic,
                                          hint_xy=hint_xy)
        except TypeError:
            try:                                   # 旧式 AI 无 hint 参数
                return self.ai.confirm_target(old_widget, screen_bgr, semantic)
            except Exception as e:
                return {"ok": False, "xy": None, "note": f"AI 调用异常 {e!r}"}
        except EngineError:
            return {"ok": False, "xy": None, "note": "AI 未授权/未配置"}
        except Exception as e:
            return {"ok": False, "xy": None, "note": f"AI 调用异常 {e!r}"}

    def _localize(self, target, page_img, candidates, extra_needles=None):
        """
        圈区本地精定位：每个圈心区域 OCR 一次，tokens 与候选词（旧词/前缀/AI 确认词）
        一次匹配；都 miss 再试模板。返回 {ok, box(相对 page_img 左上), method, matched?, note}
        """
        text = (target.get("text") or "").strip()
        needles = []
        if text:
            needles.append(text)
            short = matcher.text_needle_short(text, 2)
            if len(short) >= 2 and short != text:
                needles.append(short)
        for n in (extra_needles or []):
            n = (n or "").strip()
            if n and n not in needles:
                needles.append(n)
        tpl = None
        if target.get("image"):
            try:
                tpl = matcher.dataurl_to_bgr(target["image"])
            except Exception:
                tpl = None
        ph, pw = page_img.shape[:2]
        seen = []                            # OCR 到过的词（给"现在叫什么"当候选选项）
        for (cx, cy) in candidates:
            # 文字路径：单区域 OCR + tokens 匹配（一次调用服务全部候选词）
            hw, hh = TEXT_RADIUS
            lx0, ly0 = max(0, int(cx - hw)), max(0, int(cy - hh))
            sx = min(pw - lx0, hw * 2)
            sy = min(ph - ly0, hh * 2)
            if sx >= 60 and sy >= 40 and needles:
                strip = page_img[ly0:ly0 + sy, lx0:lx0 + sx]
                r = matcher.ocr_run(strip)
                if not r.get("error") and not r["boxes"]:
                    r2 = matcher.ocr_run(strip)
                    if r2.get("error") or r2["boxes"]:
                        r = r2
                if not r.get("error"):
                    best = None
                    for bx, txt, _sc in zip(r["boxes"], r["txts"], r["scores"]):
                        t = str(txt).strip()
                        if t and t not in seen:
                            seen.append(t)
                        for needle in needles:
                            sim = matcher.text_similar(txt, needle)
                            if sim >= 0.75 and (best is None or sim > best[0]):
                                best = (sim, bx, txt)
                    if best:
                        bx = best[1]
                        return {"ok": True,
                                "box": (lx0 + bx[0], ly0 + bx[1], bx[2], bx[3]),
                                "method": "ocr_text",
                                "matched": best[2],
                                "seen": seen,
                                "note": f"文字命中 {best[2]!r}"}
            # 模板路径（布局微移/视觉变化但形状仍在）
            if tpl is not None:
                tw, th = tpl.shape[1], tpl.shape[0]
                hw = max(TPL_RADIUS_MUL * tw // 2, 160)
                hh = max(TPL_RADIUS_MUL * th // 2, 90)
                lx0, ly0 = max(0, int(cx - hw)), max(0, int(cy - hh))
                sx = min(pw - lx0, hw * 2)
                sy = min(ph - ly0, hh * 2)
                if sx >= tw and sy >= th:
                    m = matcher.find_template(page_img, tpl, scales=(0.9, 1.0, 1.1),
                                              score_thr=0.6,
                                              search=(lx0, ly0, sx, sy))
                    if m["ok"]:
                        return {"ok": True, "box": m["rect"], "method": "tpl",
                                "matched": None,
                                "note": f"模板命中（{m['score']:.2f}）"}
        return {"ok": False, "box": None, "method": None, "seen": seen,
                "note": "圈区内未找到旧词/前缀/模板（布局大改或文字改名）"}

    def _rewrite_widget(self, target, page_img, box, matched):
        """部件全量写回（image/rect_in_page/center/text；semantic 保留）。"""
        x, y, w, h = [int(v) for v in box]
        x0, y0 = max(0, x - PAD), max(0, y - PAD)
        x1 = min(page_img.shape[1], x + w + PAD)
        y1 = min(page_img.shape[0], y + h + PAD)
        crop = page_img[y0:y1, x0:x1]
        if crop.size == 0:
            return
        target["image"] = matcher.bgr_to_dataurl(crop)
        target["rect_in_page"] = [x0, y0, x1 - x0, y1 - y0]
        target["center_in_page"] = [(x0 + x1) // 2, (y0 + y1) // 2]
        if matched and target.get("text") != matched:
            target["text"] = matched            # 改名恢复：写回新文字
        target.pop("_calib", None)

    def _touch_page_meta(self, spec, page_rect, meta):
        """部件级恢复不动整窗模板；仅刷新采集元数据（同一页模板仍有效）。"""
        cm = spec.setdefault("capture_meta", {})
        cm["ts"] = _now_iso()
        cm["calib"] = "widget_refresh"

    def _rewrite_page(self, spec, page_img, wrect, meta):
        """页面全量写回：image/size/rect_in_screen/ts（context/scale_range/anchors 保留）。"""
        spec["image"] = matcher.bgr_to_dataurl(page_img)
        h, w = page_img.shape[:2]
        spec["size"] = [w, h]
        spec["rect_in_screen"] = [int(v) for v in wrect]
        cm = spec.setdefault("capture_meta", {})
        cm["ts"] = _now_iso()
        cm["calib"] = "page_recapture"
        cm.setdefault("dpi", meta.get("dpi") or 0)

    def _selfcheck(self, target, screen, page_rect):
        """写回前自检：复用正常路径在当前屏试定位（页面+部件）。"""
        spec = target.get("page")
        try:
            if spec is not None:
                r = locator.locate_page(screen, spec)
                if not r["ok"]:
                    return False
                page_rect = r["rect"]
            if page_rect is None:
                return False
            w = locator.locate_widget_on_screen(screen, page_rect, target, exists=False)
            return w["ok"]
        except Exception:
            return False


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def _overlap_ratio(a, b) -> float:
    """两个 (x,y,w,h) 的交集面积 ÷ **较小者**面积（0~1）—— 判断"两个锚是不是同一块地方"。"""
    ax, ay, aw, ah = [int(v) for v in a]
    bx, by, bw, bh = [int(v) for v in b]
    iw = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0, min(ay + ah, by + bh) - max(ay, by))
    return (iw * ih) / float(max(1, min(aw * ah, bw * bh)))
