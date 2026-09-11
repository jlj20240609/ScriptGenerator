// ScriptGenerator M1 渲染层：列表式编辑器 + 运行日志（白话文案，术语表左列）
/* global api */

const state = {
  script: { version: '1.0', name: '未命名脚本', targets_rev: 0, steps: [] },
  pending: null,            // {target, page} —— 刚“截图目标”的结果
  insertPath: [],           // 新步骤加入位置（[] = 主流程；['<stepId>','then'] 等）
  running: false,
  runId: null,
  clip: null,               // 复制过的步骤（Ctrl+V 粘贴用）
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
  copy: '<svg viewBox="0 0 24 24"><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/></svg>',
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
        iconButton('copy', '复制这一步（也可以拖它换位置）', () => copyStep(s)),
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
  enableDragSort();
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
    api.runMode(false);                       // 运行结束 → 把构建器窗口恢复回来
    const word = { ok: '运行完成', failed: '运行失败', stopped: '已停止' }[p.status] || p.status;
    setState(word, p.status === 'ok' ? 'ok' : '');
    log(`${word}：共 ${p.steps} 行，点击 ${p.clicks} 次，输入 ${p.types} 次`, p.status === 'ok' ? 'ok' : 'warn');
    const cl = p.cloud || {};
    if (cl.calls) {
      log(`本次运行调用了云端 ${cl.calls} 次（${cl.total_tokens} tokens，`
        + `平均 ${Math.round(cl.ms_avg || 0)}ms）`);
    }
    return;
  }
  if (msg.method === 'event.confirm_request') {
    handleConfirm(p);
    return;
  }
  // ---- 录制（M3-WP2）：钩子在引擎侧跑，界面靠这几个事件跟上进度
  if (msg.method === 'event.record.started') {
    if (p.hotkey) {
      rec.hotkey = String(p.hotkey).replace(/ctrl/i, 'Ctrl').replace(/alt/i, 'Alt')
        .replace(/shift/i, 'Shift');
      $('recHint').textContent = `做完了按 ${rec.hotkey} 停止（本应用里的操作不会计入步骤）`;
    }
    log(`停止热键是 ${p.hotkey || '界面上的停止按钮'}——录制中它不会被你录进去。`);
    return;
  }
  if (msg.method === 'event.record.block') {
    recUpsert(p);
    recTick();
    return;
  }
  if (msg.method === 'event.record.done') {
    // 用户按了停止热键（他人在别的窗口，不会回来点按钮）→ 界面自己收尾
    if (rec.on) {
      recSetOn(false);
      setState('正在认你点到的是什么…');
      api.call('record.stop', {}).then((r) => {
        if (r.ok) applyRecorded(r.result || {});
        else log('停止失败：' + (r.error ? r.error.message : '（未知原因）'), 'err');
      });
    }
    return;
  }
  if (msg.method === 'event.record.cancelled') {
    if (rec.on) recSetOn(false);
    return;
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

// ---------------------------------------------------------------- 操作录制（M3-WP2）

// 录制的界面侧状态。为什么要"实时显示已经录到哪几步"：录制是个看不见的过程
// （用户在别的窗口里操作），不给反馈的话他不知道录没录上、要不要重录。
const rec = { on: false, blocks: [], t0: 0, timer: null, hotkey: 'Ctrl+Alt+Q' };

function recRender() {
  const list = $('recList');
  if (!list) return;
  list.innerHTML = '';
  rec.blocks.forEach((b) => {
    const li = document.createElement('li');
    li.textContent = b.text;
    if (b.unsupported) li.className = 'warn';
    list.appendChild(li);
  });
  $('recEmpty').hidden = rec.blocks.length > 0;
}

function recUpsert(item) {
  if (item.update && item.index < rec.blocks.length) rec.blocks[item.index] = item;
  else if (item.index === rec.blocks.length) rec.blocks.push(item);
  else if (item.index < rec.blocks.length) rec.blocks[item.index] = item;
  recRender();
}

function recTick() {
  const secs = Math.round((Date.now() - rec.t0) / 1000);
  $('recTimer').textContent = `${secs} 秒 · ${rec.blocks.length} 个动作`;
}

function recSetOn(on) {
  rec.on = on;
  $('recPanel').hidden = !on;
  $('btnRecord').disabled = on;
  $('btnRecord').classList.toggle('recording', on);
  if (on) {
    rec.blocks = [];
    rec.t0 = Date.now();
    recRender();
    recTick();
    rec.timer = setInterval(recTick, 500);
  } else if (rec.timer) {
    clearInterval(rec.timer);
    rec.timer = null;
  }
  // 录制模式交给主进程：开始录制就把窗口最小化 + 弹通知告知停止热键，
  // 停止后把窗口恢复回来。用户在别的程序里按热键停的时候，这一条尤其重要——
  // 否则他会以为"录完了但界面不见了"。
  api.recordingMode(on, rec.hotkey);
}

async function onRecord() {
  if (rec.on) return;
  setState('准备录制…');
  const r = await api.call('record.start', {});
  if (!r.ok) {
    setState('录制失败');
    log('没法开始录制：' + (r.error ? r.error.message : '（未知原因）'), 'err');
    return;
  }
  const res = r.result || {};
  if (res.ok === false) {
    setState('录制失败');
    log('没法开始录制：' + (res.note || '（未知原因）'), 'err');
    return;
  }
  recSetOn(true);
  setState('录制中', 'ok');
  log('开始录制：窗口已最小化，别挡着你操作。做完按热键停止。', 'ok');
  log('提示：你在「脚本构建器」里的操作不会计入步骤，放心切回来点停止。');
}

async function onRecordStop() {
  if (!rec.on) return;
  recSetOn(false);
  setState('正在认你点到的是什么…');
  log('正在认你点到的是什么（可能要几秒）……');
  const r = await api.call('record.stop', {});
  if (!r.ok) {
    setState('录制失败');
    log('停止失败：' + (r.error ? r.error.message : '（未知原因）'), 'err');
    return;
  }
  await applyRecorded(r.result || {});
}

async function applyRecorded(res) {
  const steps = res.steps || [];
  const notes = res.notes || [];
  if (steps.length) {
    snapshot('录制生成步骤');
    const arr = state.script.steps;              // 录制结果一律接在主流程末尾
    steps.forEach((s) => arr.push(Object.assign({}, s, { id: uid('s') })));
    renderSteps();
  }
  setState(steps.length ? '录制完成' : '没录到步骤', steps.length ? 'ok' : '');
  const sum = res.summary || {};
  const own = Number(res.skipped_own || 0);
  log(`录制完成：记下 ${sum.total || 0} 个动作，生成 ${steps.length} 步`
    + (notes.length ? `，${notes.length} 步没能生成` : ''), steps.length ? 'ok' : 'warn');
  if (own) log(`（你在本应用里的 ${own} 次操作没有计入——那是点「停止」之类的动作）`);
  notes.forEach((n) => log('· ' + n, 'warn'));
  if (!steps.length) {
    log('没有可用的步骤。若是「没拿到页面/部件」，请把要操作的程序窗口放在最前面再录一次。', 'warn');
  }
}

async function onRecordCancel() {
  if (!rec.on) return;
  recSetOn(false);
  await api.call('record.cancel', {});
  setState('已放弃录制');
  log('已放弃本次录制（什么都没加进脚本）。', 'warn');
}

// ---------------------------------------------------------------- 复制 / 拖拽排序（M4-WP6）

// 复制一个步骤（含条件/循环里的嵌套步骤），**重新生成所有 id**。
// 为什么必须重建 id：schema 校验会拦重复 id，而复制出来的步骤 id 一定与原步骤相同；
// 嵌套步骤（then/else/body）里的 id 也要一起换，否则一复制就整份脚本不合法。
function cloneStep(step) {
  const copy = JSON.parse(JSON.stringify(step));
  const fix = (s) => {
    s.id = uid('s');
    if (s.type === 'condition') { (s.then || []).forEach(fix); (s.else || []).forEach(fix); }
    if (s.type === 'loop') { (s.body || []).forEach(fix); }
  };
  fix(copy);
  return copy;
}

function copyStep(step) {
  const arr = findParentArr(step);
  if (!arr) return;
  state.clip = JSON.parse(JSON.stringify(step));
  snapshot('复制一步');
  arr.splice(arr.indexOf(step) + 1, 0, cloneStep(step));
  renderSteps();
  log('已复制这一步（就贴在它下面）。按 Ctrl+V 还能再贴一份到「接下来加入」的位置。');
}

function pasteStep() {
  if (!state.clip) {
    log('还没有复制过步骤。点任意一步右边的“复制”按钮先复制一份。', 'warn');
    return;
  }
  const arr = containerAt(state.insertPath) || state.script.steps;
  snapshot('粘贴一步');
  arr.push(cloneStep(state.clip));
  renderSteps();
  log('已粘贴到「接下来加入」的位置。');
}

// 拖拽排序：只允许在**同一个容器内**换位置（拖进/拖出条件或循环体容易误操作，
// 那件事用上面的「接下来加入」下拉框做，更明确）。
let dragSrc = null;

function enableDragSort() {
  const rows = Array.from($('stepList').children);
  rows.forEach((li) => {
    li.draggable = true;
    li.classList.add('draggable');
    li.addEventListener('dragstart', (e) => {
      dragSrc = li;
      li.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', 'step'); } catch (err) { /* ignore */ }
    });
    li.addEventListener('dragend', () => {
      dragSrc = null;
      li.classList.remove('dragging');
      rows.forEach((r) => r.classList.remove('drop-before', 'drop-after'));
    });
    li.addEventListener('dragover', (e) => {
      if (!dragSrc || dragSrc === li) return;
      e.preventDefault();
      const r = li.getBoundingClientRect();
      const after = (e.clientY - r.top) > r.height / 2;
      li.classList.toggle('drop-after', after);
      li.classList.toggle('drop-before', !after);
    });
    li.addEventListener('dragleave', () => {
      li.classList.remove('drop-before', 'drop-after');
    });
    li.addEventListener('drop', (e) => {
      if (!dragSrc || dragSrc === li) return;
      e.preventDefault();
      const after = li.classList.contains('drop-after');
      li.classList.remove('drop-before', 'drop-after');
      reorderByRows(dragSrc, li, after);
    });
  });
}

// 按"界面上的行"反推数据顺序：行顺序就是渲染顺序（含嵌套），
// 所以把两个步骤对象按行序重排即可——不用去猜它们在哪个数组里。
function reorderByRows(fromRow, toRow, after) {
  const order = [];
  const collect = (steps) => {
    for (const s of steps) {
      order.push(s);
      if (s.type === 'condition') { collect(s.then || []); collect(s.else || []); }
      if (s.type === 'loop') collect(s.body || []);
    }
  };
  collect(state.script.steps);
  const rows = Array.from($('stepList').children);
  const idOf = (li) => order[rows.indexOf(li)];
  const a = idOf(fromRow), b = idOf(toRow);
  if (!a || !b || a === b) return;
  // 只允许同容器内换位：用"同一个父数组"判断
  const arrA = findParentArr(a), arrB = findParentArr(b);
  if (arrA !== arrB) {
    log('跨层拖动先不支持：要放进「就做 / 否则 / 循环体」，用上面的「接下来加入」下拉框。', 'warn');
    return;
  }
  snapshot('拖动换位置');
  const from = arrA.indexOf(a);
  arrA.splice(from, 1);
  let to = arrA.indexOf(b);
  arrA.splice(after ? to + 1 : to, 0, a);
  renderSteps();
  log('已按你拖的位置换好顺序。');
}

// ---------------------------------------------------------------- 导出给智能体（M4-WP3）

// 向导做成一步一步的白话问答，而不是一次抛出十几个选项：
// 选目录 → 看认出什么 → 先看看要写什么（干跑）→ 有冲突再选怎么办 → 写入。
// 关键的安全感来自"先看看要写什么"这一步：不预览就落盘，用户无从判断会不会覆盖东西。
const RECENT_KEY = 'sg.export.dirs';

function recentDirs() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch (e) { return []; }
}

function rememberDir(d) {
  try {
    const list = [d].concat(recentDirs().filter((x) => x !== d)).slice(0, 5);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch (e) { /* 存不下就算了，不影响导出 */ }
}

function wizardShell(title) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-mask';
  wrap.innerHTML = `<div class="modal wide">
    <div class="modal-title">${title}</div>
    <div id="_wzBody" class="wz-body"></div>
    <div class="modal-ops">
      <button id="_wzCancel" class="btn ghost">取消</button>
      <button id="_wzOk" class="btn primary" disabled>继续</button>
    </div></div>`;
  document.body.appendChild(wrap);
  return wrap;
}

async function onExport() {
  if (!state.script.steps.length) {
    log('先搭一个步骤再导出（导出是把当前脚本变成智能体能调用的技能）。', 'warn');
    return;
  }
  const wrap = wizardShell('导出给智能体');
  const body = wrap.querySelector('#_wzBody');
  const okBtn = wrap.querySelector('#_wzOk');
  const cancelBtn = wrap.querySelector('#_wzCancel');
  const close = () => wrap.remove();

  let chosenDir = '';
  let planId = '';
  let conflict = 'skip';
  let outlet = 'image';       // image | code | both（§8.9 双导出出口，用户自选）

  const renderPick = () => {
    const dirs = recentDirs();
    body.innerHTML = `
      <p class="wz-step"><b>第 1 步 / 3</b>：导出成什么？</p>
      <div class="wz-outlets">
        <label class="wz-outlet"><input type="radio" name="wzOutlet" value="image">
          <span><b>图片脚本</b>（推荐）<br><span class="muted">智能体照着你搭的脚本跑，
          识别和点击仍在本机做——最忠实、最可靠</span></span></label>
        <label class="wz-outlet"><input type="radio" name="wzOutlet" value="code">
          <span><b>代码脚本</b>（给开发者）<br><span class="muted">AI 读页面和部件的截图，
          生成可读的 Python 源码，能修改、能脱离本产品</span></span></label>
        <label class="wz-outlet"><input type="radio" name="wzOutlet" value="both">
          <span><b>两个都要</b><br><span class="muted">源码用来读和改，图片脚本留着随时回退</span></span></label>
      </div>
      <p class="wz-step"><b>第 2 步 / 3</b>：智能体装在哪？</p>
      <p class="hint">选它的项目目录（里面通常有 skills / tools 文件夹）。
      我会先**只看不改**，扫描出它已经有哪些工具，再把你的脚本拼成它认的格式。</p>
      <div class="wz-row">
        <input id="_wzDir" class="name wz-dir" placeholder="例如 D:\\我的Agent项目"
               value="${chosenDir.replace(/"/g, '&quot;')}">
        <button id="_wzBrowse" class="btn ghost">浏览…</button>
      </div>
      ${dirs.length ? `<div class="wz-recent">最近用过：${dirs.map((d) =>
        `<button class="wz-pill" data-dir="${d.replace(/"/g, '&quot;')}">${d}</button>`).join('')}</div>` : ''}
      <div id="_wzScan" class="wz-scan"></div>`;
    body.querySelectorAll('input[name="wzOutlet"]').forEach((r) => {
      r.checked = r.value === outlet;
      r.onchange = () => { outlet = r.value; };
    });
    body.querySelector('#_wzBrowse').onclick = async () => {
      const d = await api.fileDialog('dir');
      if (d) { chosenDir = d; renderPick(); scan(); }
    };
    body.querySelectorAll('.wz-pill').forEach((b) => {
      b.onclick = () => { chosenDir = b.dataset.dir; renderPick(); scan(); };
    });
    const inp = body.querySelector('#_wzDir');
    // 手输路径也要扫描：不然用户敲完目录什么都看不到，还得先点一次「继续」才知道对不对
    let scanTimer = null;
    inp.oninput = () => {
      chosenDir = inp.value.trim();
      okBtn.disabled = !chosenDir;
      clearTimeout(scanTimer);
      if (chosenDir) scanTimer = setTimeout(scan, 350);
    };
    okBtn.textContent = '先看看要写什么';
    okBtn.disabled = !chosenDir;
    okBtn.onclick = () => preview();
    if (chosenDir) scan();
  };

  const scan = async () => {
    const box = body.querySelector('#_wzScan');
    if (!box) return;
    box.textContent = '正在看看这个目录里有什么…';
    const r = await api.call('export.scan', { dir: chosenDir });
    if (!r.ok) { box.className = 'wz-scan warn'; box.textContent = '看不了这个目录：' + r.error.message; return; }
    const s = r.result;
    box.className = 'wz-scan';
    box.innerHTML = `<b>认出来了</b>：${s.framework === '已识别' ? '这是一个智能体项目' : '没认出特别的格式，会按通用方式放'}。
      已有工具 ${s.tools.length} 个${s.tools.length ? '：' + s.tools.slice(0, 8).join('、') : ''}<br>
      已有技能 ${s.skills.length} 个　技能放 <code>${s.skills_dir}/</code>　工具放 <code>${s.tools_dir}/</code>`
      + (s.notes.length ? `<br><span class="muted">${s.notes.join('；')}</span>` : '');
  };

  const preview = async () => {
    body.innerHTML = '<p class="wz-step"><b>第 3 步 / 3</b>：先看看要写什么（还没有动你的文件）</p>'
      + '<div id="_wzPrev" class="wz-prev">正在拼装…'
      + (outlet === 'image' ? '' : '（代码脚本要请云端读一遍截图，可能要十几秒）') + '</div>';
    okBtn.disabled = true;
    const r = await api.call('export.plan', {
      script: state.script, dir: chosenDir, conflict, outlet,
    });
    if (!r.ok) {
      body.querySelector('#_wzPrev').className = 'wz-prev warn';
      body.querySelector('#_wzPrev').textContent = '拼装失败：' + r.error.message;
      okBtn.disabled = false; okBtn.textContent = '返回'; okBtn.onclick = renderPick;
      return;
    }
    const p = r.result;
    planId = p.plan_id;
    const conflicts = p.files.filter((f) => f.action === 'skip' || f.action === 'overwrite');
    body.innerHTML = `
      <p class="wz-step"><b>第 3 步 / 3</b>：先看看要写什么（还没有动你的文件）</p>
      <pre class="wz-prev">${p.diff.replace(/</g, '&lt;')}</pre>
      ${conflicts.length ? `<p class="hint">有 ${conflicts.length} 个同名文件。你可以选择怎么处理：</p>
        <div class="wz-row">
          <select id="_wzConflict" class="name">
            <option value="skip">跳过它们（最安全，默认）</option>
            <option value="overwrite">覆盖它们（会先备份成 .bak）</option>
            <option value="rename">给我的加个后缀，谁都不动</option>
          </select>
        </div>` : ''}
      ${p.problems.length ? `<p class="warn-text">产物有问题，先修好再导出：<br>${p.problems.join('<br>')}</p>` : ''}
      ${(p.notes || []).length ? `<div class="wz-notes">${p.notes.map((n) =>
        `<div>· ${n}</div>`).join('')}</div>` : ''}
      <p class="hint">放心：识别与输入仍在本机引擎里跑（导出物不是"拷到别的机器就能用"的独立代码）。</p>`;
    const sel = body.querySelector('#_wzConflict');
    if (sel) {
      sel.value = conflict;
      sel.onchange = () => { conflict = sel.value; preview(); };
    }
    if (p.ai_failed) {
      // 出口 ② 没成（没授权 / 转译被拦下）→ 说清原因并让用户回到出口选择
      okBtn.textContent = '回到上一步'; okBtn.disabled = false; okBtn.onclick = renderPick;
      return;
    }
    okBtn.textContent = `写入（${p.write_count} 个文件）`;
    okBtn.disabled = p.write_count === 0 || p.problems.length > 0;
    okBtn.onclick = doApply;
  };

  const doApply = async () => {
    okBtn.disabled = true;
    const r = await api.call('export.apply', { plan_id: planId, conflict });
    if (!r.ok) {
      body.innerHTML = `<p class="wz-step"><b>第 3 步 / 3</b>：写入</p>
        <p class="warn-text">写入没成功：${r.error.message}</p>`;
      okBtn.textContent = '返回'; okBtn.disabled = false; okBtn.onclick = renderPick;
      return;
    }
    const o = r.result;
    rememberDir(chosenDir);
    body.innerHTML = `<p class="wz-step"><b>第 3 步 / 3</b>：写好了</p>
      <p>写了 ${o.written.length} 个文件：${o.written.join('、') || '（没有需要写的）'}</p>
      ${o.skipped.length ? `<p class="muted">跳过了：${o.skipped.join('、')}</p>` : ''}
      ${o.backed_up.length ? `<p class="muted">覆盖前备份了：${o.backed_up.join('、')}</p>` : ''}
      <p><b>${o.note}</b></p>`;
    log(`导出完成：${o.written.length} 个文件 → ${chosenDir}`, 'ok');
    log(o.note);
    okBtn.textContent = '好'; okBtn.disabled = false; okBtn.onclick = close;
    cancelBtn.textContent = '关闭';
  };

  cancelBtn.onclick = close;
  renderPick();
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
  $('btnRecord').onclick = onRecord;
  $('btnRecordStop').onclick = onRecordStop;
  $('btnRecordCancel').onclick = onRecordCancel;
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
  if ($('btnExport')) $('btnExport').onclick = onExport;
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
    if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); redo(); return; }
    if (k === 'v') { e.preventDefault(); pasteStep(); }
  });
  api.onEvent(onEngineEvent);
  // 桌面浮条上的「停止」→ 录制中停录制，运行中停运行（浮条文案由主进程同步）
  if (api.onStopRequest) {
    api.onStopRequest(() => {
      if (rec.on) onRecordStop();
      else if (state.running) onStop();
      else api.closeStopBar();
    });
  }
  renderPending();
  renderSteps();
  renderUndoButtons();
  log('准备就绪：点“截图目标”开始（先框页面，再框部件）。');
  log('快捷键：Ctrl+S 保存 · Ctrl+Z 撤销 · Ctrl+Y 重做');
});
