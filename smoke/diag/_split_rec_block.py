# -*- coding: utf-8 -*-
"""把 main.js 里"录制专属"的自测段落搬进 runRecordUiChecks()，顶栏排版留在主流程。

为什么要动这次手术（用户 2026-09-11 要求"停止使用录制操作"）：那段自测会**真的开始录制**
（装全局键鼠钩子、把窗口缩下去、弹桌面浮条），用户在用机器时被这么来一下很打扰。
功能本身不动，只是不再被例行自测带着跑；要跑得显式加 --autotest-ui-record。

用标记定位而不是硬编码行号：行号会因为前面编辑而漂移，标记不会。
"""
from pathlib import Path

P = Path("app/main.js")
src = P.read_text(encoding="utf-8")
lines = src.splitlines()


def idx(needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i
    raise SystemExit(f"找不到标记：{needle}")


gate_end = idx("录制相关的自测已跳过") + 1          # if/else 块的收尾 '}'
rec_start = idx("真的走一遍开始录制")
bar_comment = idx("// ---- 桌面浮条 + 顶栏排版")
topbar_start = idx("// 顶栏排版：三种宽度下都不许折行")
cancel_start = idx("await uiClick('#btnRecordCancel');")
runmode_start = idx("// ---- 运行模式也必须收起窗口")
end = len(lines)

head = lines[:gate_end + 1]

rec_body = (lines[rec_start:bar_comment]          # 开始录制 + 实时积木
            + lines[bar_comment:topbar_start]     # 浮条检查
            + lines[cancel_start:runmode_start])  # 放弃 + applyRecorded + 浮条停止

main_body = lines[topbar_start:cancel_start] + lines[runmode_start:end]

header = [
    "",
    "// 录制相关的自测：**默认不跑**（用户 2026-09-11 要求「停止使用录制操作」）。",
    "// 这段会真的开始录制——装全局键鼠钩子、把窗口缩下去、弹桌面浮条，用户在用机器时",
    "// 很打扰。功能本身没动，只是不再被例行自测带着跑；要跑加 --autotest-ui-record。",
    "async function runRecordUiChecks() {",
]
footer = ["}", ""]

P.write_text("\n".join(head + header + rec_body + footer + main_body) + "\n", encoding="utf-8")
print(f"head={len(head)} rec={len(rec_body)} main={len(main_body)} 已写出 {P}")
