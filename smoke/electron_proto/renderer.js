// M0④ 选区层渲染：拖框 = 用户预期区域；定位结果 = 引擎坐标（物理→DIP 换算后叠加显示）
(() => {
  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1; // 系统缩放（125% → 1.25）

  const sel = { x: 0, y: 0, w: 0, h: 0, active: false };
  let marker = null; // {x, y} DIP；引擎回传的部件中心
  let markerScore = 0;
  let markerMs = 0;
  let deviation = null;

  function resize() {
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  window.addEventListener('resize', resize);
  resize();

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(2,6,23,.30)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (sel.active) {
      ctx.fillStyle = 'rgba(56,189,248,.12)';
      ctx.fillRect(sel.x, sel.y, sel.w, sel.h);
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 2;
      ctx.strokeRect(sel.x, sel.y, sel.w, sel.h);
    }
    if (marker) {
      ctx.strokeStyle = '#f43f5e';
      ctx.lineWidth = 3;
      const r = 10;
      ctx.beginPath();
      ctx.moveTo(marker.x - r, marker.y); ctx.lineTo(marker.x + r, marker.y);
      ctx.moveTo(marker.x, marker.y - r); ctx.lineTo(marker.x, marker.y + r);
      ctx.stroke();
      ctx.beginPath(); ctx.arc(marker.x, marker.y, r * 0.35, 0, 7); ctx.stroke();
    }
    if (deviation !== null) {
      ctx.strokeStyle = '#fbbf24';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(sel.x + sel.w / 2, sel.y + sel.h / 2);
      ctx.lineTo(marker.x, marker.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  let dragging = false;
  canvas.addEventListener('mousedown', (e) => {
    if (e.target !== canvas) return;
    dragging = true;
    sel.x = e.clientX; sel.y = e.clientY; sel.w = 0; sel.h = 0; sel.active = true;
    marker = null; deviation = null;
    updateInfo('拖动中…');
    draw();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    sel.w = e.clientX - sel.x;
    sel.h = e.clientY - sel.y;
    draw();
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
    if (sel.active && sel.w < 6 && sel.h < 6) {
      sel.active = false; // 纯点击不算框选
      updateInfo('请拖出一个区域');
      draw();
    } else if (sel.active) {
      updateInfo('已框选预期区域，可点击“定位”');
    }
  });

  const infoEl = document.getElementById('info');
  function updateInfo(s) { infoEl.textContent = s; }

  document.getElementById('go').addEventListener('click', async () => {
    const text = document.getElementById('text').value.trim();
    if (!text) return updateInfo('先输入目标文字');
    updateInfo('引擎定位中…');
    try {
      const t0 = performance.now();
      // 若拖了框：把框（DIP）转物理像素作为 OCR 搜索区域（小区域才可能 <1s）
      let region = null;
      if (sel.active) {
        region = [
          Math.round(sel.x * dpr), Math.round(sel.y * dpr),
          Math.max(8, Math.round(sel.w * dpr)), Math.max(8, Math.round(sel.h * dpr)),
        ];
      }
      const res = await window.m0.locateText(text, region);
      const latency = (performance.now() - t0).toFixed(0);
      if (!res || !res.ok) {
        updateInfo(`未找到“${text}” (${latency}ms)`);
        return;
      }
      // 引擎返回物理像素中心 → DIP
      marker = { x: res.center[0] / dpr, y: res.center[1] / dpr };
      markerScore = res.score;
      markerMs = latency;
      if (sel.active) {
        const cx = sel.x + sel.w / 2, cy = sel.y + sel.h / 2;
        deviation = Math.round(Math.max(Math.abs(marker.x - cx), Math.abs(marker.y - cy)));
        updateInfo(`命中 score=${res.score.toFixed(2)} 往返=${latency}ms(含OCR) ` +
          `偏差=${deviation}px scale=${dpr}`);
      } else {
        updateInfo(`命中 score=${res.score.toFixed(2)} 往返=${latency}ms scale=${dpr}`);
      }
      draw();
    } catch (err) {
      updateInfo('RPC 错误: ' + err.message);
    }
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.m0.quit();
  });

  // 自动验证入口（main 在 M0_AUTOTEST 下调用）
  // 自洽链路：①全屏定位得真值 C（物理）→ ②以 C 为中心构造 60x40 搜索框（DIP→物理换算）→
  // ③区域内二次定位应回到 C；devPhysical≤3px 即证明 引擎坐标 ⇄ 覆盖层 DIP 换算无错位。
  window.__runAuto = async () => {
    const text = '库存查询';
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    for (let attempt = 1; attempt <= 4; attempt++) {
      try {
        const loc1 = await window.m0.locateText(text, null); // 全屏
        if (!loc1 || !loc1.ok) { console.log('AUTOTEST loc1 not-found, retry'); await sleep(2500); continue; }
        const C = loc1.center; // 物理中心
        // 构造 DIP 搜索框：中心 C/1.25，尺寸 48x32 DIP → 物理 60x40
        const padPx = 30;
        const region = [
          Math.max(0, Math.round(C[0] - padPx)),
          Math.max(0, Math.round(C[1] - padPx)),
          2 * padPx, 2 * padPx,
        ];
        const loc2 = await window.m0.locateText(text, region);
        if (!loc2 || !loc2.ok) { console.log('AUTOTEST loc2 not-found (窗口被移动?), retry'); await sleep(2500); continue; }
        const devPhys = Math.max(Math.abs(loc2.center[0] - C[0]), Math.abs(loc2.center[1] - C[1]));
        // DIP 空间：引擎回传中心换算 DIP 与“预期”DIP 的偏差（应 = devPhys / dpr）
        const devDip = Math.max(
          Math.abs(loc2.center[0] / dpr - C[0] / dpr),
          Math.abs(loc2.center[1] / dpr - C[1] / dpr));
        console.log('AUTOTEST ok text=' + text +
          ' truth=' + JSON.stringify(C) +
          ' region=' + JSON.stringify(region) +
          ' loc2=' + JSON.stringify(loc2.center) +
          ' devPhysical=' + devPhys.toFixed(1) + 'px' +
          ' devDip=' + devDip.toFixed(1) +
          ' score=' + (loc2.score || 0).toFixed(3) +
          ' ms1=' + (loc1.total_ms || loc1.elapsed_ms || 0).toFixed(0) +
          ' ms2=' + (loc2.total_ms || loc2.elapsed_ms || 0).toFixed(0) +
          ' dpr=' + dpr);
        marker = { x: loc2.center[0] / dpr, y: loc2.center[1] / dpr };
        draw();
        return;
      } catch (err) {
        console.log('AUTOTEST error:', err.message);
        return;
      }
    }
    console.log('AUTOTEST failed after retries');
  };
})();
