# -*- coding: utf-8 -*-
"""M4-WP1 导出器的测试：拼装对不对、参数化全不全、引用完整性拦不拦得住。

为什么这块要测得这么细：导出物是**给别人（Agent/开发者）用的产物**——
它错了不会在本产品里报错，而是在别人的项目里崩；而且"引用了一个不存在的部件"
这种错在运行时才暴露，用户根本无从查起。所以宁可在这里钉死。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from engine import exporter as E
from engine import schema
from engine.tests import support as S


def _login_script():
    """一个贴近真实的脚本：输入账号密码 → 点登录 → 校验 → 条件 + 循环 + 参数。"""
    page = S.page_spec_of(S.login_page()[0])
    user = {"page": page, "text": "用户名", "rect_in_page": [100, 200, 80, 24],
            "image": "data:image/png;base64,AAAA", "match": "auto"}
    pwd = {"page": page, "text": "密码", "rect_in_page": [100, 240, 80, 24],
           "image": "data:image/png;base64,BBBB", "match": "auto"}
    btn = {"page": page, "text": "登录", "rect_in_page": [100, 280, 60, 30],
           "image": "data:image/png;base64,CCCC", "match": "auto"}
    ok = {"page": page, "text": "登录成功", "rect_in_page": [200, 100, 90, 24],
          "image": "data:image/png;base64,DDDD", "match": "auto"}
    return schema.new_script("自动登录") | {"steps": [
        {"id": "s1", "type": "action", "action": "type", "target": user,
         "params": {"text": "admin"}},
        {"id": "s2", "type": "action", "action": "type", "target": pwd,
         "params": {"text": "pass123"}},
        {"id": "s3", "type": "action", "action": "click", "target": btn, "params": {},
         "expected_outcome": {"target": ok, "on_fail": {"strategy": "notify",
                                                        "message": "登录没成功"}}},
        {"id": "s4", "type": "loop", "loop": {"mode": "count", "count": 3},
         "body": [{"id": "s4a", "type": "action", "action": "wait",
                   "params": {"seconds": 2}}]},
        {"id": "s5", "type": "condition",
         "condition": {"target": ok, "exists": True},
         "then": [{"id": "s5a", "type": "action", "action": "notify",
                   "params": {"message": "好了"}}],
         "else": []},
    ]}


class WidgetsTest(unittest.TestCase):
    def test_collects_and_dedupes(self):
        sg = _login_script()
        w = E.collect_widgets(sg)
        self.assertEqual(len(w), 4, list(w))
        self.assertIn("登录", w)
        self.assertIn("用户名", w)
        self.assertEqual(w["登录"]["text"], "登录")
        self.assertEqual(w["登录"]["coord"], {"x": 100, "y": 280, "w": 60, "h": 30})
        self.assertTrue(w["登录"]["images"][0].startswith("data:image/png;base64,"))

    def test_same_widget_appears_once(self):
        """同一部件在多个步骤里被引用 → 注册表里只占一份（§8.7 全局去重）。"""
        sg = _login_script()
        sg["steps"].append({"id": "s6", "type": "action", "action": "click",
                            "target": json.loads(json.dumps(sg["steps"][2]["target"])),
                            "params": {}})
        w = E.collect_widgets(sg)
        self.assertEqual(len([k for k in w if k == "登录"]), 1, list(w))

    def test_name_collision_gets_suffix(self):
        """两个不同的部件同名 → 必须区分开，否则 skill 里会引用错部件。"""
        sg = _login_script()
        other = json.loads(json.dumps(sg["steps"][2]["target"]))
        other["rect_in_page"] = [500, 500, 10, 10]
        other["image"] = "data:image/png;base64,ZZZZ"
        sg["steps"].append({"id": "s7", "type": "action", "action": "click",
                            "target": other, "params": {}})
        w = E.collect_widgets(sg)
        self.assertIn("登录", w)
        self.assertIn("登录_2", w, list(w))


class ParamsTest(unittest.TestCase):
    def test_detects_literals(self):
        p = E.collect_params(_login_script())
        self.assertEqual(len(p), 3, p)          # 两处输入文字 + 一处等待
        self.assertEqual(p["p_1"]["default"], "admin")
        self.assertEqual(p["p_1"]["where"], "用户名")
        self.assertEqual(p["p_3"]["field"], "seconds")

    def test_custom_names(self):
        sg = _login_script()
        p = E.collect_params(sg, names={("s1", "text"): "username",
                                        ("s2", "text"): "password"})
        self.assertEqual(sorted(p), ["p_1" if False else "password", "p_3", "username"]
                         if False else sorted(["username", "password", "p_3"]))

    def test_apply_params_fills_without_touching_original(self):
        sg = _login_script()
        p = E.collect_params(sg)
        out = E.apply_params(sg, {"p_1": "zhangsan", "p_3": 9}, p)
        self.assertEqual(out["steps"][0]["params"]["text"], "zhangsan")
        self.assertEqual(out["steps"][3]["body"][0]["params"]["seconds"], 9)
        self.assertEqual(sg["steps"][0]["params"]["text"], "admin",
                         "导出物是快照，不许改原脚本")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.sg = _login_script()
        self.res = E.export_plan(self.sg)

    def test_plan_ok_and_has_expected_files(self):
        self.assertTrue(self.res["ok"], self.res["problems"])
        self.assertIn("skills/自动登录.py", self.res["files"])
        self.assertIn("tools/screen.py", self.res["files"])
        self.assertIn("tools/registry.py", self.res["files"])

    def test_skill_shape(self):
        src = self.res["files"]["skills/自动登录.py"]
        self.assertIn("SKILL_NAME = \"自动登录\"", src)
        self.assertIn("SYSTEM_PROMPT", src)
        self.assertIn("def build_task(", src)
        self.assertIn("def run(", src)
        self.assertIn("单向导出", src, "产物必须标注单向导出（DoD）")
        self.assertIn("不是自包含代码", src, "必须写明识别/输入在本机引擎（§8.2 ⚠️）")

    def test_widgets_embedded_in_tools(self):
        src = self.res["files"]["tools/screen.py"]
        self.assertIn("WIDGETS", src)
        self.assertIn("登录", src)
        for name in E.collect_widgets(self.sg):
            self.assertIn(name, src, f"部件「{name}」没被内嵌")

    def test_chain_translates_control_flow(self):
        """条件/循环要翻成 if/for/while，而不是被丢掉。"""
        src = self.res["files"]["skills/自动登录.py"] + ""
        chain = E.render_chain(self.sg, self.res["widgets"])
        self.assertIn("for _i in range(3)", chain)
        self.assertIn("if find_target(", chain)
        self.assertIn("verify_result(", chain)
        self.assertEqual(chain.count("verify_result("), 1)

    def test_all_products_compile(self):
        for path, src in self.res["files"].items():
            if path.endswith(".py"):
                compile(src, path, "exec")

    def test_verify_catches_missing_widget(self):
        """引用完整性：产物引用了注册表里没有的部件 → 必须报出来（DoD）。"""
        files = dict(self.res["files"])
        files["skills/bad.py"] = "def build_task():\n    return click('不存在的部件')\n"
        problems = E.verify_product(files, self.sg, self.res["widgets"],
                                    self.res["params"])
        self.assertTrue(problems)

    def test_verify_catches_syntax_error(self):
        files = {"skills/x.py": "def broken(:\n"}
        problems = E.verify_product(files, self.sg, {}, {})
        self.assertTrue(any("无法编译" in p for p in problems))

    def test_widget_without_signal_is_reported(self):
        problems = E.verify_product({}, self.sg, {"空部件": {"match": "auto"}}, {})
        self.assertTrue(any("没有任何识别信号" in p for p in problems))

    def test_invalid_script_is_refused(self):
        bad = {"version": "1.0", "name": "x", "steps": [{"id": "s1", "type": "action",
                                                        "action": "飞"}]}
        res = E.export_plan(bad)
        self.assertFalse(res["ok"])
        self.assertTrue(any("不合法" in p for p in res["problems"]))


class RunProductTest(unittest.TestCase):
    """产物必须**真的能被 import**（DoD：导出产物在真实目录加载通过）。"""

    def test_import_and_build_task(self):
        sg = _login_script()
        res = E.export_plan(sg)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, src in res["files"].items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(src, encoding="utf-8")
            sys.path.insert(0, str(root))
            try:
                import importlib
                mod = importlib.import_module("skills.自动登录")
                self.assertEqual(mod.SKILL_NAME, "自动登录")
                self.assertIn("find_target", mod.TOOLS)
                self.assertIn("verify_result", mod.TOOLS)
                task = mod.build_task()
                self.assertEqual(task["steps"][0]["params"]["text"], "admin")
                task2 = mod.build_task(p_1="zhangsan")
                self.assertEqual(task2["steps"][0]["params"]["text"], "zhangsan")
                self.assertEqual(task["steps"][0]["params"]["text"], "admin",
                                 "两次 build_task 之间不该互相污染")
                screen = importlib.import_module("tools.screen")
                self.assertIn("登录", screen.WIDGETS)
                self.assertEqual(screen.WIDGETS["登录"]["text"], "登录")
                self.assertTrue(callable(screen.fill))
            finally:
                sys.path.remove(str(root))
                for m in ("skills.自动登录", "tools.screen", "tools.registry",
                          "skills", "tools"):
                    sys.modules.pop(m, None)

    def test_registry_tool_schemas(self):
        sg = _login_script()
        res = E.export_plan(sg)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tools" / "registry.py").write_text(
                res["files"]["tools/registry.py"], encoding="utf-8")
            sys.path.insert(0, str(root))
            try:
                import importlib
                reg = importlib.import_module("tools.registry")
                names = [s["function"]["name"] for s in reg.schemas()]
                for n in ("find_target", "click", "type_text", "wait", "notify",
                          "verify_result"):
                    self.assertIn(n, names)
            finally:
                sys.path.remove(str(root))
                for m in ("tools.registry", "tools"):
                    sys.modules.pop(m, None)


class ExportToDirTest(unittest.TestCase):
    def test_writes_files(self):
        sg = _login_script()
        with tempfile.TemporaryDirectory() as d:
            res = E.export_to_dir(sg, d)
            self.assertTrue(res["ok"], res["problems"])
            self.assertTrue((Path(d) / "skills" / "自动登录.py").exists())
            self.assertTrue((Path(d) / "tools" / "screen.py").exists())


if __name__ == "__main__":
    unittest.main()
