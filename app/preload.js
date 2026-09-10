// 渲染层桥：只暴露最小 API（引擎调用 / 事件订阅 / 文件对话框 / 人工确认 / 选区）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  call: (method, params) => ipcRenderer.invoke('engine:call', { method, params }),
  onEvent: (cb) => ipcRenderer.on('engine-event', (_e, msg) => cb(msg)),
  fileDialog: (kind, defaultPath) => ipcRenderer.invoke('file:dialog', { kind, defaultPath }),
  confirm: (message, options, defaultLabel, kind) =>
    ipcRenderer.invoke('ui:confirm', { message, options, defaultLabel, kind }),
  pickTarget: () => ipcRenderer.invoke('ui:pickTarget'),
  // 选区覆盖层专用（overlay.html 使用）
  overlaySelection: (payload) => ipcRenderer.invoke('overlay:selection', payload),
  overlayCancel: () => ipcRenderer.send('overlay:cancel'),
  overlayDebug: (payload) => ipcRenderer.send('overlay:debug', payload),
});
