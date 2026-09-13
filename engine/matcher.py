# -*- coding: utf-8 -*-
"""
engine.matcher — E3：本地图像匹配与 OCR 原语（纯离线，零 AI）。

内容：
  - 图像编解码：BGR ndarray ↔ PNG data-url（.sgscript.json 内嵌用，§11/清单§4）
  - 多尺度模板匹配 find_template（0.8~1.25×，大模板金字塔；位置无关）
  - 像素同源 pixel_sim / 文本相似 text_similar（含“更长串包含短词”方向规则）
  - RapidOCR 文本条带 find_text_ocr / ocr_run

OCR 性能注（波次1 实测，2026-09-09，本机 i9-13900H）：
  rapidocr 3.9.2 默认 Det.limit_type=min + 层级 limit_side_len(736/960/1500/2000)，
  会把 300×90 小条带放大到 736×2464（单次 0.9~2s+）。引擎初始化强制
  Det.limit_type=max + limit_side_len=960：条带保持原分辨率，实测 300×90 找中文词
  稳态 ~90-140ms、真实 ERP 菜单条带 ~80ms、整页 1288×988 ~10s（页级 OCR 属
  “条带未覆盖”兜底，本引擎默认不做全页 OCR，见 locator.exists 语义）。
   线程参数：intra_op_num_threads=4 / inter=1（波次1 实测定案：默认(-1)与 1 会在
   ~15 次连续推理后漂移到 0.4~4.9s/次；4 线程连续 20+ 次稳定 37–51ms/条带）。

登记的性能风险（M1 中段复查项，2026-09-09）：
   1) ORT 推理后紧邻的 cv2 匹配偶发短暂变慢（数百 ms 级，数拍后恢复，进程级
      时序相关）——引擎已固定 cv2 单线程 + 大模板金字塔粗选精修（稳定措施）；
   2) 页面/部件定位耗时预算以实测中位数计，验收口径见 docs/M1_进展记录.md §2。
"""
from __future__ import annotations

import base64
import difflib
import math
import threading
import time
from pathlib import Path

import cv2
import numpy as np

# 多尺度范围（v0.30 定案 0.8~1.25×，页面与部件一致）
DEFAULT_PAGE_SCALES = (0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25)
DEFAULT_TPL_SCALES = (1.00, 1.05, 0.95)
DATAURL_PREFIX = "data:image/png;base64,"


# ---------------------------------------------------------------- 图像编解码

def bgr_to_dataurl(bgr) -> str:
    """BGR ndarray → PNG data-url（§11 内嵌格式）。"""
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("PNG 编码失败")
    return DATAURL_PREFIX + base64.b64encode(buf.tobytes()).decode("ascii")


def dataurl_to_bgr(dataurl: str) -> np.ndarray:
    """PNG data-url → BGR ndarray。"""
    if not dataurl.startswith(DATAURL_PREFIX):
        raise ValueError("非 PNG data-url")
    raw = base64.b64decode(dataurl[len(DATAURL_PREFIX):])
    arr = np.frombuffer(raw, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("data-url 图像解码失败")
    return bgr


def load_png(path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return img


def save_png(path, bgr) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), bgr)


# ---------------------------------------------------------------- 相似度

def pixel_sim(a, b, size=(144, 90), thr=48.0) -> float:
    """两图“同源度”≈1−差异像素占比（对动态内容/白底 UI 稳健，替代互相关）。"""
    ta = cv2.resize(a, size)
    tb = cv2.resize(b, size)
    if ta.ndim == 3:
        ta = cv2.cvtColor(ta, cv2.COLOR_BGR2GRAY)
        tb = cv2.cvtColor(tb, cv2.COLOR_BGR2GRAY)
    diff = np.abs(ta.astype(np.int16) - tb.astype(np.int16))
    return float(1.0 - (diff > thr).mean())


def text_similar(a: str, b: str, thr=0.75) -> float:
    """a、b 文本相似度。调用约定：find_text_ocr 传 (OCR_token, needle)。

    子串规则只允许“更长的一串包含更短的词”（OCR 把整行连读时，长 token 含目标词）；
    反向（短 token 只是长目标的片段，如 token '查询' ⊂ needle '库存查询'）不算命中，防误配。
    （M0 实测修正，2026-09-09，回归在 engine/tests）
    """
    a = "".join(str(a).split()).lower()
    b = "".join(str(b).split()).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= len(b):
        return 0.98 if b in a else difflib.SequenceMatcher(None, a, b).ratio()
    return difflib.SequenceMatcher(None, a, b).ratio()


def text_needle_short(text: str, n=4) -> str:
    """长目标文字取前 n 个字符作为短探针（避免 OCR 行分段导致整句匹配失败）。"""
    t = "".join(str(text).split())
    return t[:n] if len(t) > n else t


# ---------------------------------------------------------------- 模板匹配

# 波次1 实测（2026-09-09，i9-13900H）：ORT 推理一次后 cv2 并行 matchTemplate
# 偶发/持续 20× 恶化（250ms → 2~4s，复现 25/25 次；单线程稳定 ~26ms）。
# 定案：引擎内固定 cv2 单线程 + 大模板“金字塔粗选 top-2 → 全分辨率局部精修”，
# 耗时确定性且预算内（页面定位 ~120–250ms）。根因待 M2 跟踪（疑似 cv2-5.0 并行
# 后端与 ORT 线程相互作用；届时先复测 opencv 4.10 对照）。
cv2.setNumThreads(1)

# cv2 计算挂死保护（2026-09-09 实测：本机 cv2 4.13/5.0 的 matchTemplate 存在
# 概率性冻结 >400s，与 ORT det 挂死同窗出现——疑似底层并行运行时（OpenMP 类）
# 在本机的大小核调度病态；numpy 全程正常。对策：全部匹配计算放入守护线程 +
# 超时熔断，挂死时该路径失败并自动退化（模板→文字→坐标），进程不受拖累。）
_MATCH_TIMEOUT_S = 10.0
_MATCH_MAX_HANGS = 3
_MATCH_BREAK_S = 30.0
_MATCH_HANGS = 0
_MATCH_BREAK_UNTIL = 0.0
_MATCH_LOCK = threading.Lock()


def cv_reset() -> None:
    """重置模板匹配熔断状态（诊断/自愈入口）。"""
    global _MATCH_HANGS, _MATCH_BREAK_UNTIL
    with _MATCH_LOCK:
        _MATCH_HANGS = 0
        _MATCH_BREAK_UNTIL = 0.0


def _match_once(sc_img, sc_tpl, sc):
    res = cv2.matchTemplate(sc_img, sc_tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, mxl = cv2.minMaxLoc(res)
    return float(mx), int(mxl[0]), int(mxl[1])


def _fail_result(elapsed, scale=1.0, error="hang"):
    return {"ok": False, "score": -1.0, "rect": None, "center": None,
            "scale": scale, "elapsed_ms": elapsed, "best_score": -1.0, "error": error}


def find_template(screen_bgr, tpl_bgr, scales=None, score_thr=0.70,
                  search=None, coarse_px=60_000, timeout_s=None) -> dict:
    """
    在 screen 中找 tpl，多尺度取最高分。
    - scales: 默认 DEFAULT_PAGE_SCALES（0.8~1.25×）
    - search: 可选 (x, y, w, h) 屏幕内搜索区域（页面锁定后部件只在页内搜，防越界）
    返回 {ok, score, rect(x,y,w,h 屏幕坐标), center, scale, elapsed_ms, best_score}
    大模板（>coarse_px 像素）金字塔策略：降采样对全部尺度粗定位 → 粗分 top-2 尺度
    在原图局部窗口精修取最优（单线程确定性成本，见模块注）。
    挂死保护：计算在守护线程执行，超时（默认 10s）→ 失败返回 + 计数；
    连续 ≥3 次 → 冷却 30s（期间直接失败，退化见 locator ②/③），冷却结束自动
    恢复（cv_reset() 亦可手动重置）。
    """
    global _MATCH_HANGS, _MATCH_BREAK_UNTIL
    now = time.perf_counter()
    with _MATCH_LOCK:
        if _MATCH_HANGS >= _MATCH_MAX_HANGS:
            if now < _MATCH_BREAK_UNTIL:
                return _fail_result(0.0, error="breaker_open")
            _MATCH_HANGS = 0
    t0 = time.perf_counter()
    holder = {}

    def _work():
        holder["r"] = _find_template_impl(screen_bgr, tpl_bgr, scales, score_thr,
                                          search, coarse_px)

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout_s or _MATCH_TIMEOUT_S)
    if th.is_alive():
        with _MATCH_LOCK:
            _MATCH_HANGS += 1
            if _MATCH_HANGS >= _MATCH_MAX_HANGS:
                _MATCH_BREAK_UNTIL = time.perf_counter() + _MATCH_BREAK_S
        return _fail_result((time.perf_counter() - t0) * 1000, error="timeout")
    return holder.get("r") or _fail_result((time.perf_counter() - t0) * 1000)


# 环带匹配的相似度尺度：每像素每通道 RMSE 达到这个灰度级差 → 判为"完全不像"
RMSE_FULL = 64.0


# ---------------------------------------------------------------- 局部特征兜底（M2-WP2 第二步）
# 只在整窗模板（含多尺度）与静态锚**都**失败时使用：ORB 特征 + RANSAC 单应。
# 好处是对旋转/缩放/局部遮挡都不敏感（模板匹配做不到）；代价是计算量更大，所以放最后。
FEATURE_MIN_INLIERS = 12      # 内点数下限：低于这个数不敢用（宁可失败也不误点）
FEATURE_MIN_RATIO = 0.25      # 内点占匹配数的比例下限（低于此值说明是零散巧合匹配）
FEATURE_RANSAC_PX = 5.0       # RANSAC 重投影阈值（像素）


def find_page_by_features(screen_bgr, tpl_bgr, min_inliers=FEATURE_MIN_INLIERS,
                          min_ratio=FEATURE_MIN_RATIO, n_features=1500) -> dict:
    """局部特征兜底：ORB + RANSAC 单应 → 页面屏幕矩形。

    为什么要有它（M0 遗留 bili feed 只有 3%）：强动态页里整窗模板必然失配、静态锚也可能
    不在（页面被大幅重排/滚动/整体换布局），那时只剩"页面内坐标"这种纯几何兜底。
    局部特征能从"部分还对得上的纹理"里把页面位置恢复出来，且对旋转/缩放/局部遮挡不敏感。

    返回 {ok, rect, scale, inliers, ratio, matches, n_tpl, n_screen, elapsed_ms, reason?}。
    口径仍然是"宁可失败也不误点"：内点数或内点比例不达标就 ok=False，绝不硬给一个矩形。
    """
    t0 = time.perf_counter()

    def _fail(reason, **kw):
        out = {"ok": False, "reason": reason, "elapsed_ms": (time.perf_counter() - t0) * 1000}
        out.update(kw)
        return out

    if screen_bgr is None or tpl_bgr is None:
        return _fail("no_image")
    g1 = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY) if tpl_bgr.ndim == 3 else tpl_bgr
    g2 = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY) if screen_bgr.ndim == 3 else screen_bgr
    if g1.size == 0 or g2.size == 0:
        return _fail("empty_image")
    orb = cv2.ORB_create(nfeatures=int(n_features), fastThreshold=12)
    k1, d1 = orb.detectAndCompute(g1, None)
    k2, d2 = orb.detectAndCompute(g2, None)
    n1, n2 = len(k1 or []), len(k2 or [])
    if d1 is None or d2 is None or n1 < min_inliers or n2 < min_inliers:
        return _fail("too_few_keypoints", n_tpl=n1, n_screen=n2)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(d1, d2)
    if len(matches) < min_inliers:
        return _fail("too_few_matches", matches=len(matches), n_tpl=n1, n_screen=n2)
    src = np.float32([k1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, FEATURE_RANSAC_PX)
    if H is None:
        return _fail("homography_failed", matches=len(matches), n_tpl=n1, n_screen=n2)
    inliers = int(mask.sum()) if mask is not None else 0
    ratio = inliers / max(1, len(matches))
    if inliers < min_inliers or ratio < min_ratio:
        return _fail(f"weak_homography(inliers={inliers},ratio={ratio:.2f})",
                     inliers=inliers, ratio=round(ratio, 3), matches=len(matches),
                     n_tpl=n1, n_screen=n2)
    h, w = g1.shape[:2]
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    proj = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
    x0, y0 = proj.min(axis=0)
    x1, y1 = proj.max(axis=0)
    rw, rh = max(1.0, float(x1 - x0)), max(1.0, float(y1 - y0))
    if rw > screen_bgr.shape[1] * 2 or rh > screen_bgr.shape[0] * 2:
        return _fail("rect_too_large", inliers=inliers, ratio=round(ratio, 3))
    return {"ok": True,
            "rect": (int(round(x0)), int(round(y0)), int(round(rw)), int(round(rh))),
            "scale": round(((rw / w) + (rh / h)) / 2.0, 4),
            "inliers": inliers, "ratio": round(ratio, 3), "matches": len(matches),
            "n_tpl": n1, "n_screen": n2,
            "elapsed_ms": (time.perf_counter() - t0) * 1000}


def gray_std(bgr) -> float:
    """灰度标准差：低纹理判据（大片纯色/空白页面 CCOEFF 会给高分假阳性）。"""
    if bgr is None or getattr(bgr, "size", 0) == 0:
        return 0.0
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    return float(g.std())


def ring_mask(shape, ring_px=None):
    """构造"外圈环带"掩码：只在模板**四边**参与打分，中心内容区置 0。

    用于"认边框不认内容"：输入框内侧的占位提示/已填数据都会变，但边框与底色不变。
    ring_px 为空时按模板尺寸自适应（短边的 ~22%，夹在 3~10px）。
    """
    th, tw = int(shape[0]), int(shape[1])
    if ring_px is None:
        ring_px = int(round(min(th, tw) * 0.22))
        ring_px = max(3, min(10, ring_px))
    r = max(1, min(int(ring_px), max(1, th // 2), max(1, tw // 2)))
    mask = np.zeros((th, tw), np.uint8)
    mask[:r, :] = 255          # 上边
    mask[th - r:, :] = 255     # 下边
    mask[:, :r] = 255          # 左边
    mask[:, tw - r:] = 255     # 右边
    return mask, r


def find_template_ring(screen_bgr, tpl_bgr, ring_px=None, score_thr=0.55, search=None,
                       scale=1.0, timeout_s=None) -> dict:
    """带掩码的"环带模板"匹配：只用模板四边（边框/底色）找目标，中心内容不参与。

    场景（真人反馈 2026-09-10）：录制时输入框是空的（内侧是灰色占位提示），跑过一次后
    框里被填入数据 —— 占位文字没了、整块模板也对不上，于是"找不到目标"。
    边框与"里面填了什么"无关，所以用环带定位更稳。

    实现要点：cv2 只有 TM_SQDIFF / TM_CCORR_NORMED 支持 mask，这里用 TM_SQDIFF
    （绝对差平方和，越小越好）再换算成 0~1 相似度：sim = 1 - sse / (n * 255²)。
    同样带挂死保护（守护线程 + 超时）。
    """
    global _MATCH_HANGS, _MATCH_BREAK_UNTIL
    now = time.perf_counter()
    with _MATCH_LOCK:
        if _MATCH_HANGS >= _MATCH_MAX_HANGS:
            if now < _MATCH_BREAK_UNTIL:
                return _fail_result(0.0, error="breaker_open")
            _MATCH_HANGS = 0
    t0 = time.perf_counter()
    holder = {}

    def _work():
        holder["r"] = _find_ring_impl(screen_bgr, tpl_bgr, ring_px, score_thr, search, scale)

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout_s or _MATCH_TIMEOUT_S)
    if th.is_alive():
        with _MATCH_LOCK:
            _MATCH_HANGS += 1
            if _MATCH_HANGS >= _MATCH_MAX_HANGS:
                _MATCH_BREAK_UNTIL = time.perf_counter() + _MATCH_BREAK_S
        return _fail_result((time.perf_counter() - t0) * 1000, error="timeout")
    return holder.get("r") or _fail_result((time.perf_counter() - t0) * 1000)


def _find_ring_impl(screen_bgr, tpl_bgr, ring_px, score_thr, search, scale=1.0) -> dict:
    t0 = time.perf_counter()
    if scale and abs(scale - 1.0) > 1e-3:      # 页面有缩放时按同一比例缩放模板
        th0, tw0 = tpl_bgr.shape[:2]
        w = max(6, int(round(tw0 * scale)))
        h = max(6, int(round(th0 * scale)))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        tpl_bgr = cv2.resize(tpl_bgr, (w, h), interpolation=interp)
    off_x = off_y = 0
    if search is not None:
        sx, sy, sw, sh = [int(v) for v in search]
        h, w = screen_bgr.shape[:2]
        sx = max(0, sx); sy = max(0, sy)
        sw = min(sw, w - sx); sh = min(sh, h - sy)
        if sw < 8 or sh < 8:
            return _fail_result((time.perf_counter() - t0) * 1000, error="search_too_small")
        screen_bgr = screen_bgr[sy:sy + sh, sx:sx + sw]
        off_x, off_y = sx, sy
    t_h, t_w = tpl_bgr.shape[:2]
    s_h, s_w = screen_bgr.shape[:2]
    if t_h < 6 or t_w < 6 or t_h > s_h or t_w > s_w:
        return _fail_result((time.perf_counter() - t0) * 1000, error="tpl_size")
    mask, ring = ring_mask((t_h, t_w), ring_px)
    n = int((mask > 0).sum())
    if n < 16:
        return _fail_result((time.perf_counter() - t0) * 1000, error="ring_too_thin")
    res = cv2.matchTemplate(screen_bgr, tpl_bgr, cv2.TM_SQDIFF, mask=mask)
    min_val, _, min_loc, _ = cv2.minMaxLoc(res)
    # 把"平方差之和"换算成 0~1 相似度：用**每像素每通道的均方根误差**，
    # 以 RMSE_FULL（默认 64 灰度级）作为"完全不像"的尺度。
    # 之前直接除以 255² 太宽松：整片底色差 46 级仍能算出 0.90，阈值形同虚设。
    channels = screen_bgr.shape[2] if screen_bgr.ndim == 3 else 1
    mse = float(min_val) / max(1, n * channels)
    rmse = math.sqrt(max(0.0, mse))
    sim = max(0.0, min(1.0, 1.0 - rmse / RMSE_FULL))
    x, y = int(min_loc[0]) + off_x, int(min_loc[1]) + off_y
    return {"ok": sim >= score_thr, "score": round(sim, 4), "rect": (x, y, t_w, t_h),
            "center": (x + t_w // 2, y + t_h // 2), "scale": 1.0, "ring_px": ring,
            "rmse": round(rmse, 2),
            "elapsed_ms": (time.perf_counter() - t0) * 1000, "best_score": round(sim, 4)}


def _find_template_impl(screen_bgr, tpl_bgr, scales=None, score_thr=0.70,
                        search=None, coarse_px=60_000) -> dict:
    t0 = time.perf_counter()
    s_h, s_w = screen_bgr.shape[:2]
    t_h, t_w = tpl_bgr.shape[:2]
    off_x = off_y = 0
    if search is not None:
        sx, sy, sw, sh = search
        sx = max(0, int(sx)); sy = max(0, int(sy))
        sw = min(int(sw), s_w - sx); sh = min(int(sh), s_h - sy)
        if sw < 8 or sh < 8:
            return {"ok": False, "score": -1.0, "rect": None, "center": None,
                    "elapsed_ms": (time.perf_counter() - t0) * 1000, "scale": 1.0}
        screen_bgr = screen_bgr[sy:sy + sh, sx:sx + sw]
        off_x, off_y = sx, sy
    s_h, s_w = screen_bgr.shape[:2]
    if scales is None:
        scales = DEFAULT_PAGE_SCALES

    big = t_h * t_w > coarse_px
    cands = []   # (score, scale, tw, th, x, y) 粗定位结果
    for sc in scales:
        th, tw = int(round(t_h * sc)), int(round(t_w * sc))
        if th < 8 or tw < 8 or th > s_h or tw > s_w:
            continue
        tpl = cv2.resize(tpl_bgr, (tw, th), interpolation=cv2.INTER_AREA) if sc != 1.0 else tpl_bgr
        if not big:
            mx, bx, by = _match_once(screen_bgr, tpl, sc)
            cands.append((mx, sc, tw, th, bx, by))
            continue
        ds = max(2, int((th * tw / coarse_px) ** 0.5))
        small = cv2.resize(screen_bgr, (s_w // ds, s_h // ds), interpolation=cv2.INTER_AREA)
        stpl = cv2.resize(tpl, (max(4, tw // ds), max(4, th // ds)), interpolation=cv2.INTER_AREA)
        mx, bx, by = _match_once(small, stpl, sc)
        cands.append((mx, sc, tw, th, bx * ds, by * ds))

    if not cands:
        return {"ok": False, "score": -1.0, "rect": None, "center": None,
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "scale": 1.0}

    def _refine(entry):
        mx, sc, tw, th, cx, cy = entry
        if big:
            m = 12
            x0, y0 = max(0, cx - m), max(0, cy - m)
            x1 = min(s_w, cx + tw + m)
            y1 = min(s_h, cy + th + m)
            if x1 - x0 >= tw and y1 - y0 >= th:
                region = screen_bgr[y0:y1, x0:x1]
                rmx, lx, ly = _match_once(region, cv2.resize(tpl_bgr, (tw, th),
                                                             interpolation=cv2.INTER_AREA), sc)
                if rmx > mx:
                    return (rmx, x0 + lx, y0 + ly)
        return (mx, cx, cy)

    # 粗分 top-2 精修；小模板直配即最高分，直接取最优（免无谓精修）
    if big:
        cands.sort(key=lambda c: c[0], reverse=True)
        for i, entry in enumerate(cands[:2]):
            mx, x, y = _refine(entry)
            cands[i] = (mx, entry[1], entry[2], entry[3], x, y)
    best = max(cands, key=lambda c: c[0])
    mx, sc, tw, th, bx, by = best
    elapsed = (time.perf_counter() - t0) * 1000
    if mx >= score_thr:
        return {"ok": True, "score": mx,
                "rect": (bx + off_x, by + off_y, tw, th),
                "center": (bx + tw // 2 + off_x, by + th // 2 + off_y),
                "scale": sc, "elapsed_ms": elapsed, "best_score": mx}
    return {"ok": False, "score": mx, "rect": None, "center": None,
            "scale": sc, "elapsed_ms": elapsed, "best_score": mx}


def scales_around(center: float, spread: float = 0.10, n: int = 5) -> tuple:
    """围绕定位页面得到的实际缩放 center 生成部件模板候选尺度（避免全 0.8~1.25 扫描）。"""
    if n < 2:
        n = 2
    lo, hi = center * (1.0 - spread), center * (1.0 + spread)
    return tuple(round(lo + (hi - lo) * i / (n - 1), 4) for i in range(n))


# ---------------------------------------------------------------- OCR

_OCR_ENGINE = None


def ocr_engine():
    """RapidOCR 单例；Det max-960 参数 + 显式线程数见模块 docstring（波次1 性能定案）。

    实测（2026-09-09）：intra_op_num_threads=4 时连续推理稳定 37–50ms/条带；
    默认(-1)/1 会在 ~15 次连续推理后漂移到 0.4–4.9s/次（机理待查）。
    挂死自愈：连续超时达上限后由 ocr_run 触发重建（_rebuild_ocr_engine）。
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        _rebuild_ocr_engine()
    return _OCR_ENGINE


def _rebuild_ocr_engine():
    global _OCR_ENGINE
    from rapidocr import RapidOCR
    params = {"Det.limit_side_len": 960, "Det.limit_type": "max",
              "Global.log_level": "error",
              "EngineConfig.onnxruntime.intra_op_num_threads": 4,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    try:
        _OCR_ENGINE = RapidOCR(params=params)
    except Exception:
        _OCR_ENGINE = RapidOCR()


_OCR_TIMEOUT_S = 8.0
_OCR_MAX_HANGS = 3
_OCR_BREAK_S = 30.0          # 熔断冷却窗口（期间 OCR 直接快速失败）
_OCR_HANGS = 0
_OCR_BREAK_UNTIL = 0.0
_OCR_LAST_REBUILD = 0.0
_OCR_LOCK = threading.Lock()


def ocr_reset() -> None:
    """重置 OCR 熔断状态（诊断/自愈入口）。"""
    global _OCR_HANGS, _OCR_BREAK_UNTIL
    with _OCR_LOCK:
        _OCR_HANGS = 0
        _OCR_BREAK_UNTIL = 0.0


_OCR_CACHE: dict = {}                 # 内容哈希 -> OCR 结果（只存成功的）
_OCR_CACHE_MAX = 24                   # 上限：长跑时别把内存撑起来
_OCR_CACHE_STATS = {"hit": 0, "miss": 0}


def _content_key(bgr) -> str:
    """按**像素内容**做缓存键。

    为什么不用 id(bgr)：numpy 数组会被回收、id 会复用，用对象身份当键迟早串味；
    内容相同 → OCR 结果必然相同，是最安全的键。670k 像素哈希约 1ms，
    相对 4 秒的推理完全可以忽略。
    """
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    a = bgr if bgr.flags["C_CONTIGUOUS"] else np.ascontiguousarray(bgr)
    h.update(np.asarray(a.shape, dtype=np.int64).tobytes())
    h.update(a.tobytes())
    return h.hexdigest()


def ocr_cache_stats() -> dict:
    """缓存命中情况（供性能剖析脚本读取）。"""
    return dict(_OCR_CACHE_STATS, size=len(_OCR_CACHE), cap=_OCR_CACHE_MAX)


def ocr_cache_clear() -> None:
    _OCR_CACHE.clear()
    _OCR_CACHE_STATS.update(hit=0, miss=0)


def ocr_run(bgr, timeout_s=None) -> dict:
    """OCR 一张 BGR 图 → {txts, boxes[(x,y,w,h)], scores, elapsed_ms, ok}

    挂死保护（2026-09-09 实测：ORT 1.25/1.29 在本机存在概率性 det 推理挂死
    >400s；Py3.14+rapidocr3.9 组合）：每次推理放入守护线程，超时（默认 8s）放弃。
    自愈策略：单次挂死 → 立即重建 OCR 引擎（新实例脱离病态）；短时间连续挂死
    ≥_OCR_MAX_HANGS → 冷却 _OCR_BREAK_S（期间直接快速失败），冷却结束自动重建。
    """
    global _OCR_HANGS, _OCR_BREAK_UNTIL, _OCR_ENGINE, _OCR_LAST_REBUILD
    # 4️⃣ 帧内复用（2026-09-12 实测）：一次定位会调用 6 次 OCR、共 11.5 秒，其中同一块区域
    # 被反复 OCR（1288x520 两遍、640x240 两遍），重复部分就占 ~9.8 秒。同样的像素没必要
    # 认两遍。只缓存成功结果：失败/超时/熔断一律不缓存，否则会破坏"挂死自愈"的判定。
    try:
        ckey = _content_key(bgr)
    except Exception:
        ckey = None
    if ckey is not None:
        hit = _OCR_CACHE.get(ckey)
        if hit is not None:
            _OCR_CACHE_STATS["hit"] += 1
            return {"txts": list(hit["txts"]), "boxes": list(hit["boxes"]),
                    "scores": list(hit["scores"]), "elapsed_ms": 0.0,
                    "engine": hit["engine"], "ok": True, "cached": True}
        _OCR_CACHE_STATS["miss"] += 1
    timeout = timeout_s or _OCR_TIMEOUT_S
    now = time.perf_counter()
    with _OCR_LOCK:
        if _OCR_HANGS >= _OCR_MAX_HANGS:
            if now < _OCR_BREAK_UNTIL:
                return {"txts": [], "boxes": [], "scores": [], "elapsed_ms": 0.0,
                        "engine": "rapidocr", "ok": False, "error": "breaker_open"}
            _OCR_HANGS = 0
            _OCR_ENGINE = None          # 冷却结束 → 重建引擎自愈
    t0 = time.perf_counter()
    holder = {}

    def _work():
        holder["out"] = ocr_engine()(bgr)

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        with _OCR_LOCK:
            _OCR_HANGS += 1
            rebuild_gap = now - _OCR_LAST_REBUILD
            if (_OCR_HANGS >= _OCR_MAX_HANGS
                    or (_OCR_ENGINE is not None and rebuild_gap < 10.0)):
                # 短窗口内反复挂死 → 熔断冷却
                _OCR_HANGS = _OCR_MAX_HANGS
                _OCR_BREAK_UNTIL = now + _OCR_BREAK_S
            else:
                # 单次挂死 → 立即重建引擎自愈（下次调用加载新实例）
                _OCR_ENGINE = None
                _OCR_HANGS = 0
                _OCR_LAST_REBUILD = now
        return {"txts": [], "boxes": [], "scores": [],
                "elapsed_ms": (time.perf_counter() - t0) * 1000,
                "engine": "rapidocr", "ok": False, "error": "timeout",
                "hangs": _OCR_HANGS}
    out = holder.get("out")
    elapsed = (time.perf_counter() - t0) * 1000
    boxes, txts, scores = [], [], []
    if out is not None and out.boxes is not None:
        for box, txt, sc in zip(out.boxes, out.txts, out.scores):
            arr = np.asarray(box)
            x0, y0 = int(arr[:, 0].min()), int(arr[:, 1].min())
            x1, y1 = int(arr[:, 0].max()), int(arr[:, 1].max())
            boxes.append((x0, y0, x1 - x0, y1 - y0))
            txts.append(str(txt))
            scores.append(float(sc))
    result = {"txts": txts, "boxes": boxes, "scores": scores, "elapsed_ms": elapsed,
              "engine": "rapidocr(det-max960)", "ok": True}
    if ckey is not None:                    # 只缓存成功结果（见上方说明）
        if len(_OCR_CACHE) >= _OCR_CACHE_MAX:
            _OCR_CACHE.clear()
        _OCR_CACHE[ckey] = result
    return result


def ocr_run_auto(bgr, min_h=90, max_scale=3) -> dict:
    """部件级 OCR：小块图先原图识别，认不出（或只认出单字）时放大再试。

    双截图第二步框住的多是按钮这类小块（十几像素高的字），原图 OCR 容易把
    “登录”切成两个字或直接空结果；放大到 ~90px 高再识别更稳（M1 波次3 实测：
    76×53 原图 → ['登','录']，×3 → ['登录']）。返回结构与 ocr_run 一致，
    boxes 已换算回原图坐标，另带 scale_used。
    """
    r = ocr_run(bgr)
    h = int(bgr.shape[0]) if getattr(bgr, "shape", None) is not None else 0
    scale = 0
    if h and h < min_h and max_scale > 1:
        scale = min(max_scale, max(2, int(round(min_h / h))))
    texts = [str(t) for t in r.get("txts", []) if str(t).strip()]
    best_plain = max(texts, key=len) if texts else ""
    # 认不出、超时，或只认出孤零零一个字（“登”/“录”这种被切开的词）→ 放大再试
    need_more = (not best_plain) or bool(r.get("error")) or len(best_plain) == 1
    if scale and need_more:
        import cv2
        up = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        r2 = ocr_run(up)
        up_texts = [str(t) for t in r2.get("txts", []) if str(t).strip()]
        best_up = max(up_texts, key=len) if up_texts else ""
        if r2.get("ok") and up_texts and len(best_up) > len(best_plain):
            inv = (lambda b: (b[0] // scale, b[1] // scale,
                              max(1, b[2] // scale), max(1, b[3] // scale)))
            r2["boxes"] = [inv(b) for b in r2["boxes"]]
            r2["elapsed_ms"] = r.get("elapsed_ms", 0.0) + r2.get("elapsed_ms", 0.0)
            r2["scale_used"] = scale
            return r2
        r["scale_tried"] = scale
    r.setdefault("scale_used", 0)
    return r


def find_text_all_ocr(bgr, text, thr=0.75, upsample=2, max_n=8) -> list:
    """找文字 → 返回**全部**达标候选（同一次 OCR，按相似度降序）。

    与 find_text_ocr 的区别：那个只给最佳一个。同页常出现相同文字（列表里的同名列、
    重复的"保存"按钮、上下两个一样的输入框占位提示），只取"第一个命中"会挑错；
    这里把命中全部拿出来，交给定位层按"离录点近不近 / 邻居对不对得上"挑选。
    返回 [{box(x,y,w,h), center, score, matched_text, ocr_count, upsample, elapsed_ms}]
    """
    def _collect(img, up):
        r = ocr_run(img)
        if r.get("error"):
            return None
        if not r["boxes"]:
            r2 = ocr_run(img)                  # det 偶发空结果 → 重试一次
            if r2.get("error"):
                return None
            r = r2
        out = []
        for bx, txt, _sc in zip(r["boxes"], r["txts"], r["scores"]):
            sim = text_similar(txt, text)
            if sim < thr:
                continue
            b = bx
            if up > 1:                          # 放大过 → 坐标换算回原图
                b = (b[0] // up, b[1] // up, max(1, b[2] // up), max(1, b[3] // up))
            out.append({"box": b, "center": (b[0] + b[2] // 2, b[1] + b[3] // 2),
                        "score": round(sim, 4), "matched_text": txt,
                        "ocr_count": len(r["txts"]), "upsample": up if up > 1 else 0,
                        "elapsed_ms": r.get("elapsed_ms", 0.0)})
        return out

    hits = _collect(bgr, 1)
    if hits is None:
        return []
    if not hits and upsample > 1 and getattr(bgr, "shape", [999])[0] < 70:
        import cv2
        img2 = cv2.resize(bgr, None, fx=upsample, fy=upsample,
                          interpolation=cv2.INTER_CUBIC)
        hits2 = _collect(img2, upsample)
        if hits2:
            hits = hits2
    hits.sort(key=lambda c: (-c["score"], c["center"][1], c["center"][0]))
    return hits[:max_n]


def find_text_ocr(bgr, text, thr=0.75, upsample=2) -> dict:
    """
    在图中找文字 text（OCR 路径）。返回
    {ok, box(x,y,w,h), center, score(文本相似), elapsed_ms, matched_text, ocr_count, upsample}
    先原图 OCR；det 偶发空结果自动重试一次；找不到且允许放大时 2x 再试（小字更稳）。
    """
    def _search(img_bgr):
        r = ocr_run(img_bgr)
        if r.get("error"):
            return r, None                      # 超时/熔断：立即失败，不再重试
        if not r["boxes"]:
            r2 = ocr_run(img_bgr)               # det 偶发空 → 重试一次
            if r2.get("error"):
                return r2, None
            r = r2
        best = None
        for bx, txt, sc in zip(r["boxes"], r["txts"], r["scores"]):
            sim = text_similar(txt, text)
            if sim >= thr and (best is None or sim > best["sim"]):
                best = {"sim": sim, "box": bx, "txt": txt, "sc": sc}
        return r, best

    t0 = time.perf_counter()
    r, best = _search(bgr)
    used_upsample = 0.0
    # 2x 放大只用于小字输入（区域高 <70px）；大带放大既慢又易把多行卷进
    if (best is None and not r.get("error") and upsample > 1
            and bgr.shape[0] < 70 and bgr.shape[0] * upsample <= 8192):
        img2 = cv2.resize(bgr, None, fx=upsample, fy=upsample, interpolation=cv2.INTER_CUBIC)
        r2, best2 = _search(img2)
        if best2 is not None:
            used_upsample = upsample
            best = best2
            bx, by, bw, bh = best["box"]
            best["box"] = (bx // upsample, by // upsample, bw // upsample, bh // upsample)
            best["_from_up"] = True
    if best is None:
        return {"ok": False, "box": None, "center": None, "score": 0.0,
                "elapsed_ms": (time.perf_counter() - t0) * 1000, "matched_text": None,
                "ocr_count": len(r["txts"]), "upsample": used_upsample,
                "error": r.get("error")}
    x, y, w, h = best["box"]
    return {"ok": True, "box": best["box"], "center": (x + w // 2, y + h // 2),
            "score": best["sim"], "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "matched_text": best["txt"], "ocr_count": len(r["txts"]),
            "upsample": used_upsample}
