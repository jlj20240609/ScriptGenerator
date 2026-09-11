# -*- coding: utf-8 -*-
"""M4-WP3 导出 IPC 契约测试：界面调的方法名/字段/安全默认在这里钉死。

界面与引擎之间只有这一层契约。它错了的表现是"点了导出没反应"或更糟——
**预览说没写、其实写了**，或者**把用户 Agent 里的文件覆盖了**。
所以用真实的 RPC 通路（handle_line → 方法表 → 序列化）验一遍，不需要界面。
"""
import json
import tempfile
import unittest
from pathlib import Path

from engine import ipc as IP
from engine.tests.test_exporter import _login_script


def _agent_dir(root: Path, existing=True):
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (root / "skills" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tools" / "registry.py").write_text("def schemas():\n    return []\n",
                                                encoding="utf-8")
    if existing:
        (root / "tools" / "mine.py").write_text(
            "def click(name):\n    return {}\n", encoding="utf-8")
    return root


class ExportIpcTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.srv = IP.IpcServer()
        self.srv.send = lambda obj: self.sent.append(obj)

    def call(self, method, params=None):
        return self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params or {}})

    def result(self, method, params=None):
        r = self.call(method, params)
        self.assertNotIn("error", r, r.get("error"))
        return r["result"]

    def test_scan_reports_existing_tools(self):
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            r = self.result("export.scan", {"dir": str(root)})
            self.assertTrue(r["ok"])
            self.assertIn("click", r["tools"])
            self.assertEqual(r["framework"], "已识别")

    def test_scan_requires_dir(self):
        r = self.call("export.scan", {})
        self.assertIn("error", r)
        self.assertIn("选择目标目录", r["error"]["message"])


class ExportPlanApplyTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.srv = IP.IpcServer()
        self.srv.send = lambda obj: self.sent.append(obj)
        self.sg = _login_script()

    def call(self, method, params=None):
        return self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params or {}})

    def result(self, method, params=None):
        r = self.call(method, params)
        self.assertNotIn("error", r, r.get("error"))
        return r["result"]

    def test_plan_is_dry_run_and_returns_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            r = self.result("export.plan", {"script": self.sg, "dir": str(root)})
            self.assertTrue(r["ok"])
            self.assertTrue(r["plan_id"])
            self.assertIn("目标目录", r["diff"], "要给人话预览")
            self.assertIn("click", r["reused_tools"])
            self.assertFalse((root / "tools" / "screen.py").exists(),
                             "预览阶段不许落盘")

    def test_apply_writes_and_consumes_plan(self):
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            plan = self.result("export.plan", {"script": self.sg, "dir": str(root)})
            out = self.result("export.apply", {"plan_id": plan["plan_id"]})
            self.assertTrue(out["ok"], out)
            self.assertTrue((root / "tools" / "screen.py").exists())
            self.assertTrue((root / "skills" / "自动登录.py").exists())
            self.assertIn("刷新", out["note"])
            # 计划用完即废，防止重复点"确认"写出两遍
            again = self.call("export.apply", {"plan_id": plan["plan_id"]})
            self.assertIn("error", again)

    def test_conflict_default_is_not_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            victim = root / "skills" / "自动登录.py"
            victim.write_text("# 用户自己的东西\n", encoding="utf-8")
            plan = self.result("export.plan", {"script": self.sg, "dir": str(root)})
            self.result("export.apply", {"plan_id": plan["plan_id"]})
            self.assertEqual(victim.read_text(encoding="utf-8"),
                             "# 用户自己的东西\n", "默认不许覆盖")

    def test_conflict_overwrite_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            victim = root / "skills" / "自动登录.py"
            victim.write_text("# 旧的\n", encoding="utf-8")
            plan = self.result("export.plan", {"script": self.sg, "dir": str(root),
                                               "conflict": "overwrite"})
            out = self.result("export.apply", {"plan_id": plan["plan_id"],
                                               "conflict": "overwrite"})
            self.assertTrue(out["ok"])
            self.assertIn("单向导出", victim.read_text(encoding="utf-8"))
            self.assertTrue((root / "skills" / "自动登录.py.bak").exists(),
                            "覆盖前要备份")

    def test_plan_without_script_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.call("export.plan", {"dir": d})
            self.assertIn("error", r)

    def test_apply_unknown_plan_is_refused(self):
        r = self.call("export.apply", {"plan_id": "x999"})
        self.assertIn("error", r)
        self.assertIn("过期", r["error"]["message"])

    def test_events_are_json_serializable(self):
        """推送走同一条 stdout 通道，必须能序列化。"""
        with tempfile.TemporaryDirectory() as d:
            root = _agent_dir(Path(d))
            self.result("export.plan", {"script": self.sg, "dir": str(root)})
            for e in self.sent:
                json.dumps(e, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
