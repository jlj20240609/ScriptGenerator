// ScriptGenerator M1 主进程：引擎进程管理（stdio JSON-RPC）+ 主窗口 + 双截图选区覆盖层
// 契约见 docs/M1_IPC契约.md v0.1（renderer 只与主进程通信，主进程持有引擎子进程）
const { app, BrowserWindow, ipcMain, dialog, screen } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

const ROOT = path.join(__dirname, '..');           // 仓库根（引擎从这里以 python -m engine 启动）
const AUTOTEST = process.argv.includes('--autotest');
const AUTOTEST_PICK = process.argv.includes('--autotest-pick');
const FIXTURE_TITLE = 'M0 演示登录';

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

// ---------------------------------------------------------------- 窗口

let win = null;
let overlay = null;
let overlayPicker = null;      // {pageRect, step, origin, scale, resolve, reject}

function createMainWindow() {
  win = new BrowserWindow({
    width: 1160,
    height: 760,
    minWidth: 900,
    minHeight: 600,
    title: '脚本构建器',
    backgroundColor: '#f8fafc',
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  win.on('closed', () => { win = null; });
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
  overlay.on('closed', () => { overlay = null; });
  return overlay;
}

// 双截图选区：第一次框页面 → 第二次框部件 → 返回物理像素矩形
function pickTarget() {
  return new Promise((resolve, reject) => {
    const display = screen.getPrimaryDisplay();
    overlayPicker = {
      pageRect: null, step: 'page',
      origin: { x: display.workArea.x, y: display.workArea.y },
      scale: display.scaleFactor || 1,
      resolve, reject,
    };
    openOverlay();
    if (!overlay) { reject(new Error('无法打开选区层')); return; }
  });
}

function finishPick(result) {
  const p = overlayPicker;
  overlayPicker = null;
  if (overlay) { try { overlay.close(); } catch (e) { /* ignore */ } }
  if (!p) return;
  if (result && result.cancel) p.reject(new Error('已取消'));
  else p.resolve(result);
}

ipcMain.handle('overlay:selection', async (_e, payload) => {
  if (!overlayPicker) return { ok: false };
  const { origin, scale } = overlayPicker;
  const toPhysical = (r) => [
    Math.round((r.x + origin.x) * scale), Math.round((r.y + origin.y) * scale),
    Math.round(r.w * scale), Math.round(r.h * scale),
  ];
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

ipcMain.handle('engine:call', async (_e, { method, params }) => {
  try {
    const result = await engine.call(method, params || {});
    return { ok: true, result };
  } catch (err) {
    return { ok: false, error: { message: err.message, code: err.code, data: err.data } };
  }
});

ipcMain.handle('file:dialog', async (_e, { kind, defaultPath }) => {
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

ipcMain.handle('ui:confirm', async (_e, { message, options, defaultLabel }) => {
  const r = await dialog.showMessageBox(win, {
    type: 'info', message, buttons: options, defaultId: Math.max(0, options.indexOf(defaultLabel)),
    noLink: true, cancelId: -1,
  });
  return options[r.response] || defaultLabel || options[0];
});

// ---------------------------------------------------------------- 启动

app.whenReady().then(() => {
  engine.onEvent = (msg) => { if (win && !win.isDestroyed()) win.webContents.send('engine-event', msg); };
  engine.start();
  createMainWindow();
  if (AUTOTEST_PICK) runAutoPickTest();
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
  let wins = (await engine.call('window.find', { title: FIXTURE_TITLE })).windows;
  if (wins.length) return wins[0];
  const edge = findEdge();
  if (!edge) throw new Error('未找到 msedge.exe，无法拉起演示页面');
  const dst = path.join(app.getPath('temp'), 'web_login.html');
  fs.copyFileSync(path.join(ROOT, 'smoke', 'fixtures', 'web-login.html'), dst);
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
    // 依据登录资产几何算出“登录”按钮的物理框（按当前窗口尺寸等比缩放）
    const asset = JSON.parse(fs.readFileSync(path.join(ROOT, 'engine', 'tests', 'assets',
      'live', 'login.sgscript.json'), 'utf8'));
    const tgt = asset.steps[0].target, pg = tgt.page, rp = tgt.rect_in_page;
    const sx = w.rect[2] / pg.size[0], sy = w.rect[3] / pg.size[1];
    const widgetPhys = [Math.round(w.rect[0] + rp[0] * sx), Math.round(w.rect[1] + rp[1] * sy),
      Math.round(rp[2] * sx), Math.round(rp[3] * sy)];
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
