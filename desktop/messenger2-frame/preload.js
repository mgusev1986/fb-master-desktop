'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('m2api', {
  getConfig: () => ipcRenderer.invoke('m2:get-config'),
  spawnChrome: (userDataDir) => ipcRenderer.invoke('m2:spawn-chrome', { userDataDir }),
});
