// 预加载：暴露最小 RPC 通道
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('m0', {
  locateText: (text, region) => ipcRenderer.invoke('locate-text', { text, region }),
  ping: () => ipcRenderer.invoke('rpc', { method: 'ping' }),
  quit: () => ipcRenderer.send('quit'),
});
