# M1 IPC 契约（Electron UI ↔ Python 引擎）

> 版本：v0.1（2026-09-09）｜依据：《项目开发过程文档》§6.1（方法清单与分层约束）、
> 《项目分析文档》v0.31 §5.1/§7.3/§11、决策 16.1 #6（Electron + Python 独立进程本地 IPC）
> 实现：引擎侧 `engine/ipc.py`（`python -m engine serve`）；UI 侧 `app/`（Electron 主进程持有子进程与通道）
> 纪律：契约变更需版本化并同步 UI/引擎两侧（开发过程文档 §6.1）；本文件为方法/事件/错误码单一来源。

## 1. 传输与帧格式

- 传输：**stdio JSON-Lines**（UI 主进程 spawn `python -m engine serve`，逐行 JSON）。
- 协议：**JSON-RPC 2.0 子集**
  - 请求 `{"jsonrpc":"2.0","id":<int|str>,"method":"<名字>","params":{...}}`
  - 响应 `{"jsonrpc":"2.0","id":...,"result":{...}}` 或 `{"jsonrpc":"2.0","id":...,"error":{"code":N,"message":"...","data":{...}}}`
  - 通知（引擎→UI，无需回复）`{"jsonrpc":"2.0","method":"event.xxx","params":{...}}`
- 引擎 stdout **只承载协议**；日志走 stderr（UI 可折叠显示）。
- **坐标一律物理像素**（引擎进程已 DPI 感知；UI 覆盖层为 DIP，需 ×`devicePixelRatio` 换算后传参，
  回传坐标 ÷`devicePixelRatio` 显示 —— M0④ 实测 1px 无错位 @125%）。

## 2. 方法清单（UI → 引擎）

| 方法 | 参数 | 结果 | 说明 |
|---|---|---|---|
| `ping` | — | `{ok, engine_version, spec, dpi}` | 连通性与引擎信息 |
| `script.new` | `{name?}` | `{script}` | 新脚本（`.sgscript.json` v1.0 结构） |
| `script.load` | `{path}` | `{script}` | 读盘 + 校验；`script` 为完整对象（含 target 内嵌图 data-url，UI 可直接显示缩略图） |
| `script.save` | `{path, script}` | `{ok, path, targets_rev}` | 写盘前校验；写回前旧值由 UI 决定是否备份（`.bak.json`） |
| `page.capture` | `{rect:[x,y,w,h], grab:"screen"\|"window"?}` | `{ok, page, hwnd}` | **双截图第一步**：框住操作页面；`page` 为 §11 全量 PageSpec（`page.image` 即 data-url，UI 直接作缩略图回显）；窗口上下文（进程/标题/类）由点位置确定性采集 |
| `widget.capture` | `{rect_in_page:[x,y,w,h], text?}` | `{ok, target, page, ocr_others:[...]}` | **双截图第二步**：框页面内有效部件；自动 OCR 识别文字（回显"已识别：xx"，`text` 可覆盖）+ UIA hit-test；返回可直接放入步骤的 target（`target.image` 缩略图）与所属 `page` |
| `page.find` | `{page, search?}` | `{ok, rect, method, confidence, scale, elapsed_ms}` | 定位调试/校准核对（页面范围） |
| `widget.locate` | `{page_rect, target, exists?}` | `{ok, level, method, box, center, confidence, elapsed_ms}` | 部件三级定位（①UI 树→②相似度→③页内坐标） |
| `input.click` | `{x, y, dbl?}` | `{ok}` | 物理像素；UI 手动测试用 |
| `input.type` | `{text}` | `{ok}` | 文本输入 |
| `input.hotkey` | `{keys:"ctrl+s"}` | `{ok}` | 组合键 |
| `script.run` | `{script?, path?, options?:{calibrate?:bool, ai?:"off"\|"stub"\|"cloud", guard?:bool, loc_log?:path, first_run_calibrate?:bool}}` | `{run_id}` | **异步**：立即返回；进度经事件推送；同时只允许一个运行（否则 `4002 busy`） |
| `script.stop` | `{run_id?}` | `{ok}` | 停止当前运行（等价停止热键/停止按钮） |
| `ai.authorize` | `{provider:"zhipu", agree:bool}` | `{ok, authorized}` | 云端语义确认的**显式授权**（知情提示由 UI 呈现；未授权时校准只用本地语义桩） |
| `confirm.reply` | `{request_id, choice}` | `{ok}` | 回复引擎的人工确认请求（见 §3） |
| `engine.shutdown` | — | `{ok}` | 优雅退出（停止运行、释放资源） |

## 3. 事件与人工确认（引擎 → UI）

| 事件 | 参数 | 说明 |
|---|---|---|
| `event.log` | `{level:"info"\|"warn"\|"error", text, ts}` | 白话日志行（术语表左列词汇，UI 直接显示） |
| `event.step` | `{run_id, seq, step_id, path, status, label, method?, level?, confidence?, elapsed_ms?}` | 逐步进度（"第 2 步 ✓ 点一下"） |
| `event.run_state` | `{run_id, state:"running"\|"stopped"\|"done"}` | 运行状态 |
| `event.run_done` | `{run_id, status:"ok"\|"failed"\|"stopped", steps, clicks, types, notifies, targets_rev, saved?}` | 运行结束汇总 |
| `event.calibrate` | `{run_id, reason, ok, updated, note}` | 自动校准过程记录（"自动完成了重新识别"） |
| `event.confirm_request` | `{request_id, kind:"notify"\|"not_found"\|"outcome_fail"\|"ai_authorize"\|"calibrate_lowconf", message, options:[...], default}` | **需要用户决定**；UI 弹窗（白话）后回 `confirm.reply` |
| `event.error` | `{code, message, data}` | 引擎侧异常（运行外） |

**确认流程（引擎发起、UI 回复）**：引擎在一次运行中需要人时发 `event.confirm_request`（含
`request_id` 与白话 `message`、候选 `options`），阻塞等待 `confirm.reply`；
超时（默认 120s 无回复）按 `stop` 处理（无人值守安全默认）。
- `kind=notify`（提示我步骤）：options `["继续"]`
- `kind=not_found`（没找到 X，L1 兜底）：options `["继续","跳过","停止"]`
- `kind=outcome_fail`（做完后没看到 X，L2 失败）：options `["继续","停止"]`
- `kind=ai_authorize`：options `["同意上传","使用本地识别"]`（对应 §7.5 授权）

## 4. 错误码

| code | 含义 | UI 处理 |
|---|---|---|
| -32700 | JSON 解析失败 | 内部错误提示 |
| -32601 | 方法不存在 | 版本不匹配提示（升级 UI/引擎） |
| -32602 | 参数不合法 | 白话提示 + 日志 |
| 4001 | 引擎业务错误（data.code 见 `engine/errors.py`：page_not_found/widget_not_found/verify_failed/schema_invalid…） | 映射白话提示 |
| 4002 | 忙（已有运行/采集中） | 禁用按钮/提示 |
| 4003 | 未授权（云端 AI） | 弹授权 |

## 5. 典型序列（对照 M1 验收演示）

```
UI→ page.capture {rect: 页面框}          ← 覆盖层第一次框选
UI→ widget.capture {rect_in_page: 部件框} ← 覆盖层第二次框选（引擎回 text/缩略图 → "已识别：登录"）
UI→ input.type / script 组装（步骤列表）
UI→ script.save {path, script}
UI→ script.run {script, options:{calibrate:true, ai:"stub", first_run_calibrate:true}}
   ← event.run_state running
   ← event.calibrate {reason:"first_run", updated:false}
   ← event.step 逐条（"点一下 ✓"）
   ← event.confirm_request {kind:"outcome_fail", message:"做完后没看到“登录成功”，用户名或密码可能不对"}
UI→ confirm.reply {request_id, choice:"继续"}
   ← event.run_done {status:"ok"}
```

## 6. 版本与变更

- v0.1（2026-09-09）：首版；方法/事件/错误码以本表为准；引擎实现 `engine/ipc.py`，
  与《M1_引擎设计清单》校验模式契约（CalibRequest/CalibResult、HumanIO）一一映射。
- 变更流程：改本文件 → 同步 `engine/ipc.py` 与 `app/` 两侧 → 追加变更行 → 回归 `engine/tests/test_ipc.py`。
