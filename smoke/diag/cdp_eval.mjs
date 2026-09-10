// 通过 CDP 在渲染层执行一段 JS（绕开 OCR/鼠标定位，诊断用）
// 用法：node smoke/diag/cdp_eval.mjs <port> "<js 表达式>"
const port = process.argv[2] || '9222';
const expr = process.argv[3] || '1';

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = list.find((t) => t.type === 'page' && (t.title || '').includes('脚本构建器'))
  || list.find((t) => t.type === 'page');
if (!page) {
  console.log('没找到可调试的页面:', JSON.stringify(list.map((t) => t.title)));
  process.exit(1);
}
console.log('目标页面:', page.title);

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
const send = (id, method, params) => new Promise((resolve, reject) => {
  const onMsg = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id === id) {
      ws.removeEventListener('message', onMsg);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  };
  ws.addEventListener('message', onMsg);
  ws.send(JSON.stringify({ id, method, params }));
});

const res = await send(1, 'Runtime.evaluate', {
  expression: expr, awaitPromise: true, returnByValue: true,
});
console.log('结果:', JSON.stringify(res.result && res.result.value));
ws.close();
