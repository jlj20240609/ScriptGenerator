// 双截图选区覆盖层：第一次拖框=页面；第二次拖框=部件；Esc 取消
/* global api */
(() => {
  const canvas = document.getElementById('cv');
  const ctx = canvas.getContext('2d');
  const hint = document.getElementById('hint');
  const dpr = window.devicePixelRatio || 1;

  let stage = 1;                  // 1=框页面，2=框部件
  let pageRect = null;            // DIP，相对窗口
  let sel = { x: 0, y: 0, w: 0, h: 0 };
  let down = { x: 0, y: 0 };
  let dragging = false;
  let busy = false;
  const tip = document.getElementById('tip');
  const tip0 = tip.textContent;

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    // 双保险：CSS 尺寸必须等于视口（否则 canvas 按内在尺寸布局 → 画出来的框会缩放）
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    report('resize');
    draw();
  }
  window.addEventListener('resize', resize);

  // 诊断上报：覆盖层"看到的"坐标系（与真实光标对照，用来定位框选偏移）
  function report(tag, ev, rect) {
    try {
      const cs = getComputedStyle(canvas);
      api.overlayDebug({
        tag, dpr, innerW: window.innerWidth, innerH: window.innerHeight,
        canvasCss: [parseFloat(cs.width), parseFloat(cs.height)],
        canvasRect: (() => { const r = canvas.getBoundingClientRect();
          return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })(),
        canvasBacking: [canvas.width, canvas.height],
        screen: [screen.width, screen.height], avail: [screen.availWidth, screen.availHeight],
        client: ev ? [ev.clientX, ev.clientY] : null,
        screenXY: ev ? [ev.screenX, ev.screenY] : null,
        rect: rect || null,
      });
    } catch (e) { /* ignore */ }
  }

  function draw() {
    const W = window.innerWidth, H = window.innerHeight;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = 'rgba(2,6,23,.32)';
    ctx.fillRect(0, 0, W, H);
    if (pageRect) {                        // 已选页面：浅色保留
      ctx.save();
      ctx.clearRect(pageRect.x, pageRect.y, pageRect.w, pageRect.h);
      ctx.strokeStyle = 'rgba(148,197,253,.9)';
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(pageRect.x, pageRect.y, pageRect.w, pageRect.h);
      ctx.restore();
    }
    if (dragging && sel.w > 0 && sel.h > 0) {
      ctx.fillStyle = 'rgba(56,189,248,.14)';
      ctx.fillRect(sel.x, sel.y, sel.w, sel.h);
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 2;
      ctx.strokeRect(sel.x, sel.y, sel.w, sel.h);
      const info = `起点 ${Math.round(down.x)},${Math.round(down.y)} · ` +
        `${Math.round(sel.w)} × ${Math.round(sel.h)}`;
      tip.textContent = info;
      ctx.fillStyle = '#0ea5e9';
      ctx.font = '13px "Microsoft YaHei", sans-serif';
      ctx.fillText(info, sel.x + 6, Math.max(14, sel.y - 6));
    } else {
      tip.textContent = tip0;
    }
  }

  canvas.addEventListener('mousedown', (e) => {
    if (busy) return;
    dragging = true;
    sel = { x: e.clientX, y: e.clientY, w: 0, h: 0 };
    down = { x: e.clientX, y: e.clientY };
    report('down', e);
    draw();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    sel.w = Math.abs(e.clientX - sel.x);
    sel.h = Math.abs(e.clientY - sel.y);
    sel.x = Math.min(sel.x, e.clientX);
    sel.y = Math.min(sel.y, e.clientY);
    draw();
  });
  window.addEventListener('mouseup', async (e) => {
    if (!dragging || busy) return;
    dragging = false;
    const r = { x: Math.min(sel.x, e.clientX), y: Math.min(sel.y, e.clientY),
      w: Math.abs(e.clientX - sel.x), h: Math.abs(e.clientY - sel.y) };
    if (r.w < 8 || r.h < 8) { sel = { x: 0, y: 0, w: 0, h: 0 }; draw(); return; }
    busy = true;
    report('up', e, r);
    const res = await api.overlaySelection({ step: stage === 1 ? 'page' : 'widget',
      rect: r, dpr });
    if (stage === 1 && res && res.ok) {         // 进入第二步
      pageRect = r;
      sel = { x: 0, y: 0, w: 0, h: 0 };
      stage = 2;
      hint.textContent = res.hint || '第二步：框住要操作的位置';
      busy = false;
      draw();
      return;
    }
    if (res && res.ok) return;                  // 主进程会关闭覆盖层
    busy = false;
    hint.textContent = '这次框选没有生效：' + ((res && res.error) || '请重试');
    sel = { x: 0, y: 0, w: 0, h: 0 };
    draw();
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') api.overlayCancel();
  });

  resize();

  // 自动测试入口（--autotest-pick 时主进程注入合成拖拽事件后调用）
  window.__autoPick = (rects) => {
    return new Promise(async (resolve) => {
      const fire = (type, x, y) => window.dispatchEvent(new MouseEvent(type,
        { clientX: x, clientY: y, bubbles: true }));
      const canvasDown = (x, y) => canvas.dispatchEvent(new MouseEvent('mousedown',
        { clientX: x, clientY: y, bubbles: true }));
      const [p1, p2] = rects;
      canvasDown(p1.x, p1.y); fire('mousemove', p1.x + p1.w, p1.y + p1.h);
      fire('mouseup', p1.x + p1.w, p1.y + p1.h);
      await new Promise((r) => setTimeout(r, 700));
      canvasDown(p2.x, p2.y); fire('mousemove', p2.x + p2.w, p2.y + p2.h);
      fire('mouseup', p2.x + p2.w, p2.y + p2.h);
      await new Promise((r) => setTimeout(r, 400));
      resolve({ stage, pageRect });
    });
  };
})();
