# M0 命中率/性能统计（自动生成）
生成时间: 2026-09-09T10:28:11.652

## ① 双截图闭环命中率

（另跳过 0 条环境性空转记录：窗口不存在/句柄失效等）
| 类别 | 目标 | 次数 | page命中 | 部件命中 | 校验 | 综合HIT | 环境性失败 | dev中位 | page中位ms | 部件中位ms | L1/L2/L3 分布 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| desktop | bili_set | 20 | 20 (100%) | 20 (100%) | 20 | **20 (100%)** | 0 | 1.0 | 499.5 | 5947.1 | L2:20 |

## ② UIA 覆盖率

| 进程 | 窗口 | 树节点 | 交互控件 | 有Name | 有AutoId | 结论 |
|---|---|---|---|---|---|---|
| RvRvpnGui.exe | Radmin VPN | 0 | 0 | 0 | 0 | 弱/无 |
| msedge.exe | 启动M0 — DeepSeek Ha | 284 | 110 | 105 | 54 | UIA可命中 |
| pycharm64.exe | ScriptGenerator –  | 7 | 4 | 5 | 1 | UIA可命中 |
| TextInputHost.exe | Windows 输入体验 | 0 | 0 | 0 | 0 | 弱/无 |
| SystemSettings.exe | 设置 | 0 | 0 | 0 | 0 | 弱/无 |
| QQ.exe | QQ | 8 | 0 | 0 | 0 | 弱/无 |
| WindowsTerminal.exe | Windows PowerShell | 36 | 18 | 25 | 17 | UIA可命中 |
| LightXtreme.exe | LightXtreme VPN | 42 | 4 | 11 | 1 | UIA可命中 |
| explorer.exe | Program Manager | 76 | 75 | 75 | 1 | UIA可命中 |

## OCR 基准

| crop | 首次ms | 中位ms |
|---|---|---|
| page-full 1280x800 | 18094 | 15472 |
| half-window 640x480 | 12753 | 8797 |
| widget 300x90 | 9045 | 5798 |
| chip 150x44 | 7391 | 4559 |
