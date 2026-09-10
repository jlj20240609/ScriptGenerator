// ScriptGenerator M1 渲染层：列表式编辑器 + 运行日志（白话文案，术语表左列）
/* global api */

const state = {
  script: { version: '1.0', name: '未命名脚本', targets_rev: 0, steps: [] },
  pending: null,            // {target, page} —— 刚“截图目标”的结果
  insertPath: [],           // 新步骤加入位置（[] = 主流程；['<stepId>','then'] 等）
  running: false,
  runId: null,
};

const $ = (id) => document.getElementById(id);
const uid = (() => { let n = 0; return (p) => `${p}${++n}_${Math.random().toString(36).slice(2, 6)}`; })();

// ---------------------------------------------------------------- 小工具（白话提示）

function log(text, cls = '') {
  const box = $('log');
  const div = document.createElement('div');
  div.className = cls;
  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = new Date().toTimeString().slice(0, 8);
  div.appendChild(ts);
  div.appendChild(document.createTextNode(text));
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setState(text, cls = '') {
  $('state').textContent = text;
  $('state').className = 'pill ' + cls;
}

function askText(title, defaultValue = '') {
  return new Promise((resolve) => {
    const wrap = document.createElement('div');
    wrap.className = 'modal-mask';
    wrap.innerHTML = `<div class="modal">
      <div class="modal-title">${title}</div>
      <input id="_askInput" class="name" value="${defaultValue.replace(/"/g, '&quot;')}">
      <div class="modal-ops">
        <button id="_askCancel" class="btn ghost">取消</button>
        <button id="_askOk" class="btn primary">确定</button>
      </div></div>`;
    document.body.appendChild(wrap);
    const input = wrap.querySelector('#_askInput');
    input.focus(); input.select();
    const done = (v) => { wrap.remove(); resolve(v); };
    wrap.querySelector('#_askOk').onclick = () => done(input.value.trim());
    wrap.querySelector('#_askCancel').onclick = () => done(null);
    input.onkeydown = (e) => {
      if (e.key === 'Enter') done(input.value.trim());
      if (e.key === 'Escape') done(null);
    };
  });
}

async function askChoice(title, options) {
  return api.confirm(title, options, options[0]);
}

// ---------------------------------------------------------------- 步骤摘要（白话）

const ACT_LABEL = { click: '点一下', dblclick: '点两下', type: '输入文字',
  wait: '等一下', notify: '提示我', hotkey: '按快捷键', stop: '停止' };

function stepSummary(step) {
  if (step.type === 'action') {
    const t = step.target || {};
    const name = t.text ? `“${t.text}”` : '这个部件';
    if (step.action === 'click') return `点一下 ${name}`;
    if (step.action === 'dblclick') return `点两下 ${name}`;
    if (step.action === 'type') return `在 ${name} 输入文字 “${(step.params || {}).text || ''}”`;
    if (step.action === 'wait') return `等一下 ${(step.params || {}).seconds || 1} 秒`;
    if (step.action === 'notify') return `提示我 “${(step.params || {}).message || ''}”`;
    if (step.action === 'hotkey') return `按快捷键 ${(step.params || {}).keys || ''}`;
    if (step.action === 'stop') return '停止（脚本到这里就结束）';
    return ACT_LABEL[step.action] || step.action;
  }
  if (step.type === 'condition') {
    const t = (step.condition || {}).target || {};
    const see = (step.condition || {}).exists === false ? '如果没看到' : '如果看到';
    return `${see} “${t.text || '部件'}”`;
  }
  if (step.type === 'loop') {
    const lp = step.loop || {};
    if (lp.mode === 'count') return `重复 ${lp.count || 0} 次`;
    if (lp.mode === 'until') return `一直重复，直到看到 “${(lp.target || {}).text || '部件'}”`;
    return '一直重复（手动停止）';
  }
  return step.type;
}

function stepExtra(step) {
  if (step.expected_outcome) {
    const eo = step.expected_outcome;
    const t = (eo.target || {}).text || '预期结果';
    const of = (eo.on_fail || {}).strategy || 'notify';
    const word = { retry: '再试一次', notify: '提示我', stop: '停止', skip: '跳过' }[of];
    return `做完后应该看到 “${t}”（没看到就${word}）`;
  }
  return '';
}

// ---------------------------------------------------------------- 容器解析（主流程/分支/循环体）

function containerAt(path) {
  let arr = state.script.steps;
  let node = null;
  for (let i = 0; i < path.length; i += 2) {
    const id = path[i];
    const which = path[i + 1];
    node = arr.find((s) => s.id === id);
    if (!node) return null;
    if (which === 'then' || which === 'else') arr = node[which] || (node[which] = []);
    else if (which === 'body') arr = node.body || (node.body = []);
    else return null;
  }
  return arr;
}

function containerOptions() {
  const out = [{ path: [], label: '主流程' }];
  const walk = (steps, path, depth) => {
    for (const s of steps) {
      const pad = '&nbsp;'.repeat(depth * 3);
      if (s.type === 'condition') {
        for (const [k, label] of [['then', '就做'], ['else', '否则']]) {
          const p = [...path, s.id, k];
          out.push({ path: p, label: `${pad}${stepSummary(s)} → ${label}里面` });
          walk(s[k] || [], p, depth + 1);
        }
      } else if (s.type === 'loop') {
        const p = [...path, s.id, 'body'];
        out.push({ path: p, label: `${pad}${stepSummary(s)} → 循环体里面` });
        walk(s.body || [], p, depth + 1);
      }
    }
  };
  walk(state.script.steps, [], 0);
  return out;
}

function renderInsertSelector() {
  const sel = $('insertInto');
  const opts = containerOptions();
  const prev = JSON.stringify(state.insertPath);
  sel.innerHTML = '';
  for (const o of opts) {
    const el = document.createElement('option');
    el.value = JSON.stringify(o.path);
    el.textContent = o.label.replace(/&nbsp;/g, '\u00a0');
    sel.appendChild(el);
  }
  const match = opts.find((o) => JSON.stringify(o.path) === prev);
  sel.value = JSON.stringify(match ? match.path : []);
  state.insertPath = JSON.parse(sel.value);
}

// ---------------------------------------------------------------- 渲染步骤列表

const ICON = {
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 5v14M6 13l6 6 6-6"/></svg>',
  check: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M8.6 12.4l2.5 2.5 4.4-5"/></svg>',
  del: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/></svg>',
};

function iconButton(icon, title, fn, cls = '') {
  const b = document.createElement('button');
  b.innerHTML = ICON[icon];
  b.title = title;
  b.setAttribute('aria-label', title);
  if (cls) b.className = cls;
  b.onclick = fn;
  return b;
}

function renderSteps() {
  const ol = $('stepList');
  ol.innerHTML = '';
  const walk = (steps, depth) => {
    for (const s of steps) {
      const li = document.createElement('li');
      li.dataset.type = s.type;
      if (depth > 0) { li.classList.add('nested'); li.style.marginLeft = (depth * 16) + 'px'; }
      const idx = document.createElement('span');
      idx.className = 'idx';
      idx.textContent = String(ol.children.length + 1);
      const body = document.createElement('div');
      body.className = 'body';
      const l1 = document.createElement('div');
      l1.className = 'l1'; l1.textContent = stepSummary(s);
      const l2 = document.createElement('div');
      l2.className = 'l2';
      const extra = stepExtra(s);
      if (s.target && s.target.image) {
        const img = document.createElement('img');
        img.src = s.target.image; img.alt = '部件';
        l2.appendChild(img);
      }
      l2.appendChild(document.createTextNode(extra || (s.target && s.target.text ? '已识别：' + s.target.text : '')));
      body.appendChild(l1);
      if (s.type === 'condition') {                       // 分支块：给个“里面有多少步”的标签
        const tag = document.createElement('span');
        tag.className = 'block-tag';
        tag.textContent = `就做 ${countSteps(s.then || [])} 步 · 否则 ${countSteps(s.else || [])} 步`;
        body.appendChild(tag);
      }
      if (s.type === 'loop') {
        const tag = document.createElement('span');
        tag.className = 'block-tag';
        tag.textContent = `循环体 ${countSteps(s.body || [])} 步`;
        body.appendChild(tag);
      }
      body.appendChild(l2);
      const ops = document.createElement('div');
      ops.className = 'ops';
      ops.append(
        iconButton('up', '往上挪一位', () => moveStep(s, -1)),
        iconButton('down', '往下挪一位', () => moveStep(s, +1)),
        iconButton('check', '加“做完后应该看到”', () => addVerify(s)),
        iconButton('del', '删除这一步', () => removeStep(s), 'del'),
      );
      li.append(idx, body, ops);
      ol.appendChild(li);
      if (s.type === 'condition') { walk(s.then || [], depth + 1); walk(s.else || [], depth + 1); }
      if (s.type === 'loop') walk(s.body || [], depth + 1);
    }
  };
  walk(state.script.steps, 0);
  $('stepCount').textContent = `${countSteps(state.script.steps)} 步`;
  $('emptyHint').style.display = state.script.steps.length ? 'none' : 'flex';
  renderInsertSelector();
}

function countSteps(steps) {
  let n = 0;
  for (const s of steps) {
    n += 1;
    if (s.type === 'condition') n += countSteps(s.then || []) + countSteps(s.else || []);
    if (s.type === 'loop') n += countSteps(s.body || []);
  }
  return n;
}

function findParentArr(step) {
  const search = (arr) => {
    if (arr.includes(step)) return arr;
    for (const s of arr) {
      if (s.type === 'condition') {
        const r = search(s.then || []) || search(s.else || []);
        if (r) return r;
      }
      if (s.type === 'loop') {
        const r = search(s.body || []);
        if (r) return r;
      }
    }
    return null;
  };
  return search(state.script.steps);
}

// ---------------------------------------------------------------- 撤销/重做（M2-WP9）

// 每次改动步骤前拍一张快照。合并窗口是为了把"一次用户操作"里内部的多次 addStep
// （例如建一个条件：容器 + 分支步骤）并成一格撤销，而不是要按好几次。
const editHistory = { past: [], future: [], limit: 60 };
let lastSnapAt = 0;

function snapshot(label, mergeMs = 500) {
  const now = Date.now();
  if (editHistory.past.length && now - lastSnapAt < mergeMs) {
    lastSnapAt = now;
    return;
  }
  editHistory.past.push({ script: JSON.parse(JSON.stringify(state.script)), label });
  if (editHistory.past.length > editHistory.limit) editHistory.past.shift();
  editHistory.future.length = 0;
  lastSnapAt = now;
  renderUndoButtons();
}

function applySnapshot(snap) {
  state.script = JSON.parse(JSON.stringify(snap.script));
  $('scriptName').value = state.script.name || '未命名脚本';
  renderInsertSelector();
  renderSteps();
  renderUndoButtons();
}

function undo() {
  if (!editHistory.past.length) { log('没有可以撤销的改动了', 'warn'); return; }
  const snap = editHistory.past.pop();
  editHistory.future.push({ script: JSON.parse(JSON.stringify(state.script)), label: '当前' });
  applySnapshot(snap);
  log('已撤销' + (snap.label ? '：' + snap.label : ''), '');
}

function redo() {
  if (!editHistory.future.length) { log('没有可以重做的改动了', 'warn'); return; }
  const snap = editHistory.future.pop();
  editHistory.past.push({ script: JSON.parse(JSON.stringify(state.script)), label: '撤销的那一步' });
  applySnapshot(snap);
  log('已重做', '');
}

function renderUndoButtons() {
  const u = $('btnUndo'), r = $('btnRedo');
  if (u) u.disabled = !editHistory.past.length;
  if (r) r.disabled = !editHistory.future.length;
}

function currentScript() { return state.script; }        // 自测用：读当前脚本

function moveStep(step, delta) {
  const arr = findParentArr(step);
  if (!arr) return;
  const i = arr.indexOf(step);
  const j = i + delta;
  if (j < 0 || j >= arr.length) return;
  snapshot(delta < 0 ? '上移一步' : '下移一步');
  arr.splice(i, 1);
  arr.splice(j, 0, step);
  renderSteps();
}

function removeStep(step) {
  const arr = findParentArr(step);
  if (!arr) return;
  snapshot('删掉一步');
  arr.splice(arr.indexOf(step), 1);
  renderSteps();
}

// ---------------------------------------------------------------- 添加步骤

function addStep(step) {
  const arr = containerAt(state.insertPath);
  snapshot('加一步');
  (arr || state.script.steps).push(step);
  renderSteps();
  return step;
}

function requirePending(hint) {
  if (state.pending) return state.pending;
  log(hint || '请先点“截图目标”：先框住页面，再框住要操作的位置。', 'warn');
  return null;
}

async function onAction(act) {
  if (act === 'stop') {                     // L3：看到某提示就停（不需要框目标）
    addStep({ id: uid('s'), type: 'action', action: 'stop', params: {} });
    return;
  }
  if (act === 'wait') {
    const v = await askText('等一下多久？（秒）', '1');
    if (v === null) return;
    const secs = Number(v) || 1;
    addStep({ id: uid('s'), type: 'action', action: 'wait', params: { seconds: secs } });
    return;
  }
  if (act === 'notify') {
    const msg = await askText('要提醒你什么？', '请检查网络后点继续');
    if (msg === null || msg === '') return;
    addStep({ id: uid('s'), type: 'action', action: 'notify', params: { message: msg } });
    return;
  }
  if (act === 'hotkey') {
    const keys = await askText('按哪个快捷键？（例如 ctrl+s）', 'ctrl+s');
    if (keys === null || keys === '') return;
    addStep({ id: uid('s'), type: 'action', action: 'hotkey', params: { keys } });
    return;
  }
  const p = requirePending();
  if (!p) return;
  const target = Object.assign({}, p.target, { page: p.page });
  if (act === 'type') {
    const text = await askText('要输入什么文字？', '');
    if (text === null || text === '') return;
    addStep({ id: uid('s'), type: 'action', action: 'type', target, params: { text } });
    return;
  }
  addStep({ id: uid('s'), type: 'action', action: act, target, params: {} });
}

async function addVerify(step) {
  const p = requirePending('请先点“截图目标”框出“做完后应该看到”的那个东西。');
  if (!p) return;
  const strategy = await askChoice('做完后没看到，怎么办？',
    ['再试一次', '提示我', '停止', '跳过']);
  const map = { 再试一次: 'retry', 提示我: 'notify', 停止: 'stop', 跳过: 'skip' };
  step.expected_outcome = {
    target: Object.assign({}, p.target, { page: p.page }),
    on_fail: { strategy: map[strategy] || 'notify' },
  };
  if (strategy === '提示我') {
    step.expected_outcome.on_fail.message = '做完后没看到预期结果';
  }
  renderSteps();
}

async function onIf(see) {
  const p = requirePending('请先点“截图目标”框出要判断的东西。');
  if (!p) return;
  const step = { id: uid('c'), type: 'condition',
    condition: { target: Object.assign({}, p.target, { page: p.page }), exists: !!see },
    then: [], else: [] };
  addStep(step);
}

function onLoopN() {
  return (async () => {
    const v = await askText('重复几次？', '3');
    if (v === null) return;
    const n = Math.max(0, parseInt(v, 10) || 0);
    addStep({ id: uid('l'), type: 'loop', loop: { mode: 'count', count: n }, body: [] });
  })();
}

function onLoopUntil() {
  const p = requirePending('请先点“截图目标”框出“一直重复直到看到”的那个东西。');
  if (!p) return;
  addStep({ id: uid('l'), type: 'loop',
    loop: { mode: 'until', target: Object.assign({}, p.target, { page: p.page }), exists: true },
    body: [] });
}

function onLoopForever() {
  addStep({ id: uid('l'), type: 'loop', loop: { mode: 'forever' }, body: [] });
}

// ---------------------------------------------------------------- 截图目标（双截图）

async function onPickTarget() {
  setState('请在弹出的窗口里框选…');
  const r = await api.pickTarget();
  if (!r || !r.ok) {
    setState(r && r.error && r.error.indexOf('取消') < 0 ? '选区未完成' : '准备就绪');
    if (r && r.error && r.error.indexOf('取消') < 0) log('框选没有完成：' + r.error, 'warn');
    return;
  }
  state.pending = { target: r.target, page: r.page };
  renderPending();
  setState('准备就绪');
  log(`已识别：${r.target.text || '（这个位置没有文字，按形状记住）'}`, 'ok');
}

function renderPending() {
  const box = $('pending');
  if (!state.pending) {
    box.innerHTML = '（还没有：点“截图目标”，先框页面，再框部件）';
    return;
  }
  const t = state.pending.target;
  box.innerHTML = '';
  const line = document.createElement('div');
  line.innerHTML = `<b>已识别：${t.text || '（无文字，按形状记住）'}</b>`;
  const img = document.createElement('img');
  img.className = 'thumb'; img.src = t.image; img.alt = '部件缩略图';
  const tip = document.createElement('div');
  tip.textContent = '点上面的动作按钮，把它变成一个步骤。';
  box.append(line, img, tip);
}

// ---------------------------------------------------------------- 运行与事件

async function onRun() {
  if (!state.script.steps.length) { log('先加一个步骤再运行。', 'warn'); return; }
  state.script.name = $('scriptName').value.trim() || '未命名脚本';
  const r = await api.call('script.run', {
    script: state.script,
    options: { calibrate: true, ai: 'stub', first_run_calibrate: true, guard: true },
  });
  if (!r.ok) { log('运行没有开始：' + r.error.message, 'err'); return; }
  state.runId = r.result.run_id;
  state.running = true;
  $('btnRun').disabled = true; $('btnStop').disabled = false;
  setState('正在运行', 'ok');
  log('开始运行脚本');
}

async function onStop() {
  await api.call('script.stop', {});
  log('已请求停止', 'warn');
}

function onEngineEvent(msg) {
  const p = msg.params || {};
  if (msg.method === 'event.log') {
    log(p.text, p.level === 'warn' ? 'warn' : '');
    return;
  }
  if (msg.method === 'event.calibrate') {
    const word = p.updated ? '自动完成了重新识别（已更新脚本里的位置）' : '自动识别：' + (p.note || '');
    log(word, 'calib');
    return;
  }
  if (msg.method === 'event.step') {
    const mark = { ok: '✓', fail: '✗', skipped: '跳过', stopped: '已停止', iter: '…' }[p.status] || '';
    if (p.label) log(`${mark} ${p.label}`, p.status === 'fail' ? 'err' : (p.status === 'ok' ? 'ok' : ''));
    return;
  }
  if (msg.method === 'event.run_state') {
    if (p.state === 'running') { $('logState').textContent = '运行中'; }
    return;
  }
  if (msg.method === 'event.run_done') {
    state.running = false;
    $('btnRun').disabled = false; $('btnStop').disabled = true;
    $('logState').textContent = '';
    const word = { ok: '运行完成', failed: '运行失败', stopped: '已停止' }[p.status] || p.status;
    setState(word, p.status === 'ok' ? 'ok' : '');
    log(`${word}：共 ${p.steps} 行，点击 ${p.clicks} 次，输入 ${p.types} 次`, p.status === 'ok' ? 'ok' : 'warn');
    return;
  }
  if (msg.method === 'event.confirm_request') {
    handleConfirm(p);
  }
}

async function handleConfirm(p) {
  // kind 决定弹窗的标题与图标语气（notify / not_found / outcome_fail / ai_authorize）：
  // "请你处理一下"、"这一步没找到"、"做完后没看到预期的结果"、"需要你同意"——四种场景语气不同，
  // 用同一句"确认"会让人分不清是在问什么（M2-WP9）。
  const choice = await api.confirm(p.message, p.options, p.default, p.kind);
  await api.call('confirm.reply', { request_id: p.request_id, choice });
  log(`你选择了：${choice}`);
}

// ---------------------------------------------------------------- 保存/打开

async function onSave() {
  const p = await api.fileDialog('save', `${$('scriptName').value || 'script'}.sgscript.json`);
  if (!p) return;
  state.script.name = $('scriptName').value.trim() || '未命名脚本';
  const r = await api.call('script.save', { path: p, script: state.script });
  if (!r.ok) { log('保存失败：' + r.error.message, 'err'); return; }
  log('已保存：' + p, 'ok');
}

async function onOpen() {
  const p = await api.fileDialog('open');
  if (!p) return;
  const r = await api.call('script.load', { path: p });
  if (!r.ok) { log('打开失败：' + r.error.message, 'err'); return; }
  state.script = r.result.script;
  $('scriptName').value = state.script.name || '未命名脚本';
  renderSteps();
  log('已打开：' + p, 'ok');
}

// ---------------------------------------------------------------- 绑定

window.addEventListener('DOMContentLoaded', () => {
  $('btnPickTarget').onclick = onPickTarget;
  // 新步骤加到哪儿（主流程 / 就做里面 / 否则里面 / 循环体里面）
  $('insertInto').onchange = () => {
    try { state.insertPath = JSON.parse($('insertInto').value); } catch (e) { /* ignore */ }
  };
  document.querySelectorAll('[data-act]').forEach((b) => {
    b.onclick = () => onAction(b.dataset.act);
  });
  $('btnIfSee').onclick = () => onIf(true);
  $('btnIfNotSee').onclick = () => onIf(false);
  $('btnLoopN').onclick = onLoopN;
  $('btnLoopUntil').onclick = onLoopUntil;
  $('btnLoopForever').onclick = onLoopForever;
  $('btnRun').onclick = onRun;
  $('btnStop').onclick = onStop;
  $('btnSave').onclick = onSave;
  $('btnOpen').onclick = onOpen;
  if ($('btnUndo')) $('btnUndo').onclick = undo;
  if ($('btnRedo')) $('btnRedo').onclick = redo;
  $('btnClearLog').onclick = () => { $('log').innerHTML = ''; };
  // 快捷键：Ctrl+S 保存、Ctrl+Z 撤销、Ctrl+Y（或 Ctrl+Shift+Z）重做。
  // 输入框里的撤销/重做交回系统原生（不然打字时按 Ctrl+Z 会去撤销"加步骤"，很怪）。
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = String(e.key || '').toLowerCase();
    if (k === 's') { e.preventDefault(); onSave(); return; }
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA'
      || (e.target && e.target.isContentEditable);
    if (typing) return;
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); return; }
    if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); redo(); }
  });
  api.onEvent(onEngineEvent);
  renderPending();
  renderSteps();
  renderUndoButtons();
  log('准备就绪：点“截图目标”开始（先框页面，再框部件）。');
  log('快捷键：Ctrl+S 保存 · Ctrl+Z 撤销 · Ctrl+Y 重做');
});
