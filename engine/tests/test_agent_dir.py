# -*- coding: utf-8 -*-
"""M4-WP2 Agent 目录适配与写入的测试（§8.6）。

三条安全默认必须钉住，因为它们都是"错了会毁别人项目"的那类：
  干跑优先（plan 不落盘）、冲突默认不覆盖、**绝不写到目标目录之外**。
另外"差集复用"要能证明：目标 Agent 已有的工具，我们不重复写、也不覆盖。
"""
import json
import tempfile
import unittest
from pathlib import Path

from engine import agent_dir as A
from engine import exporter as E
from engine.tests.test_exporter import _login_script


def _make_agent_dir(root: Path, with_existing=True):
    """造一个像样的目标 Agent 项目：有 tools/ skills/ 与注册入口。"""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (root / "skills" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tools" / "registry.py").write_text(
        "def schemas():\n    return []\n", encoding="utf-8")
    if with_existing:
        # 目标 Agent 已经有 click 与 find_target：导出时必须复用、不许覆盖
        (root / "tools" / "mine.py").write_text(
            "def click(name):\n"
            "    return {'ok': True, 'mine': True}\n\n"
            "def find_target(name):\n"
            "    return {'exists': True}\n\n"
            "def helper():\n    return 1\n", encoding="utf-8")
    (root / "skills" / "existing_skill.py").write_text("# 已有的技能\n", encoding="utf-8")
    return root


class ScanTest(unittest.TestCase):
    def test_finds_layout_tools_and_skills(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            sc = A.scan(root)
            self.assertTrue(sc["ok"], sc)
            self.assertEqual(sc["skills_dir"], "skills")
            self.assertEqual(sc["tools_dir"], "tools")
            self.assertIn("click", sc["tools"])
            self.assertIn("find_target", sc["tools"])
            self.assertIn("helper", sc["tools"], "已有工具清单要列全（差集依据）")
            self.assertIn("existing_skill", sc["skills"])
            self.assertIn("registry.py", sc["entries"])
            self.assertEqual(sc["framework"], "已识别")

    def test_reads_json_declared_tools(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            (root / "tools" / "manifest.json").write_text(json.dumps(
                {"tools": [{"name": "my_click"}, {"function": {"name": "my_type"}}]}),
                encoding="utf-8")
            sc = A.scan(root)
            self.assertIn("my_click", sc["tools"])
            self.assertIn("my_type", sc["tools"])

    def test_unknown_layout_falls_back_with_note(self):
        with tempfile.TemporaryDirectory() as d:
            sc = A.scan(Path(d))
            self.assertTrue(sc["ok"])
            self.assertEqual(sc["framework"], "通用")
            self.assertTrue(any("通用目录" in n for n in sc["notes"]), sc["notes"])

    def test_missing_dir(self):
        sc = A.scan("Z:/definitely/not/here")
        self.assertFalse(sc["ok"])

    def test_nested_layout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src" / "tools").mkdir(parents=True)
            (root / "src" / "skills").mkdir(parents=True)
            sc = A.scan(root)
            self.assertEqual(sc["tools_dir"], "src/tools")
            self.assertEqual(sc["skills_dir"], "src/skills")


class PlanTest(unittest.TestCase):
    def test_plan_is_dry_run_and_lists_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d), with_existing=False)
            res = A.plan(_login_script(), root)
            self.assertTrue(res["ok"], res)
            self.assertTrue(res["dry_run"])
            by = {f["rel"]: f["action"] for f in res["files"]}
            self.assertEqual(by["skills/自动登录.py"], "create")
            self.assertEqual(by["tools/screen.py"], "create")
            # 目标目录里已有的空 __init__.py 与自定义 registry.py 都不该被动：
            # 前者内容相同 → "已是最新"；后者内容不同 → 默认跳过（不覆盖别人）
            self.assertEqual(by["tools/__init__.py"], "identical")
            self.assertEqual(by["tools/registry.py"], "skip")
            self.assertTrue(any("registry.py" in n and "覆盖" in n for n in res["notes"]),
                            res["notes"])
            self.assertFalse((root / "tools" / "screen.py").exists(),
                             "plan() 只算不写")

    def test_existing_tools_are_reused_not_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            res = A.plan(_login_script(), root)
            self.assertIn("click", res["reused_tools"], res["reused_tools"])
            self.assertIn("find_target", res["reused_tools"])
            src = next(f["_src"] for f in res["files"] if f["rel"] == "tools/screen.py")
            self.assertIn("_agent_tool(\"click\")", src, "被复用的工具应留转发壳")
            self.assertNotIn("def click(name):\n    \"\"\"点一下", src,
                             "不该重新实现别人已有的工具")
            self.assertIn("def verify_result(", src, "对方没有的工具要正常生成")

    def test_existing_file_conflict_defaults_to_skip(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            (root / "skills" / "自动登录.py").write_text("# 别人的东西\n", encoding="utf-8")
            res = A.plan(_login_script(), root)
            f = next(x for x in res["files"] if x["rel"] == "skills/自动登录.py")
            self.assertEqual(f["action"], "skip", f)
            self.assertIn("不覆盖", f["reason"])
            self.assertNotIn("skills/自动登录.py", json.dumps(
                A.apply(res)["written"]), "跳过的不该被写")

    def test_overwrite_strategy_does_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            (root / "skills" / "自动登录.py").write_text("# 旧内容\n", encoding="utf-8")
            res = A.plan(_login_script(), root, {"conflict": "overwrite"})
            f = next(x for x in res["files"] if x["rel"] == "skills/自动登录.py")
            self.assertEqual(f["action"], "overwrite")
            out = A.apply(res)
            self.assertIn("skills/自动登录.py", out["written"])
            self.assertIn("单向导出", (root / "skills" / "自动登录.py").read_text("utf-8"))
            self.assertTrue((root / "skills" / "自动登录.py.bak").exists(),
                            "覆盖前应备份")

    def test_rename_strategy_avoids_clobber(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            (root / "skills" / "自动登录.py").write_text("# 别人的\n", encoding="utf-8")
            res = A.plan(_login_script(), root, {"conflict": "rename"})
            rels = [f["rel"] for f in res["files"]]
            self.assertIn("skills/自动登录_sg2.py", rels, rels)
            self.assertEqual((root / "skills" / "自动登录.py").read_text("utf-8"),
                             "# 别人的\n")

    def test_identical_content_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            A.export(_login_script(), root, {"dry_run": False})
            res = A.plan(_login_script(), root)
            same = [f for f in res["files"] if f["action"] == "identical"]
            self.assertTrue(same, "重复导出应识别出内容相同")
            self.assertEqual(res["write_count"], 0)


class ApplyTest(unittest.TestCase):
    def test_writes_into_detected_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src" / "tools").mkdir(parents=True)
            (root / "src" / "skills").mkdir(parents=True)
            res = A.export(_login_script(), root, {"dry_run": False})
            self.assertTrue(res["apply"]["ok"], res["apply"])
            self.assertTrue((root / "src" / "skills" / "自动登录.py").exists())
            self.assertTrue((root / "src" / "tools" / "screen.py").exists())

    def test_refuses_to_write_outside_target_dir(self):
        """安全红线：产物路径被改到目录之外时，必须拒绝执行。"""
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            res = A.plan(_login_script(), root)
            outside = Path(d).parent / "escaped.py"
            res["files"].append({"rel": "../escaped.py", "abs": str(outside),
                                 "action": "create", "reason": "test", "size": 1,
                                 "_src": "x = 1\n"})
            out = A.apply(res)
            self.assertFalse(out["ok"])
            self.assertIn("目标目录之外", out["note"])
            self.assertFalse(outside.exists())

    def test_apply_reports_refresh_hint(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            out = A.export(_login_script(), root, {"dry_run": False})["apply"]
            self.assertIn("刷新", out["note"])
            self.assertTrue(out["written"])

    def test_render_diff_is_human_readable(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            text = A.render_diff(A.plan(_login_script(), root))
            self.assertIn("目标目录", text)
            self.assertIn("新建", text)
            self.assertIn("复用", text)

    def test_product_still_valid_after_adapter(self):
        """适配之后产物仍要能过引用完整性校验（部件/工具都在）。"""
        with tempfile.TemporaryDirectory() as d:
            root = _make_agent_dir(Path(d))
            res = A.plan(_login_script(), root)
            self.assertEqual(res["problems"], [], res["problems"])


if __name__ == "__main__":
    unittest.main()
