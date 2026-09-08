# M0 命中率/性能统计（自动生成）
生成时间: 2026-09-08T21:01:59.796

## ① 双截图闭环命中率

| 类别 | 次数 | page命中 | 部件命中 | 校验 | 综合HIT | 环境性失败 | dev中位 | page中位ms | 部件中位ms | L1/L2/L3 分布 |
|---|---|---|---|---|---|---|---|---|---|---|
| web-login | 30 | 30 (100%) | 30 (100%) | 30 | **30 (100%)** | 0 | 1.0 | 633.6 | 4383.5 | L2:30 |
| erp-web | 30 | 30 (100%) | 30 (100%) | 28 | **28 (93%)** | 0 | 1.0 | 624.5 | 6445.1 | L2:29 L3:1 |
| desktop | 25 | 0 (0%) | 0 (0%) | 0 | **0 (0%)** | 25 | None | 811.8 | None | L0:25 |
| icon-app | 25 | 0 (0%) | 0 (0%) | 0 | **0 (0%)** | 25 | None | 598.6 | None | L0:25 |
| dynamic | 16 | 16 (100%) | 16 (100%) | 16 | **16 (100%)** | 0 | 0.0 | 657.9 | 4360.5 | L2:16 |

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
