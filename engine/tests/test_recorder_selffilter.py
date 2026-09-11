# -*- coding: utf-8 -*-
"""自家窗口过滤的测试：**用户点脚本构建器自己的界面，不该变成脚本步骤**。

为什么必须钉住：钩子是全局的，用户为了点「停止」而切回应用、点一下按钮，
那一串操作会被如实录进去——录出来的是"操作脚本构建器"，不是他想自动化的那件事。
这个 bug 只有在真人真机试的时候才会被发现，正是用户试出来的，所以更要用测试固定住。
"""
import unittest

from engine import recorder as R


def _click(t, x, y):
    return {"t": t, "kind": "click", "x": x, "y": y, "button": "left"}


def _key(t, key, mods=()):
    return {"t": t, "kind": "key", "key": key, "mods": list(mods), "at": None}


class _FakeHooker:
    def __init__(self):
        self.cb = None

    def start(self, cb):
        self.cb = cb

    def stop(self):
        pass


SELF_HWND = 4242
OTHER_HWND = 999


class OwnWindowFilterTest(unittest.TestCase):
    def _rec(self, **kw):
        hook = _FakeHooker()
        self.windows = {}          # (x,y) -> hwnd
        self.fg = [OTHER_HWND]
        kw.setdefault("window_of", lambda x, y: {"hwnd": self.windows.get((x, y), OTHER_HWND)})
        kw.setdefault("fg_window_of", lambda: self.fg[0])
        kw.setdefault("skip_hwnds", [SELF_HWND])
        r = R.Recorder(hooker=hook, **kw)
        r.start()
        return r, hook

    def test_click_on_own_window_is_dropped(self):
        r, hook = self._rec()
        self.windows[(10, 20)] = SELF_HWND          # 点在自家界面上
        hook.cb(_click(0.0, 10, 20))
        hook.cb(_click(1.0, 30, 40))                # 点在别的程序上
        res = r.stop()
        self.assertEqual(len(r.events), 1, "自家界面上的点击不该进事件流")
        self.assertEqual([b["at"] for b in res["blocks"]], [(30, 40)])
        self.assertEqual(res["skipped_own"], 1)

    def test_keys_in_own_window_are_dropped(self):
        """在应用里改"脚本名"之类打的字，不该变成「输入文字」步骤。"""
        r, hook = self._rec()
        self.fg[0] = SELF_HWND                      # 前台是自家窗口
        hook.cb(_key(0.0, "a"))
        hook.cb(_key(0.1, "b"))
        self.fg[0] = OTHER_HWND
        hook.cb(_key(1.0, "c"))
        r.stop()
        self.assertEqual([e["key"] for e in r.events], ["c"],
                         "只有回到别的程序里打的字才算步骤")

    def test_other_windows_are_unaffected(self):
        r, hook = self._rec()
        hook.cb(_click(0.0, 5, 5))
        hook.cb(_key(0.2, "x"))
        res = r.stop()
        self.assertEqual(res["skipped_own"], 0)
        self.assertEqual(len(r.events), 2)

    def test_no_skip_list_means_record_everything(self):
        r, hook = self._rec(skip_hwnds=None)
        self.windows[(1, 1)] = SELF_HWND
        hook.cb(_click(0.0, 1, 1))
        r.stop()
        self.assertEqual(len(r.events), 1, "没配置自家窗口时照常录（默认行为不变）")

    def test_query_failure_records_instead_of_swallowing(self):
        """查窗口失败时宁可多录一条，也不能把用户的真实操作悄悄吞掉。"""
        def boom(*_a):
            raise RuntimeError("WindowFromPoint 挂了")

        r, hook = self._rec(window_of=boom)
        hook.cb(_click(0.0, 1, 1))
        r.stop()
        self.assertEqual(len(r.events), 1, "查询失败不能导致丢步骤")

    def test_fg_query_failure_records(self):
        def boom():
            raise RuntimeError("GetForegroundWindow 挂了")

        r, hook = self._rec(fg_window_of=boom)
        hook.cb(_key(0.0, "a"))
        r.stop()
        self.assertEqual(len(r.events), 1)

    def test_scroll_on_own_window_is_dropped_too(self):
        r, hook = self._rec()
        self.windows[(7, 8)] = SELF_HWND
        hook.cb({"t": 0.0, "kind": "scroll", "x": 7, "y": 8, "dy": -120})
        r.stop()
        self.assertEqual(len(r.events), 0)


if __name__ == "__main__":
    unittest.main()
