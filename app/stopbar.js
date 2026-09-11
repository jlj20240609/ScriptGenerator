// 桌面浮动「停止」条的交互（录制/运行期间显示）。
// 这里只做两件事：切文案（录制中 → 停止录制；运行中 → 停止运行）、把"用户点了停止"
// 告诉主进程。
//
// 为什么停止要单独做一个浮条（用户反馈）：快捷键 Ctrl+Alt+Q 被别的软件占用了，
// 等于没有停止手段；而且录制/运行时构建器窗口是收起来的，不该为了停止去任务栏翻窗口。
// 它自己的点击会被录制器按窗口句柄过滤掉，不会变成脚本里的步骤。
const stopBtn = document.getElementById('stop');
let mode = 'record';

window.api.onBarMode((p) => {
  mode = (p && p.kind) || 'record';
  stopBtn.textContent = mode === 'run' ? '停止运行' : '停止录制';
  stopBtn.title = mode === 'run'
    ? '让脚本停下来；这个条上的操作不会进入脚本'
    : '结束录制并生成步骤；这个条上的操作不会被录进去';
});

function stopNow() {
  stopBtn.disabled = true;
  stopBtn.textContent = '正在停止…';
  window.api.stopRecording().then((r) => {
    if (!r || !r.ok) {          // 没人在跑/录了（比如已经用别的方式停了）→ 直接关掉自己
      window.api.closeStopBar();
    }
  }).catch(() => {
    stopBtn.disabled = false;
    stopBtn.textContent = mode === 'run' ? '停止运行' : '停止录制';
  });
}

stopBtn.addEventListener('click', stopNow);
// 拖动手感：整条都是拖动区（见 html 里的 -webkit-app-region: drag），
// 所以这里不需要自己实现拖动逻辑，Windows 会按原生窗口拖动处理。
