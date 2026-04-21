'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('fbmUpdateDialog', {
  onInit(cb) {
    const fn = (_e, payload) => cb(payload);
    ipcRenderer.on('update-dialog-init', fn);
    return () => ipcRenderer.removeListener('update-dialog-init', fn);
  },
  onProgress(cb) {
    const fn = (_e, payload) => cb(payload);
    ipcRenderer.on('update-download-progress', fn);
    return () => ipcRenderer.removeListener('update-download-progress', fn);
  },
  onDone(cb) {
    const fn = (_e, payload) => cb(payload);
    ipcRenderer.on('update-download-done', fn);
    return () => ipcRenderer.removeListener('update-download-done', fn);
  },
  onError(cb) {
    const fn = (_e, payload) => cb(payload);
    ipcRenderer.on('update-download-error', fn);
    return () => ipcRenderer.removeListener('update-download-error', fn);
  },
  secondary: () => ipcRenderer.invoke('update-dialog:secondary'),
  download: () => ipcRenderer.invoke('update-dialog:download'),
  closeWindow: () => ipcRenderer.invoke('update-dialog:close'),
});
