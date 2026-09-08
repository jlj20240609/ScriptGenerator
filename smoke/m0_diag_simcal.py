# -*- coding: utf-8 -*-
"""可见性度量标定：在同源/异源样本上比较候选指标"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import m0lib
m0lib.setup_utf8_stdio()
m0lib.init_dpi_aware()
import numpy as np
import cv2
import m0_trial

def gray_small(img, w=192, h=120):
    g = cv2.cvtColor(cv2.resize(img, (w, h)), cv2.COLOR_BGR2GRAY).astype(np.float32)
    return g

def m_pixdiff(a, b, thr):
    return float(1.0 - (np.abs(a - b) > thr).mean())

def m_blocks(a, b, bw=24, bh=15, thr=35.0):
    # 每块平均亮度差异超阈的块占比（块数）
    n = (a.shape[0] // bh) * (a.shape[1] // bw)
    bad = 0
    for by in range(0, a.shape[0] - bh + 1, bh):
        for bx in range(0, a.shape[1] - bw + 1, bw):
            ma = a[by:by + bh, bx:bx + bw].mean()
            mb = b[by:by + bh, bx:bx + bw].mean()
            if abs(ma - mb) > thr:
                bad += 1
    return float(1.0 - bad / max(1, n))

def m_edges(a, b):
    # 块边缘密度差占比
    ea = cv2.Canny(a.astype(np.uint8), 60, 160)
    eb = cv2.Canny(b.astype(np.uint8), 60, 160)
    ea = cv2.resize(ea, (48, 30)).astype(np.float32) / 255.0
    eb = cv2.resize(eb, (48, 30)).astype(np.float32) / 255.0
    diff = np.abs(ea - eb)
    return float(1.0 - (diff > 0.25).mean())

titles = {}
def load():
    out = {}
    for name in ("dyn", "icons", "login", "tk"):
        t = m0lib.Target.load("smoke/targets/%s.json" % name)
        h = t.page["hwnd"]
        ref = m0lib.grab_window_content(h)
        rect = m0_trial.window_rect(h)
        grab = m0lib.grab_screen(rect)
        out[name] = (grab, ref, rect)
        print("%s: rect=%s ref_std=%.1f" % (name, rect, -1 if ref is None else float(ref.std())))
    return out

d = load()

def pairs():
    cases = []
    for n in ("dyn", "icons", "login", "tk"):
        g, r, _ = d[n]
        if r is not None:
            cases.append(("%s 同源" % n, g, r))
    # 异源：dyn屏 vs tkPW, icons屏 vs loginPW
    g_dyn, _, _ = d["dyn"]
    g_icons, _, _ = d["icons"]
    _, r_tk, _ = d["tk"]
    _, r_login, _ = d["login"]
    if r_tk is not None:
        cases.append(("dyn屏 vs tk窗", g_dyn, r_tk))
    if r_login is not None:
        cases.append(("icons屏 vs login窗", g_icons, r_login))
    return cases

for name, a, b in pairs():
    ga, gb = gray_small(a), gray_small(b)
    print("%-22s pix48=%.3f pix24=%.3f block=%.3f edge=%.3f" % (
        name, m_pixdiff(ga, gb, 48), m_pixdiff(ga, gb, 24),
        m_blocks(ga, gb), m_edges(ga, gb)))
