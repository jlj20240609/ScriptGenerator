// M0④ 原型主进程：全屏选区覆盖层 + Python 引擎 JSON-line IPC（可丢弃 Spike 产物）
const { app, BrowserWindow, ipcMain, screen } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

const ENGINE_PY = path.join(__dirname, '..', 'm0_ipc_engine.py');
// 默认用官方 onnxruntime 的 venv（更快）；缺失时退回系统 python
let pythonBin = process.env.M0_PYTHON || '';
const venvCandidates = [
  path.join(__dirname, '..', '.venv-ortcpu', 'Scripts', 'python.exe'),
  path.join(__dirname, '..', '.venv-ortcpu', 'Scripts', 'python'),
];
if (!pythonBin) {
  pythonBin = venvCandidates.find((p) => fs.existsSync(p)) || 'python';
}

let win = null;
let engine = null;
const pending = new Map();
let nextId = 1;

function engineStart() {
  engine = spawn(pythonBin, [ENGINE_PY], {
    stdio: ['pipe', 'pipe', 'inherit'],
    windowsHide: false,
  });
  const rl = readline.createInterface({ input: engine.stdout });
  rl.on('line', (line) => {
    try {
      const msg = JSON.parse(line);
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        p(msg);
      }
    } catch (e) {
      console.error('[engine-msg]', e.message, line.slice(0, 200));
    }
  });
  engine.on('exit', (code) => {
    console.log('[engine] exited', code);
    engine = null;
  });
  return engine;
}

function rpc(method, params = {}, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    if (!engine) engineStart();
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('rpc timeout: ' + method));
    }, timeoutMs);
    pending.set(id, (msg) => {
      clearTimeout(timer);
      if (msg.error) reject(new Error(msg.error));
      else resolve(msg.result || {});
    });
    engine.stdin.write(JSON.stringify({ id, method, params }) + '\n');
  });
}

function createOverlay() {
  const { workArea } = screen.getPrimaryDisplay();
  win = new BrowserWindow({
    x: workArea.x,
    y: workArea.y,
    width: workArea.width,
    height: workArea.height,
    transparent: true,
    frame: false,
    resizable: false,
    movable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.loadFile('overlay.html');
  win.on('closed', () => (win = null));
}

app.whenReady().then(async () => {
  engineStart();
  createOverlay();
  console.log('[main] overlay ready; scaleFactor=',
    screen.getPrimaryDisplay().scaleFactor,
    'engine=', pythonBin);
  if (process.env.M0_AUTOTEST) {
    // 自动验证：渲染层定位 → 主进程打印结果 → 自动退出（验证坐标链路与 DIP 换算）
    win.webContents.on('console-message', (_e, level, msg) => {
      if (String(msg).includes('AUTOTEST') || level >= 2) console.log('[renderer:' + level + ']', msg);
    });
    setTimeout(async () => {
      try {
        await win.webContents.executeJavaScript('window.__runAuto()');
      } catch (e) {
        console.log('[main] autotest failed:', e.message);
      }
    }, 2500);
    setTimeout(() => app.quit(), 30000);
  }
});

app.on('window-all-closed', () => {
  if (engine) engine.kill();
  app.quit();
});

// 渲染层请求 RPC
ipcMain.handle('rpc', async (_e, req) => {
  const t0 = Date.now();
  const result = await rpc(req.method, req.params || {});
  return { ...result, rpc_ms: Date.now() - t0 };
});

// 窗口矩形（DIP 逻辑坐标）转物理像素请求引擎抓屏定位
ipcMain.handle('locate-text', async (_e, { text, region }) => {
  const res = await rpc('screen.find_text', { text, region });
  return res;
});

ipcMain.on('quit', () => app.quit());
