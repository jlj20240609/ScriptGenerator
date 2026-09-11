// ScriptGenerator M1 主进程：引擎进程管理（stdio JSON-RPC）+ 主窗口 + 双截图选区覆盖层
// 契约见 docs/M1_IPC契约.md v0.1（renderer 只与主进程通信，主进程持有引擎子进程）
const { app, BrowserWindow, ipcMain, dialog, screen, Menu, Notification } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const readline = require('readline');

const ROOT = path.join(__dirname, '..');           // 仓库根（引擎从这里以 python -m engine 启动）
const AUTOTEST = process.argv.includes('--autotest');
const AUTOTEST_PICK = process.argv.includes('--autotest-pick');
const AUTOTEST_DEMO = process.argv.includes('--autotest-demo');
const AUTOTEST_UI = process.argv.includes('--autotest-ui');
const FIXTURE_TITLE = 'M0 演示登录';
const demoState = { confirms: [], nextPick: null, boxes: null };

function findPython() {
  if (process.env.ENGINE_PYTHON && fs.existsSync(process.env.ENGINE_PYTHON)) {
    return process.env.ENGINE_PYTHON;
  }
  const cands = [
    path.join(ROOT, 'smoke', '.venv-ortcpu', 'Scripts', 'python.exe'),
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
  ];
  const hit = cands.find((p) => fs.existsSync(p));
  return hit || 'python';
}

// ---------------------------------------------------------------- 引擎客户端

class EngineClient {
  constructor() {
    this.proc = null;
    this.pending = new Map();
    this.seq = 1;
    this.onEvent = null;      // (msg) => void，转发给渲染层
    this.onExit = null;
  }

  start() {
    if (this.proc) return;
    const py = findPython();
    this.proc = spawn(py, ['-u', '-m', 'engine', 'serve'], {
      cwd: ROOT,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const rl = readline.createInterface({ input: this.proc.stdout });
    rl.on('line', (line) => {
      let msg = null;
      try { msg = JSON.parse(line); } catch (e) {
        console.error('[engine] 非法输出:', line.slice(0, 200));
        return;
      }
      if (msg.id !== undefined && msg.id !== null && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.error) p.reject(Object.assign(new Error(msg.error.message || '引擎错误'), {
          code: msg.error.code, data: msg.error.data,
        }));
        else p.resolve(msg.result || {});
        return;
      }
      if (this.onEvent) this.onEvent(msg);
    });
    this.proc.stderr.on('data', (d) => process.stderr.write('[engine] ' + d));
    this.proc.on('exit', (code) => {
      console.log('[engine] exited', code);
      this.proc = null;
      for (const [, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error('引擎进程已退出'));
      }
      this.pending.clear();
      if (this.onExit) this.onExit(code);
    });
  }

  call(method, params = {}, timeoutMs = 180000) {
    this.start();
    return new Promise((resolve, reject) => {
      const id = this.seq++;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('引擎响应超时：' + method));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  stop() {
    if (!this.proc) return;
    try { this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 99999, method: 'engine.shutdown' }) + '\n'); } catch (e) { /* ignore */ }
    setTimeout(() => { if (this.proc) { try { this.proc.kill(); } catch (e) { /* ignore */ } } }, 1500);
  }
}

const engine = new EngineClient();

// 排障日志：Electron 在 Windows 上不往 stdout 写，关键诊断落到文件（用户报"框选不对"时靠它）
const DEBUG_LOG = path.join(require('os').tmpdir(), 'm1_ui_debug.log');

function dbg(...parts) {
  const line = `[${new Date().toISOString().slice(11, 23)}] ` + parts.join(' ');
  try {
    fs.appendFileSync(DEBUG_LOG, line + '\n');
  } catch (e) {
    try { fs.appendFileSync(path.join(__dirname, 'debug_fallback.log'), line + '\n'); }
    catch (e2) { /* ignore */ }
  }
  console.log(line);
}
dbg('[启动] pid=' + process.pid + ' 日志文件=' + DEBUG_LOG);

// ---------------------------------------------------------------- 窗口

let win = null;
let overlay = null;
let overlayPicker = null;      // {pageRect, step, origin, scale, resolve, reject}

function createMainWindow() {
  win = new BrowserWindow({
    width: 1240,
    height: 840,
    minWidth: 1040,
    minHeight: 680,
    title: '脚本构建器',
    backgroundColor: '#f4f6fb',
    show: false,
    // 现代观感：隐藏系统标题栏（顶部条由页面自绘），但保留最小化/最大化/关闭按钮
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#ffffff', symbolColor: '#475569', height: 68 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // 录制时窗口会被最小化，界面逻辑（计时、实时积木、停止收尾）不能因此被节流：
      // 否则"按热键停止"之后界面可能过很久才反应过来。
      backgroundThrottling: false,
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  // 先隐藏再显示可以避免白闪，但 ready-to-show 偶尔不触发（实测窗口一直不可见）→ 三重兜底
  const showWin = () => { if (win && !win.isDestroyed() && !win.isVisible()) win.show(); };
  win.once('ready-to-show', showWin);
  win.webContents.once('did-finish-load', showWin);
  setTimeout(showWin, 1500);
  win.on('closed', () => {
    win = null;
    // 主窗口关掉时如果选区层还开着，会剩下一个全屏遮罩、用户没法退出
    if (overlay) { try { overlay.close(); } catch (e) { /* ignore */ } }
  });
}

function openOverlay() {
  const { workArea } = screen.getPrimaryDisplay();
  overlay = new BrowserWindow({
    x: workArea.x, y: workArea.y, width: workArea.width, height: workArea.height,
    transparent: true, frame: false, resizable: false, movable: false,
    alwaysOnTop: true, skipTaskbar: true, hasShadow: false, fullscreenable: false,
    backgroundColor: '#00000000',
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver');
  overlay.loadFile(path.join(__dirname, 'overlay.html'));
  overlay.on('closed', () => { dbg('[选区] 覆盖层已关闭'); overlay = null; });
  return overlay;
}

// 双截图选区：第一次框页面 → 第二次框部件 → 返回物理像素矩形
function pickTarget() {
  return new Promise((resolve, reject) => {
    const display = screen.getPrimaryDisplay();
    dbg(`[选区] 开始：主窗口=${win ? JSON.stringify(win.getBounds()) : 'null'} ` +
      `workArea=${JSON.stringify(display.workArea)} 屏幕=${display.size.width}×` +
      `${display.size.height} 缩放=${display.scaleFactor}`);
    overlayPicker = {
      pageRect: null, step: 'page',
      origin: { x: display.workArea.x, y: display.workArea.y },
      scale: display.scaleFactor || 1,
      resolve, reject, minimizedMain: false,
    };
    // 小屏上构建器会压住要框的页面（截图会把构建器也拍进去）→ 框选期间先让开
    if (win && !win.isDestroyed()) {
      try { win.minimize(); overlayPicker.minimizedMain = true; } catch (e) { /* ignore */ }
    }
    openOverlay();
    if (!overlay) { reject(new Error('无法打开选区层')); return; }
    dbg('[选区] 覆盖层已创建 ' + JSON.stringify(overlay.getBounds()));
    overlay.webContents.once('did-finish-load', () => dbg('[选区] 覆盖层加载完成'));
    armOverlayAutoPick();          // 自动演示：合成两次框选（真人用鼠标）
  });
}

// --autotest-demo：覆盖层加载完就注入合成框选（走的是与真人同样的选区代码路径）
function armOverlayAutoPick() {
  if (!AUTOTEST_DEMO || !demoState.boxes || !overlay) return;
  const inject = async () => {
    try {
      await new Promise((r) => setTimeout(r, 350));
      overlay.webContents.executeJavaScript(
        `window.__autoPick(${JSON.stringify(demoState.boxes)})`, true)
        .catch(() => { /* 覆盖层随后被关闭 */ });
    } catch (e) { /* ignore */ }
  };
  if (overlay.webContents.isLoading()) overlay.webContents.once('did-finish-load', inject);
  else inject();
}

function finishPick(result) {
  const p = overlayPicker;
  overlayPicker = null;
  if (overlay) { try { overlay.close(); } catch (e) { /* ignore */ } }
  if (p && p.minimizedMain && win && !win.isDestroyed()) {
    try { win.restore(); win.focus(); } catch (e) { /* ignore */ }
  }
  if (demoState.nextPick) { const r = demoState.nextPick; demoState.nextPick = null; r(result); }
  if (!p) return;
  dbg('[选区] 结束 ' + JSON.stringify(result ? { ok: result.ok, error: result.error,
    cancel: result.cancel, rect: result.target && result.target.rect_in_page } : null));
  if (result && result.cancel) p.reject(new Error('已取消'));
  else p.resolve(result);
}

ipcMain.on('overlay:debug', (_e, payload) => {
  dbg('[覆盖层] ' + JSON.stringify(payload));
});

ipcMain.handle('overlay:selection', async (_e, payload) => {
  if (!overlayPicker) return { ok: false };
  const { origin, scale } = overlayPicker;
  const toPhysical = (r) => [
    Math.round((r.x + origin.x) * scale), Math.round((r.y + origin.y) * scale),
    Math.round(r.w * scale), Math.round(r.h * scale),
  ];
  // 选区诊断（真人反馈"指示器与截图位置不符"时靠这行数据定位）
  const ow = overlay ? overlay.getBounds() : null;
  dbg(`[选区] stage=${payload.step} dpr=${payload.dpr} 覆盖层=${JSON.stringify(ow)} ` +
    `原点=${JSON.stringify(origin)} 缩放=${scale} DIP=${JSON.stringify(payload.rect)} ` +
    `物理=${JSON.stringify(toPhysical(payload.rect))}`);
  if (payload.step === 'page') {
    overlayPicker.pageRect = toPhysical(payload.rect);
    overlayPicker.step = 'widget';
    return { ok: true, step: 'widget',
      hint: '第二步：框住要操作的位置（比如“登录”按钮）' };
  }
  // 第二步：部件 → 先收起选区层（遮罩会压暗截图），再调引擎双截图采集
  const pageRect = overlayPicker.pageRect;
  const wRect = toPhysical(payload.rect);
  const rel = [
    Math.max(0, wRect[0] - pageRect[0]), Math.max(0, wRect[1] - pageRect[1]),
    wRect[2], wRect[3],
  ];
  try {
    if (overlay) {
      overlay.hide();                        // 存进脚本的图必须是"干净"的屏幕
      await new Promise((r) => setTimeout(r, 250));
    }
    const pr = await engine.call('page.capture', { rect: pageRect });
    const wr = await engine.call('widget.capture', { rect_in_page: rel });
    finishPick({ ok: true, page: pr.page, hwnd: pr.hwnd, target: wr.target,
      ocr_others: wr.ocr_others });
    return { ok: true, done: true };
  } catch (err) {
    finishPick({ ok: false, error: err.message });
    return { ok: false, error: err.message };
  }
});

ipcMain.on('overlay:cancel', () => finishPick({ ok: false, cancel: true }));

// ---------------------------------------------------------------- 渲染层 API

// 桌面浮动「停止录制」条。
// 为什么要有它（用户反馈）：停止热键 Ctrl+Alt+Q 被别的软件占用了，等于没有停止手段；
// 而且录制时主窗口已经最小化，用户不该为了停止去任务栏翻窗口。
// 这个条：无边框、置顶、不进任务栏、整条可拖动（含边缘）；**它自己的点击会被录制器
// 按窗口句柄过滤掉**，不会变成脚本步骤（句柄在 record.start 时一并传给引擎）。
let stopBar = null;

function hwndOf(w) {
  try {
    if (!w || w.isDestroyed()) return 0;
    const h = w.getNativeWindowHandle();
    return h.length >= 8 ? Number(h.readBigUInt64LE(0)) : h.readUInt32LE(0);
  } catch (err) {
    return 0;
  }
}

function openStopBar(kind) {
  if (stopBar && !stopBar.isDestroyed()) {
    try { stopBar.webContents.send('bar-mode', { kind: kind || workKind || 'record' }); }
    catch (err) { /* ignore */ }
    return stopBar;
  }
  const w = 176;
  const h = 60;
  const area = screen.getPrimaryDisplay().workArea;
  stopBar = new BrowserWindow({
    width: w, height: h,
    x: Math.round(area.x + area.width - w - 28),
    y: Math.round(area.y + area.height - h - 28),
    frame: false, transparent: true, resizable: false, movable: true,
    alwaysOnTop: true, skipTaskbar: true, show: false, maximizable: false,
    minimizable: false, fullscreenable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      backgroundThrottling: false,
    },
  });
  stopBar.setAlwaysOnTop(true, 'screen-saver');
  stopBar.loadFile(path.join(__dirname, 'stopbar.html'));
  stopBar.webContents.once('did-finish-load', () => {
    try { stopBar.webContents.send('bar-mode', { kind: kind || 'record' }); }
    catch (err) { /* ignore */ }
  });
  stopBar.once('ready-to-show', () => { try { stopBar.show(); } catch (err) { /* ignore */ } });
  stopBar.on('closed', () => { stopBar = null; });
  return stopBar;
}

function closeStopBar() {
  if (stopBar && !stopBar.isDestroyed()) {
    try { stopBar.close(); } catch (err) { /* ignore */ }
  }
  stopBar = null;
}

// 本窗口的原生句柄（HWND）。传给引擎，让录制器把"用户在脚本构建器里的操作"过滤掉：
// 否则用户为了点「停止」切回来、点一下按钮，那一串会被如实录成步骤。
function selfHwnd() {
  return hwndOf(win);
}

ipcMain.handle('engine:call', async (_e, { method, params }) => {
  try {
    const p = Object.assign({}, params || {});
    // 录制：先把浮条建出来，再把「浮条 + 主窗口」的句柄交给引擎，
    // 这样用户为了点停止而切回应用的操作不会被录成步骤。
    if (method === 'record.start') {
      openStopBar('record');
      p.self_hwnds = [selfHwnd(), hwndOf(stopBar)].filter((x) => x);
    }
    // 运行：**必须把构建器窗口收起来**。否则它就压在目标画面上，截图里拍到的是
    // 我们自己——页面/部件当然找不到（用户实测反馈："脚本运行时没有最小化，导致识别失败"）。
    // 注意：script.run 成功时返回的是 {run_id}，**没有 ok 字段**（真实失败会抛错、
    // 走下面的 catch）。早先按 result.ok 判断"是否失败"，结果每次都把刚进入的
    // 运行模式立刻退掉——窗口根本没缩。
    if (method === 'script.run') enterWorkMode('run');
    const result = await engine.call(method, p);
    if (method === 'record.start' && !(result && result.ok)) leaveWorkMode('record');
    return { ok: true, result };
  } catch (err) {
    if (method === 'record.start') leaveWorkMode('record');
    if (method === 'script.run') leaveWorkMode('run');
    return { ok: false, error: { message: err.message, code: err.code, data: err.data } };
  }
});

// 浮条上的「停止录制」：转给主窗口的渲染层去走完整流程
// （停止 → OCR 反查 → 接步骤 → 恢复窗口 → 关掉浮条）。
ipcMain.handle('record:stop-request', async () => {
  if (win && !win.isDestroyed()) win.webContents.send('record:stop-request');
  return { ok: true };
});
ipcMain.handle('record:close-stopbar', async () => { closeStopBar(); return { ok: true }; });

// ---- 工作模式（录制 / 运行）：一律把构建器窗口收起来，并给一条桌面浮条能停 ----
// 为什么要统一（用户实测反馈）：录制时最小化做了，**运行**时漏了——构建器就压在
// 目标画面上，截图里拍到的是我们自己，页面自然找不到。运行和录制是同一类问题，
// 就该走同一条路：窗口收起 + 桌面浮条可停 + 结束后自动恢复。
let workKind = null;      // 'record' | 'run' | null

function enterWorkMode(kind) {
  workKind = kind;
  console.log('[work] enter', kind);
  openStopBar(kind);
  try { if (win && !win.isDestroyed()) win.minimize(); } catch (err) { /* 不影响主流程 */ }
}

function leaveWorkMode(kind) {
  console.log('[work] leave', kind, 'current=', workKind);
  if (kind && workKind && workKind !== kind) return;   // 另一种模式还在进行，别误恢复
  workKind = null;
  closeStopBar();
  try {
    if (win && !win.isDestroyed()) { win.restore(); win.show(); win.focus(); }
  } catch (err) { /* ignore */ }
}

ipcMain.handle('ui:recordingMode', async (_e, { on, hotkey }) => {
  if (!win || win.isDestroyed()) return { ok: false };
  if (on) {
    enterWorkMode('record');
    try {
      if (Notification.isSupported()) {
        new Notification({
          title: '正在录制你的操作',
          body: '桌面上有一条「停止录制」，点它就能停。' +
            `本应用和那条浮条上的操作都不会计入步骤（热键 ${hotkey || 'Ctrl+Alt+Q'} 也可以）。`,
        }).show();
      }
    } catch (err) { /* 通知失败不影响录制 */ }
  } else {
    leaveWorkMode('record');
  }
  return { ok: true };
});

// 运行结束（引擎推 event.run_done）由渲染层回调这里恢复窗口
ipcMain.handle('ui:runMode', async (_e, { on }) => {
  if (on) enterWorkMode('run'); else leaveWorkMode('run');
  return { ok: true };
});

ipcMain.handle('file:dialog', async (_e, { kind, defaultPath }) => {
  if (AUTOTEST_DEMO) {
    // 自动演示：原生文件对话框无法被脚本点击 → 固定用临时脚本文件
    const p = path.join(app.getPath('temp'), 'm1_demo_script.sgscript.json');
    console.log('[demo] 文件对话框（自动）:', kind, '→', p);
    return p;
  }
  if (kind === 'dir') {
    // 选目录（导出向导用）：只读扫描目标 Agent 项目，不做任何写入
    const r = await dialog.showOpenDialog(win, {
      title: '选择智能体的项目目录', properties: ['openDirectory'],
    });
    return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
  }
  if (kind === 'save') {
    const r = await dialog.showSaveDialog(win, {
      title: '保存脚本', defaultPath: defaultPath || 'script.sgscript.json',
      filters: [{ name: '脚本文件', extensions: ['json'] }],
    });
    return r.canceled ? null : r.filePath;
  }
  const r = await dialog.showOpenDialog(win, {
    title: '打开脚本', properties: ['openFile'],
    filters: [{ name: '脚本文件', extensions: ['json'] }],
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});

ipcMain.handle('ui:pickTarget', async () => {
  try {
    const r = await pickTarget();
    return r;
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

// 弹窗按场景定语气（M2-WP9）：同一句"确认"会让人分不清在问什么。
// 文案一律用白话（术语表左列），不出现"定位/校准/置信度"这类词。
const CONFIRM_STYLE = {
  notify: { type: 'info', title: '请你处理一下' },
  not_found: { type: 'warning', title: '这一步没找到' },
  outcome_fail: { type: 'warning', title: '做完后没看到预期的结果' },
  ai_authorize: { type: 'question', title: '需要你同意' },
};

ipcMain.handle('ui:confirm', async (_e, { message, options, defaultLabel, kind }) => {
  const style = CONFIRM_STYLE[kind] || { type: 'info', title: '需要你确认' };
  if (AUTOTEST_DEMO) {
    // 自动演示：原生弹窗无法被脚本点击 → 记录后作答（弹窗链路本身照走）。
    // 有“停止”说明是真失败：直接停，别在“继续”里反复重试。
    const choice = options.includes('停止') ? '停止'
      : (defaultLabel || options[options.length - 1] || options[0]);
    demoState.confirms.push({ kind, message, options, choice });
    console.log(`[demo] 弹窗（自动选择）[${kind || '-'}] ${style.title}：`, message, '→', choice);
    return choice;
  }
  const r = await dialog.showMessageBox(win, {
    type: style.type, title: style.title, message,
    buttons: options, defaultId: Math.max(0, options.indexOf(defaultLabel)),
    noLink: true, cancelId: -1,
  });
  return options[r.response] || defaultLabel || options[0];
});

// ---------------------------------------------------------------- 启动

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);          // 去掉 File/Edit/View 默认菜单（现代观感）
  engine.onEvent = (msg) => { if (win && !win.isDestroyed()) win.webContents.send('engine-event', msg); };
  engine.start();
  createMainWindow();
  if (AUTOTEST_DEMO) runAutoDemo();
  else if (AUTOTEST_PICK) runAutoPickTest();
  else if (AUTOTEST_UI) runAutoUiTest();
  else if (AUTOTEST) runAutoTest();
});

app.on('window-all-closed', () => { engine.stop(); app.quit(); });

// 自动冒烟（选区）：拉起真实 fixture 窗口 → 合成两次框选 → 双截图采集 → 定位复验
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function withTimeout(p, ms, msg) {
  return Promise.race([p, new Promise((_r, rej) => setTimeout(() => rej(new Error(msg)), ms))]);
}

function findEdge() {
  const cands = [
    path.join(process.env['ProgramFiles(x86)'] || 'C:/Program Files (x86)',
      'Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.ProgramFiles || 'C:/Program Files',
      'Microsoft/Edge/Application/msedge.exe'),
  ];
  return cands.find((p) => fs.existsSync(p)) || null;
}

// 目标窗口：已存在就复用，否则用 Edge app 模式拉起 fixture（与回归脚本同一口径）
async function ensureFixtureWindow() {
  // 演示页面可能改过：每次都刷新临时副本（曾因“只复制一次”的旧副本漏掉新脚本）
  const dst = path.join(app.getPath('temp'), 'web_login.html');
  fs.copyFileSync(path.join(ROOT, 'smoke', 'fixtures', 'web-login.html'), dst);
  let wins = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows;
  if (wins.length) {
    // 复用已有窗口时刷新一次，确保加载的是当前 fixture
    await engine.call('window.activate', { title: FIXTURE_TITLE });
    await sleep(300);
    try { await engine.call('input.hotkey', { keys: 'f5' }); } catch (e) { /* ignore */ }
    await sleep(1500);
    wins = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows;
    if (wins.length) return wins[0];
  }
  const edge = findEdge();
  if (!edge) throw new Error('未找到 msedge.exe，无法拉起演示页面');
  const url = require('url').pathToFileURL(dst).href;
  const child = spawn(edge, [
    `--user-data-dir=${path.join(app.getPath('temp'), 'm1_edge_profile')}`,
    '--no-first-run', '--no-default-browser-check', '--window-size=1020,780', `--app=${url}`,
  ], { detached: true, stdio: 'ignore' });
  child.unref();
  for (let i = 0; i < 40; i++) {
    await sleep(1500);
    wins = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows;
    if (wins.length) { await sleep(3000); return wins[0]; }   // 等渲染稳定
  }
  throw new Error('等待演示窗口超时');
}

async function runAutoPickTest() {
  const log = (...a) => console.log('[autotest-pick]', ...a);
  try {
    await sleep(1200);
    const w = await ensureFixtureWindow();
    log('目标窗口', w.title, '物理 rect', w.rect, w.class);
    // 要框的部件位置：先在实时画面上定位“登录”（不依赖历史资产几何，窗口尺寸变了也不怕）
    const loc = await engine.call('widget.locate',
      { page_rect: w.rect, target: { text: '登录', match: 'text_first' } });
    if (!loc.ok || !loc.box) throw new Error('实时画面上没找到“登录”，先确认演示页可见');
    const pad = 6;
    const widgetPhys = [loc.box[0] - pad, loc.box[1] - pad,
      loc.box[2] + pad * 2, loc.box[3] + pad * 2];
    log('实时定位“登录” → 盒', loc.box, '方法', loc.method, loc.level);
    const display = screen.getPrimaryDisplay();
    const origin = display.workArea, scale = display.scaleFactor || 1;
    const toDip = (r) => ({ x: (r[0] - origin.x) / scale, y: (r[1] - origin.y) / scale,
      w: r[2] / scale, h: r[3] / scale });
    const pageBox = toDip(w.rect), widgetBox = toDip(widgetPhys);
    log('scale =', scale, '页面框(DIP)', pageBox, '部件框(DIP)', widgetBox);

    const picked = pickTarget();
    if (overlay.webContents.isLoading()) {
      await new Promise((res) => overlay.webContents.once('did-finish-load', res));
    }
    await sleep(400);
    // 合成两次框选；覆盖层在完成时会被主进程关闭，故不 await 它的返回值
    overlay.webContents.executeJavaScript(
      `window.__autoPick(${JSON.stringify([pageBox, widgetBox])})`, true)
      .catch((e) => log('（覆盖层已关闭）', e.message));
    const pick = await withTimeout(picked, 45000, '选区未在 45s 内完成');
    if (!pick.ok) throw new Error('选区失败：' + (pick.error || String(pick.cancel)));
    log('已识别文字 =', JSON.stringify(pick.target.text),
      'rect_in_page =', pick.target.rect_in_page);
    log('页面 size =', pick.page.size, 'hwnd =', pick.hwnd,
      'OCR 其它文字 =', JSON.stringify(pick.ocr_others || []));
    // 定位复验：页面模板定位 → 部件定位（真实屏幕）
    const pf = await engine.call('page.find', { page: pick.page });
    log('page.find ok =', pf.ok, 'method =', pf.method, 'conf =', pf.confidence,
      'ms =', pf.elapsed_ms);
    const cx = widgetPhys[0] + Math.floor(widgetPhys[2] / 2);
    const cy = widgetPhys[1] + Math.floor(widgetPhys[3] / 2);
    const wl = await engine.call('widget.locate', { page_rect: pf.rect, target: pick.target });
    const gx = wl.center ? wl.center[0] : -9999, gy = wl.center ? wl.center[1] : -9999;
    const dev = Math.max(Math.abs(gx - cx), Math.abs(gy - cy));
    log('widget.locate(auto) ok =', wl.ok, 'level =', wl.level, 'method =', wl.method,
      'center =', wl.center, '偏差 =', dev, 'px', 'ms =', wl.elapsed_ms);
    const inside = Math.abs(gx - cx) <= widgetPhys[2] / 2 && Math.abs(gy - cy) <= widgetPhys[3] / 2;
    // 模板路径（image-first）：与框选几何应逐像素一致
    const wl2 = await engine.call('widget.locate',
      { page_rect: pf.rect, target: Object.assign({}, pick.target, { match: 'image' }) });
    const dev2 = wl2.center ? Math.max(Math.abs(wl2.center[0] - cx), Math.abs(wl2.center[1] - cy)) : 9999;
    log('widget.locate(image) ok =', wl2.ok, 'method =', wl2.method, 'center =', wl2.center,
      '偏差 =', dev2, 'px', 'ms =', wl2.elapsed_ms);
    const pass = wl.ok && inside && wl2.ok && dev2 <= 4 && (wl.elapsed_ms <= 500);
    log(pass ? 'AUTOTEST-PICK PASS' : `AUTOTEST-PICK FAIL: auto_in=${inside} dev2=${dev2}`);
  } catch (e) {
    log('AUTOTEST-PICK FAIL:', e.message);
  } finally {
    setTimeout(() => { try { engine.stop(); } catch (err) { /* ignore */ } app.quit(); }, 800);
  }
}

// ================================================================
// 验收演示（--autotest-demo）：模拟"零编程用户"用界面搭出自动登录
//   ① 框目标 → 选动作（输入文字/点一下）→ 加条件（如果看到"密码错误"）→ 提示我
//   ② 全程只用界面按钮与弹窗（合成鼠标框选 + 自动作答原生弹窗）
//   ③ 跑完整脚本，验证"故意输错密码 → 提示我"分支命中
// ================================================================

let demoEvents = [];

async function uiEval(js) { return win.webContents.executeJavaScript(js, true); }

async function uiClick(sel) {
  const ok = await uiEval(`(() => { const e = document.querySelector(${JSON.stringify(sel)});
    if (!e) return false; e.click(); return true; })()`);
  if (!ok) throw new Error('界面上找不到按钮：' + sel);
  await sleep(250);
}

async function uiAskText(value) {
  await sleep(200);
  const ok = await uiEval(`(() => { const i = document.getElementById('_askInput');
    if (!i) return false; i.value = ${JSON.stringify(value)};
    document.getElementById('_askOk').click(); return true; })()`);
  if (!ok) throw new Error('输入弹窗没出现');
  await sleep(250);
}

async function uiSelectContainer(match) {
  const ok = await uiEval(`(() => { const s = document.getElementById('insertInto');
    const o = [...s.options].find((x) => x.textContent.includes(${JSON.stringify(match)}));
    if (!o) return false; s.value = o.value;
    s.dispatchEvent(new Event('change')); return true; })()`);
  if (!ok) throw new Error('下拉里找不到：' + match);
  await sleep(200);
}

async function demoWaitRunDone(afterIdx, timeoutMs = 240000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const done = demoEvents.slice(afterIdx).filter((m) => m.method === 'event.run_done')[0];
    if (done) return done.params;
    await sleep(300);
  }
  throw new Error('运行没有在预期时间内结束');
}

// 用引擎的文字定位算出"要框住的部件"（物理像素）
async function demoWidgetBox(text, winRect) {
  const loc = await engine.call('widget.locate',
    { page_rect: winRect, target: { text, match: 'text_first' } });
  if (!loc.ok || !loc.box) throw new Error(`没在页面上找到“${text}”`);
  const [bx, by, bw, bh] = loc.box;
  const padX = 26, padY = 12;
  const px = Math.max(winRect[0], bx - padX);
  const py = Math.max(winRect[1], by - padY);
  const pw = Math.min(winRect[0] + winRect[2] - px, bw + padX * 2);
  const ph = Math.min(winRect[1] + winRect[3] - py, bh + padY * 2);
  return { phys: [px, py, pw, ph], text };
}

// 登录按钮：用 M0 资产里记录的框（按当前窗口尺寸等比缩放，已验证 dev≤1px）
function demoButtonBox(winRect) {
  const asset = JSON.parse(fs.readFileSync(path.join(ROOT, 'engine', 'tests', 'assets',
    'live', 'login.sgscript.json'), 'utf8'));
  const pg = asset.steps[0].target.page;
  const rp = asset.steps[0].target.rect_in_page;
  const sx = winRect[2] / pg.size[0], sy = winRect[3] / pg.size[1];
  return { phys: [Math.round(winRect[0] + rp[0] * sx), Math.round(winRect[1] + rp[1] * sy),
    Math.round(rp[2] * sx), Math.round(rp[3] * sy)], text: '登录（按已有目标）' };
}

// 走真实界面：点“截图目标” → 覆盖层里合成框选 → 回到界面
async function demoPickBox(display, winRect, physBox, label) {
  const origin = display.workArea, scale = display.scaleFactor || 1;
  const toDip = (r) => ({ x: (r[0] - origin.x) / scale, y: (r[1] - origin.y) / scale,
    w: r[2] / scale, h: r[3] / scale });
  demoState.boxes = [toDip(winRect), toDip(physBox)];
  const picked = new Promise((res) => { demoState.nextPick = res; });
  await uiClick('#btnPickTarget');
  const r = await withTimeout(picked, 60000, `框选“${label}”超时`);
  demoState.boxes = null;
  if (!r || !r.ok) throw new Error(`框选“${label}”失败：` + ((r && r.error) || ''));
  // 等界面把这次框选结果接住（pending 就绪）再加动作，否则会用到上一次的目标
  const want = r.target.text || '';
  const t0 = Date.now();
  for (;;) {
    const got = await uiEval('(state.pending && state.pending.target && '
      + 'state.pending.target.text) || ""');
    if (got === want) break;
    if (Date.now() - t0 > 8000) {
      throw new Error(`界面没接住这次框选：期望“${want}”，实际“${got}”`);
    }
    await sleep(120);
  }
  return r;
}

async function demoSteps() {
  const s = await uiEval(`(() => { const flat = [];
    const walk = (arr, d) => { for (const x of arr) {
      const c = x.condition || {}, lp = x.loop || {};
      flat.push({ d, t: x.type, a: x.action || '',
        tt: (x.target || {}).text || (c.target || {}).text || (lp.target || {}).text || '',
        msg: (x.params || {}).message || '' });
      if (x.type === 'condition') { walk(x.then || [], d + 1); walk(x.else || [], d + 1); }
      if (x.type === 'loop') walk(x.body || [], d + 1);
    } };
    walk(state.script.steps, 0); return JSON.stringify(flat); })()`);
  return JSON.parse(s);
}

// 界面文案术语检查（DoD 5：界面上不出现技术词）
const TERM_BLACKLIST = ['OCR', 'IPC', 'JSON', 'schema', 'target', 'widget', 'locate',
  'UIA', 'base64', '置信度', '模板匹配', '定位', '坐标', '选择器', '渲染', '进程',
  'hwnd', 'DPI', '像素', '正则', '线程'];

async function demoTermCheck(log) {
  const text = await uiEval('document.body.innerText');
  const low = text.toLowerCase();
  const hits = TERM_BLACKLIST.filter((w) => low.includes(w.toLowerCase()));
  log(hits.length ? `术语检查未通过：${hits.join('、')}`
    : `术语检查通过：界面可见文案共 ${text.replace(/\s+/g, '').length} 字，无技术词`);
  return hits;
}

// 走真实界面：保存 → 打开（原生对话框在演示模式下自动给临时路径）
async function demoSaveLoad(log) {
  await uiClick('#btnSave');
  const file = path.join(app.getPath('temp'), 'm1_demo_script.sgscript.json');
  const size = fs.existsSync(file) ? fs.statSync(file).size : 0;
  const before = JSON.parse(await uiEval('JSON.stringify({n: state.script.steps.length,'
    + ' name: state.script.name})'));
  if (!size) throw new Error('保存后没有生成脚本文件');
  // 清空界面再打开
  await uiEval('state.script = {version:"1.0", name:"未命名脚本", targets_rev:0, steps:[]};'
    + 'renderSteps(); true');
  await uiClick('#btnOpen');
  const after = JSON.parse(await uiEval(`JSON.stringify({n: state.script.steps.length,
    name: state.script.name, img: !!(state.script.steps[0] && state.script.steps[0].target
    && state.script.steps[0].target.image)})`));
  log(`保存/打开：${path.basename(file)} ${size} 字节；步骤 ${before.n} → ${after.n}，`
    + `目标图内嵌=${after.img}`);
  if (after.n !== before.n || !after.img) {
    throw new Error(`打开后脚本不一致：${JSON.stringify({ before, after })}`);
  }
  return { file, size, steps: after.n };
}

// 切到目标页面并检查没有被别的窗口压住（被压住时点按会点错地方）
async function demoActivate() {
  const r = await engine.call('window.activate', { title: FIXTURE_TITLE });
  if (!r.ok) throw new Error('没找到演示页面窗口（' + FIXTURE_TITLE + '）');
  if (r.occluded) {
    throw new Error(`演示页面上方压着别的窗口（“${r.top_title || '未命名'}”），`
      + '请先把它最小化或关掉再跑演示');
  }
  await sleep(300);
  return r.hwnd;
}

// 把登录页刷回初始状态（输入框清空、提示收起）——用户"再跑一遍"时也会这么做。
// 浏览器可能在 reload 后恢复表单值，所以每一步都要复核，必要时用键盘清空。
async function ensureCleanPage(log) {
  const winRect = async () => (await engine.call('window.find', { title: FIXTURE_TITLE }))
    .windows[0].rect;
  const hasUser = async () => (await engine.call('widget.locate',
    { page_rect: await winRect(), target: { text: '请输入工号', match: 'text_first' } })).ok;
  await demoActivate();
  await sleep(500);
  if (await hasUser()) return true;
  for (let i = 0; i < 2; i++) {                       // ① 刷新页面
    try { await engine.call('input.hotkey', { keys: 'f5' }); } catch (e) { /* ignore */ }
    await sleep(2200);
    if (await hasUser()) { log(`已把登录页刷回初始状态（F5 第 ${i + 1} 次）`); return true; }
  }
  for (const [label, dy] of [['用户名/工号', 36], ['密码', 36]]) {   // ② 手动清空
    const box = await demoWidgetBox(label, await winRect());
    const cx = Math.round(box.phys[0] + box.phys[2] / 2);
    const cy = Math.round(box.phys[1] + box.phys[3] / 2 + dy);
    await engine.call('input.click', { x: cx, y: cy });
    await sleep(200);
    await engine.call('input.hotkey', { keys: 'ctrl+a' });
    await sleep(120);
    await engine.call('input.hotkey', { keys: 'delete' });
    await sleep(200);
  }
  const ok = await hasUser();
  log(ok ? '已把登录页刷回初始状态（点进去全选删除）'
    : '登录页没能回到初始状态，照样往下跑');
  return ok;
}

async function runAutoDemo() {
  const log = (...a) => console.log('[demo]', ...a);
  const t0 = Date.now();
  const mark = () => ((Date.now() - t0) / 1000).toFixed(1) + 's';
  const display = screen.getPrimaryDisplay();
  let pass = false, failMsg = '';
  demoEvents = [];
  engine.onEvent = (msg) => {
    demoEvents.push(msg);
    if (msg.method === 'event.log') console.log('[demo][引擎]', msg.params.text);
    if (msg.method === 'event.calibrate') console.log('[demo][校准]', JSON.stringify(msg.params.note || msg.params));
    if (win && !win.isDestroyed()) win.webContents.send('engine-event', msg);
  };
  try {
    await sleep(1200);
    const w = await ensureFixtureWindow();
    log(`目标页面：${w.title}（物理 ${w.rect.join('×')}）`);
    if (!(await ensureCleanPage(log))) {
      log('提示：登录页不是初始状态，第一次框选可能失败');
    }
    await sleep(400);
    const winRect = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows[0].rect;

    // ---- 第 1 段：输入工号 → 输入密码（故意输错）→ 点一下登录
    const targets = [
      { text: '请输入工号', kind: 'type', value: 'demo' },
      { text: '请输入密码', kind: 'type', value: 'wrong-pass' },
      { box: demoButtonBox(winRect), kind: 'click' },
    ];
    for (const t of targets) {
      const box = t.box || await demoWidgetBox(t.text, winRect);
      const label = t.text || box.text;
      const pick = await demoPickBox(display, winRect, box.phys, label);
      log(`[${mark()}] 已框住“${label}” → 界面已识别：${JSON.stringify(pick.target.text)}`
        + `（框 ${box.phys[2]}×${box.phys[3]} 物理像素）`);
      if (t.kind === 'type') {
        await uiClick('[data-act="type"]');
        await uiAskText(t.value);
      } else {
        await uiClick('[data-act="click"]');
      }
    }
    log(`[${mark()}] 步骤：`, JSON.stringify(await demoSteps()));
    const idx1 = demoEvents.length;
    await uiClick('#btnRun');
    const r1 = await demoWaitRunDone(idx1);
    log(`[${mark()}] 第一次运行：${r1.status}（点击 ${r1.clicks} 次、输入 ${r1.types} 次）`);
    if (r1.status !== 'ok') {
      const w = await engine.call('window.find', { title: FIXTURE_TITLE }).catch(() => null);
      const a = await engine.call('window.activate', { title: FIXTURE_TITLE }).catch(() => null);
      log('诊断·窗口列表=', JSON.stringify(w && w.windows));
      log('诊断·激活结果=', JSON.stringify(a));
      throw new Error('第一次运行没有成功：' + r1.status);
    }

    // ---- 第 2 段：页面上已经出现“密码错误” → 框住它 → 如果看到 → 提示我
    await demoActivate();
    await sleep(700);
    const winRect2 = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows[0].rect;
    const box2 = await demoWidgetBox('密码错误', winRect2);
    const pick2 = await demoPickBox(display, winRect2, box2.phys, '密码错误');
    log(`[${mark()}] 已框住“${pick2.target.text}” → 用界面加条件分支`);
    await uiClick('#btnIfSee');
    await uiSelectContainer('就做');
    await uiClick('[data-act="notify"]');
    await uiAskText('密码输错了，请重新输入');
    log(`[${mark()}] 步骤：`, JSON.stringify(await demoSteps()));
    const terms = await demoTermCheck(log);
    await demoSaveLoad(log);

    // ---- 第 3 段：刷新登录页（清空输入）→ 整脚本重跑 → 命中“如果看到 密码错误 → 提示我”
    await ensureCleanPage(log);
    const idx2 = demoEvents.length;
    await uiClick('#btnRun');
    const r2 = await demoWaitRunDone(idx2);
    const newly = demoEvents.slice(idx2);
    // 运行后查页面上的关键文字：用来判断“输入有没有真的进输入框 / 红字出没出”
    try {
      const wr = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows[0];
      if (wr) {
        for (const t of ['密码错误', '请输入工号', '请输入密码']) {
          const q = await engine.call('widget.locate',
            { page_rect: wr.rect, target: { text: t, match: 'text_first' } });
          log(`诊断·页面上有“${t}”吗 → ${q.ok ? '有' : '没有'}`
            + (q.ok ? `（${q.method}/${q.level}）` : ''));
        }
      }
    } catch (e) { log('诊断失败：' + e.message); }
    const confirms = newly.filter((m) => m.method === 'event.confirm_request'
      && m.params.kind === 'notify');
    const calibs = demoEvents.filter((m) => m.method === 'event.calibrate');
    log(`[${mark()}] 最终运行：${r2.status}（点击 ${r2.clicks} 次、输入 ${r2.types} 次、提示 ${r2.notifies} 次）`);
    log(`[${mark()}] “提示我”弹窗 ${confirms.length} 次：`,
      JSON.stringify(confirms.map((c) => c.params.message)));
    log(`[${mark()}] 自动校准事件 ${calibs.length} 次（首次执行校验）`);
    log(`[${mark()}] 界面弹窗记录：`, JSON.stringify(demoState.confirms.map((c) => c.choice)));
    pass = r2.status === 'ok' && confirms.length >= 1 && demoState.confirms.length >= 1
      && terms.length === 0;
    if (!pass) failMsg = `status=${r2.status} notify=${confirms.length} `
      + `dialogs=${demoState.confirms.length} 术语=${terms.join('、') || 'ok'}`;
  } catch (e) {
    failMsg = e.message;
    log('DEMO FAIL:', e.message);
  }
  const secs = (Date.now() - t0) / 1000;
  log(`总用时 ${secs.toFixed(1)} 秒（验收口径：5 分钟内）`);
  log(pass && secs <= 300 ? 'AUTOTEST-DEMO PASS' : `AUTOTEST-DEMO FAIL: ${failMsg}`);
  setTimeout(() => { try { engine.stop(); } catch (e) { /* ignore */ } app.quit(); }, 900);
}

// ================================================================
// --autotest-ui：界面行为自测（M2-WP9 撤销/重做 + 快捷键）
//   全程用界面按钮与真实键盘事件驱动，断言步骤数与按钮状态
// ================================================================

async function runAutoUiTest() {
  const log = (...a) => console.log('[autotest-ui]', ...a);
  const fail = [];
  const check = (name, got, want) => {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    log(`${ok ? 'OK  ' : 'FAIL'} ${name}: got=${JSON.stringify(got)}`
      + ` want=${JSON.stringify(want)}`);
    if (!ok) fail.push(name);
  };
  const stepCount = () => uiEval('currentScript().steps.length');
  const undoDisabled = () => uiEval("document.getElementById('btnUndo').disabled");
  const redoDisabled = () => uiEval("document.getElementById('btnRedo').disabled");
  const pressCtrl = (key, shift) => uiEval(`(() => {
    const e = new KeyboardEvent('keydown', { key: ${JSON.stringify(key)}, ctrlKey: true,
      shiftKey: ${!!shift}, bubbles: true, cancelable: true });
    document.dispatchEvent(e); return e.defaultPrevented; })()`);
  const pressCtrlInInput = () => uiEval(`(() => {
    const inp = document.querySelector('input');
    const e = new KeyboardEvent('keydown', { key: 'z', ctrlKey: true,
      bubbles: true, cancelable: true });
    inp.dispatchEvent(e); return e.defaultPrevented; })()`);

  try {
    await sleep(1600);
    check('初始没有步骤', await stepCount(), 0);
    check('初始撤销不可用', await undoDisabled(), true);

    await uiClick('[data-act="stop"]');                  // 「停止」步不需要框目标，最省事
    await sleep(700);                                    // 越过历史合并窗口
    check('加一步后 1 个步骤', await stepCount(), 1);
    check('加一步后撤销可用', await undoDisabled(), false);

    await uiClick('[data-act="stop"]');
    await sleep(700);
    check('再加一步 → 2 个步骤', await stepCount(), 2);

    await uiClick('#btnUndo');
    check('点撤销 → 回到 1 个', await stepCount(), 1);
    check('撤销后重做可用', await redoDisabled(), false);

    await uiClick('#btnRedo');
    check('点重做 → 回到 2 个', await stepCount(), 2);

    check('Ctrl+Z 被拦截', await pressCtrl('z', false), true);
    check('Ctrl+Z 生效 → 1 个', await stepCount(), 1);
    check('Ctrl+Y 生效 → 2 个', await pressCtrl('y', false) && await stepCount(), 2);
    check('Ctrl+Z 生效 → 1 个', await pressCtrl('z', false) && await stepCount(), 1);
    check('Ctrl+Shift+Z 生效 → 2 个', await pressCtrl('z', true) && await stepCount(), 2);
    check('输入框里的 Ctrl+Z 不拦截（交给系统）', await pressCtrlInInput(), false);
    check('输入框里按 Ctrl+Z 不该改脚本', await stepCount(), 2);

    await uiClick('#btnUndo');
    check('撤销 → 1 个', await stepCount(), 1);
    await uiClick('#btnUndo');
    check('再撤销 → 0 个', await stepCount(), 0);
    check('没有可撤销时按钮禁用', await undoDisabled(), true);
    await sleep(200);
    await uiClick('#btnUndo');                           // 空撤回不应崩
    check('空撤销后仍是 0 个', await stepCount(), 0);

    // ---- 录制模式（M3-WP2）---------------------------------------------
    check('有「录制我的操作」入口', await uiEval(
      "!!document.getElementById('btnRecord')"), true);
    check('录制面板默认不显示', await uiEval(
      "document.getElementById('recPanel').hidden"), true);

    // 真的走一遍开始录制：点按钮 → IPC → 引擎挂全局钩子。
    // 这一条是"界面与引擎通不通"的实证，不是模拟。
    await uiClick('#btnRecord');
    await sleep(1200);
    const st = await uiEval("api.call('record.status', {}).then(r => r.result)");
    check('点「录制我的操作」后引擎真的在录', st && st.recording, true);
    check('开始录制后窗口自动最小化（别挡着用户操作）', win.isMinimized(), true);
    check('录制面板已显示', await uiEval(
      "document.getElementById('recPanel').hidden"), false);
    check('录制中按钮被禁用（避免重复开始）', await uiEval(
      "document.getElementById('btnRecord').disabled"), true);
    check('提示里给出了停止热键', await uiEval(
      "/ctrl\\+alt\\+q/i.test(document.getElementById('recHint').textContent)"), true);

    // 实时积木：喂给渲染层的 onEngineEvent —— 真机上的推送走的就是这个入口，
    // 所以这不是"另写一套"，而是把引擎推的那几条消息原样重放。
    await uiEval(`(() => {
      onEngineEvent({ method: 'event.record.block',
        params: { index: 0, action: 'click', text: '点一下', update: false } });
      onEngineEvent({ method: 'event.record.block',
        params: { index: 1, action: 'type', text: '输入文字「de」', update: false } });
      return true; })()`);
    await sleep(200);
    check('录制中实时显示已录到的动作', await uiEval(
      "Array.from(document.querySelectorAll('#recList li')).map(li => li.textContent)"),
      ['点一下', '输入文字「de」']);
    await uiEval(`(() => {
      onEngineEvent({ method: 'event.record.block',
        params: { index: 1, action: 'type', text: '输入文字「demo」', update: true } });
      return true; })()`);
    await sleep(200);
    check('同一块（打字还在继续）是更新而不是又加一条', await uiEval(
      "Array.from(document.querySelectorAll('#recList li')).map(li => li.textContent)"),
      ['点一下', '输入文字「demo」']);
    check('录制中不显示"还没录到动作"', await uiEval(
      "document.getElementById('recEmpty').hidden"), true);

    // ---- 桌面浮条 + 顶栏排版（用户试用反馈的两条）-------------------------
    check('录制中桌面出现了「停止录制」浮条', !!stopBar && !stopBar.isDestroyed(), true);
    check('浮条置顶显示', !!(stopBar && stopBar.isAlwaysOnTop()), true);
    check('浮条不能最大化/最小化（就是个按钮条）',
      !!(stopBar && !stopBar.isMaximizable() && !stopBar.isMinimizable()), true);
    const barHwnd = hwndOf(stopBar);
    const winHwnd = hwndOf(win);
    log(`浮条句柄=${barHwnd} 主窗口句柄=${winHwnd}`);
    check('浮条与主窗口各有独立句柄', barHwnd > 0 && barHwnd !== winHwnd, true);
    // 真验证：问引擎"你现在把哪几个窗口的操作排除在外了"，必须同时包含浮条和主窗口
    const sc = await uiEval("api.call('record.status', {}).then(r => r.result)");
    check('引擎确实在过滤「浮条 + 主窗口」（它们的点击不会进步骤）',
      (sc.self_hwnds || []).slice().sort().join(',') ===
      [barHwnd, winHwnd].slice().sort().join(','), true);

    // 顶栏排版：三种宽度下都不许折行、不许溢出（"窗口不全屏时文字换行"是用户报的问题）
    const topbarAt = async (w) => {
      win.setSize(w, 760);
      await sleep(420);
      return uiEval(`(() => {
        const bar = document.querySelector('.topbar');
        const span = document.querySelector('.brand-text span');
        const b = document.querySelector('.brand-text b');
        const wrappedSpans = [span, b].filter((el) => el && el.offsetParent
          && el.getClientRects().length > 1).length;
        return { wrapped: wrappedSpans, overflow: bar.scrollWidth > bar.clientWidth + 2 };
      })()`);
    };
    for (const w of [1240, 1060, 1040]) {
      const r = await topbarAt(w);
      check(`${w}px 宽时顶栏文字不折行`, r.wrapped, 0);
      check(`${w}px 宽时顶栏按钮不被挤出去`, r.overflow, false);
    }
    win.setSize(1240, 840);
    await sleep(300);

    await uiClick('#btnRecordCancel');
    await sleep(500);
    const st2 = await uiEval("api.call('record.status', {}).then(r => r.result)");
    check('点「放弃」后引擎不再录制', st2 && st2.recording, false);
    check('放弃录制后窗口自动恢复（用户不必去任务栏找）', win.isMinimized(), false);
    check('放弃后浮条自动关掉', !stopBar || stopBar.isDestroyed(), true);
    check('放弃后面板收起', await uiEval(
      "document.getElementById('recPanel').hidden"), true);
    check('放弃后没往脚本里加步骤', await stepCount(), 0);

    // 停止 → 生成步骤：用引擎真实返回的字段形状（steps/summary/notes）驱动界面
    await uiEval(`applyRecorded({ steps: [
        { id: 'r1', type: 'action', action: 'click', params: {},
          target: { text: '确定', rect_in_page: [1,2,3,4], center_in_page: [3,4] } },
        { id: 'r2', type: 'action', action: 'wait', params: { seconds: 2 } }],
      summary: { total: 2 }, notes: [] })`);
    await sleep(300);
    check('录制结果接进脚本成为步骤', await stepCount(), 2);
    check('录制后撤销可用（整批可一次撤回）', await undoDisabled(), false);
    await uiClick('#btnUndo');
    check('一次撤销撤掉整批录制的步骤', await stepCount(), 0);

    // ---- 浮条上的「停止录制」按钮必须真的能停（这是用户的主要停止方式）----
    await uiClick('#btnRecord');
    await sleep(1000);
    check('再次开始录制（准备用浮条停）',
      await uiEval("api.call('record.status', {}).then(r => r.result.recording)"), true);
    // 浮条的按钮在**另一个渲染进程**里，得从它自己的 webContents 点
    let clicked = false;
    try {
      if (stopBar && !stopBar.isDestroyed()) {
        clicked = await stopBar.webContents.executeJavaScript(
          "(() => { const b = document.getElementById('stop'); if (!b) return false;"
          + ' b.click(); return true; })()');
      }
    } catch (err) { clicked = false; }
    check('能点到浮条上的「停止录制」', !!clicked, true);
    await sleep(1800);
    check('点浮条后引擎确实停了',
      await uiEval("api.call('record.status', {}).then(r => r.result.recording)"), false);
    check('点浮条后浮条自动消失', !stopBar || stopBar.isDestroyed(), true);
    check('点浮条后主窗口恢复', win.isMinimized(), false);

    // ---- 运行模式也必须收起窗口（用户实测反馈：运行时没最小化 → 自己挡住目标画面 → 识别失败）
    await uiEval("(() => { const s = currentScript(); s.steps = []; return true; })()");
    await uiClick('[data-act="wait"]');
    await uiAskText('2');
    await sleep(400);
    check('已加一个「等一下」步骤（不需要框目标，正好用来测运行模式）',
      await stepCount(), 1);
    await uiClick('#btnRun');
    await sleep(900);
    log('运行中的界面日志：' + await uiEval(
      "Array.from(document.querySelectorAll('#log div')).slice(-4)"
      + ".map((d) => d.textContent).join(' ｜ ')"));
    log('运行状态：running=' + await uiEval('state.running'));
    check('运行中构建器窗口自动最小化（否则截图里拍到的是它自己）',
      win.isMinimized(), true);
    check('运行中桌面浮条出现（运行也能随时停）',
      !!stopBar && !stopBar.isDestroyed(), true);
    check('运行中浮条文案是「停止运行」', await (async () => {
      try {
        return await stopBar.webContents.executeJavaScript(
          "document.getElementById('stop').textContent");
      } catch (err) { return null; }
    })(), '停止运行');
    let waited = 0;
    while (waited < 15000 && (await uiEval('state.running'))) { await sleep(400); waited += 400; }
    check('运行结束（等到了 run_done）', await uiEval('state.running'), false);
    check('运行结束后构建器窗口自动恢复', win.isMinimized(), false);
    check('运行结束后浮条关掉', !stopBar || stopBar.isDestroyed(), true);

    // ---- 脚本编辑：复制 / 粘贴 / 拖拽排序（M4-WP6）--------------------------
    await uiEval("(() => { const s = currentScript(); s.steps = []; return true; })()");
    await uiClick('[data-act="stop"]');
    await sleep(700);
    await uiClick('[data-act="stop"]');
    await sleep(700);
    check('准备两步用来测复制', await stepCount(), 2);
    const ids0 = await uiEval('currentScript().steps.map((s) => s.id)');
    await uiEval(`(() => {
      const rows = document.querySelectorAll('#stepList > li');
      rows[0].querySelector('.ops button[title^="复制"]').click(); return true; })()`);
    await sleep(300);
    check('点「复制」后多了一步', await stepCount(), 3);
    const ids1 = await uiEval('currentScript().steps.map((s) => s.id)');
    check('复制出来的步骤有**新的 id**（否则脚本会因 id 重复而不合法）',
      new Set(ids1).size, 3);
    check('原来的两步还在原位', ids1.slice(0, 1).concat(ids1.slice(2)),
      [ids0[0], ids0[1]]);

    await uiEval(`(() => {
      const e = new KeyboardEvent('keydown', { key: 'v', ctrlKey: true,
        bubbles: true, cancelable: true });
      document.dispatchEvent(e); return true; })()`);
    await sleep(300);
    check('Ctrl+V 再贴一份', await stepCount(), 4);

    // 拖拽排序：把第 4 步拖到第 1 步上面
    const before = await uiEval(`currentScript().steps.map((s) => s.summary || s.id)`);
    await uiEval(`(() => {
      const rows = document.querySelectorAll('#stepList > li');
      const a = rows[3], b = rows[0];
      const dt = { effectAllowed: '', setData() {}, getData() { return ''; } };
      const mk = (type, el, y) => {
        const r = el.getBoundingClientRect();
        const ev = new Event(type, { bubbles: true, cancelable: true });
        ev.dataTransfer = dt;
        Object.defineProperty(ev, 'clientY', { value: y });
        Object.defineProperty(ev, 'clientX', { value: r.left + 10 });
        return ev;
      };
      a.dispatchEvent(mk('dragstart', a));
      const rb = b.getBoundingClientRect();
      b.dispatchEvent(mk('dragover', b, rb.top + 2));      // 上半 → 插到它前面
      b.dispatchEvent(mk('drop', b, rb.top + 2));
      a.dispatchEvent(mk('dragend', a));
      return true; })()`);
    await sleep(400);
    const after = await uiEval('currentScript().steps.length');
    check('拖拽后步骤数不变（只是换了顺序）', after, 4);
    const moved = await uiEval(`(() => {
      const ids = currentScript().steps.map((s) => s.id);
      return ids[0] !== ${JSON.stringify(ids1[0])} &&
             ids[1] !== ${JSON.stringify(ids1[1])}; })()`);
    check('被拖的那一步真的换了位置', await moved, true);
    await uiClick('#btnUndo');
    check('拖拽可以一次撤销回原顺序',
      await uiEval(`currentScript().steps.map((s) => s.id)[0]`), ids1[0]);
    void before;

    // ---- 导出给智能体：向导 + 干跑 + 写入（M4-WP3）--------------------------
    const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sg_agent_'));
    fs.mkdirSync(path.join(agentDir, 'tools'), { recursive: true });
    fs.mkdirSync(path.join(agentDir, 'skills'), { recursive: true });
    fs.writeFileSync(path.join(agentDir, 'tools', 'registry.py'), '# 别人的注册表\n');
    log('导出测试用的目标目录：' + agentDir);
    // 造一个带真实目标的脚本（有页面/文字/图像，导出才有 WIDGETS 可内嵌）
    await uiEval(`(() => {
      const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';
      const page = { image: png, size: [800, 600], context: { title: '示例窗口' },
                     anchors: [], scale_range: [0.8, 1.25] };
      const s = currentScript();
      s.name = '导出测试脚本';
      s.steps = [
        { id: 'e1', type: 'action', action: 'type', params: { text: 'admin' },
          target: { page: page, text: '用户名', image: png,
                    rect_in_page: [10, 20, 60, 24], center_in_page: [40, 32] } },
        { id: 'e2', type: 'action', action: 'click', params: {},
          target: { page: page, text: '登录', image: png,
                    rect_in_page: [10, 60, 60, 24], center_in_page: [40, 72] } }];
      renderSteps(); return true; })()`);
    await uiClick('#btnExport');
    await sleep(400);
    check('导出向导打开了', await uiEval(
      "!!document.querySelector('.modal.wide #_wzDir')"), true);
    await uiEval(`(() => {
      const i = document.querySelector('#_wzDir');
      i.value = ${JSON.stringify(agentDir)};
      i.dispatchEvent(new Event('input', { bubbles: true }));
      return true; })()`);
    await sleep(900);
    check('向导扫描后回显了"已有工具"', await uiEval(
      "/已有工具/.test(document.querySelector('#_wzScan').textContent)"), true);
    await uiEval("document.querySelector('#_wzOk').click(); true");
    await sleep(1200);
    const prev = await uiEval("(document.querySelector('.wz-prev') || {}).textContent || ''");
    check('干跑预览列出了要写的文件', /自动登录|导出测试脚本|新建/.test(prev), true);
    check('干跑阶段**真的没写文件**',
      !fs.existsSync(path.join(agentDir, 'tools', 'screen.py')), true);
    check('别人的 registry.py 默认不会被覆盖',
      fs.readFileSync(path.join(agentDir, 'tools', 'registry.py'), 'utf8')
        .includes('别人的注册表'), true);
    await uiEval("document.querySelector('#_wzOk').click(); true");
    await sleep(1500);
    check('确认后写入了产物',
      fs.existsSync(path.join(agentDir, 'tools', 'screen.py')), true);
    const skillFiles = fs.readdirSync(path.join(agentDir, 'skills'))
      .filter((f) => f.endsWith('.py') && f !== '__init__.py');
    log('产出的技能文件：' + (skillFiles.join('、') || '（无）'));
    check('产出了技能文件', skillFiles.length >= 1, true);
    if (skillFiles.length) {
      const body = fs.readFileSync(path.join(agentDir, 'skills', skillFiles[0]), 'utf8');
      check('产物标注了「单向导出」（DoD 第 3 条）', body.includes('单向导出'), true);
    }
    check('向导最后给了刷新提示', await uiEval(
      "/刷新|重启/.test(document.querySelector('#_wzBody').textContent)"), true);
    await uiEval("document.querySelector('#_wzOk').click(); true");
    await sleep(200);

    if (fail.length) {
      log(`AUTOTEST-UI FAIL: ${fail.join(', ')}`);
    } else {
      log('AUTOTEST-UI PASS');
    }
  } catch (e) {
    log('AUTOTEST-UI FAIL:', e.message);
    fail.push('exception');
  } finally {
    // 用 app.exit(code) 而不是 app.quit()：quit 会忽略 process.exitCode，
    // 外层脚本就没法靠退出码判断结果（stdout 在重定向时还可能整个丢掉）。
    setTimeout(() => {
      try { engine.stop(); } catch (err) { /* ignore */ }
      app.exit(fail.length ? 1 : 0);
    }, 800);
  }
}

// 自动冒烟：验证 UI↔引擎链路（启动 → ping → 加载资产 → 运行 → 事件 → 退出）
async function runAutoTest() {
  const log = (...a) => console.log('[autotest]', ...a);
  const events = [];
  engine.onEvent = async (msg) => {
    events.push(msg);
    if (win && !win.isDestroyed()) win.webContents.send('engine-event', msg);
    // 无人值守：人工确认请求自动按“停止”处理，避免冒烟卡在等待
    if (msg.method === 'event.confirm_request') {
      try {
        await engine.call('confirm.reply', {
          request_id: msg.params.request_id,
          choice: (msg.params.options || ['停止']).includes('停止') ? '停止'
            : (msg.params.options || ['继续'])[0],
        });
      } catch (e) { /* ignore */ }
    }
  };
  try {
    await new Promise((r) => setTimeout(r, 1500));
    const ping = await engine.call('ping');
    log('ping ok:', ping.ok, 'engine', ping.engine_version);
    const scriptPath = path.join(ROOT, 'engine', 'tests', 'assets', 'live', 'login.sgscript.json');
    const sg = await engine.call('script.load', { path: scriptPath });
    log('load ok: steps =', sg.script.steps.length,
      'text =', sg.script.steps[0].target.text);
    const run = await engine.call('script.run', { script: sg.script,
      options: { calibrate: false, loc_log: path.join(app.getPath('temp'), 'm1_ui_autotest_loc.jsonl') } });
    log('run started:', run.run_id);
    await new Promise((resolve) => {
      const t0 = Date.now();
      const timer = setInterval(() => {
        const done = events.find((m) => m.method === 'event.run_done');
        if (done || Date.now() - t0 > 90000) { clearInterval(timer); resolve(); }
      }, 200);
    });
    const done = events.find((m) => m.method === 'event.run_done');
    const stepRows = events.filter((m) => m.method === 'event.step');
    log('run_done:', done ? done.params.status : '(无)', 'steps:', stepRows.length);
    log('事件类型:', [...new Set(events.map((m) => m.method))].join(','));
    log('AUTOTEST DONE');
  } catch (e) {
    log('AUTOTEST FAIL:', e.message);
  } finally {
    setTimeout(() => { try { engine.stop(); } catch (err) { /* ignore */ } app.quit(); }, 800);
  }
}
