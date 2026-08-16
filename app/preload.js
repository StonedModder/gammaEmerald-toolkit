'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('gamma', {
  call: (method, params) => ipcRenderer.invoke('rpc', method, params),
  pickFolder: () => ipcRenderer.invoke('pick-folder'),
  pickExe: () => ipcRenderer.invoke('pick-exe'),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  getSettings: () => ipcRenderer.invoke('settings-get'),
  setSettings: (patch) => ipcRenderer.invoke('settings-set', patch),
  onEvent: (fn) => {
    ipcRenderer.on('daemon-event', (_e, msg) => fn(msg));
  },
});
