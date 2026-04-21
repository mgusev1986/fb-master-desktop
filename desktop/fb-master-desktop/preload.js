'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('fbMasterDesktop', {
  /** SHA256(machine-id) для диагностики; на сервер уходит тем же значением через заголовок. */
  getHardwareDeviceId: () => ipcRenderer.invoke('fb-desktop:hardware-device-id'),
  openChromeProfile: (userDataDir, messengerUrl) =>
    ipcRenderer.invoke('fb-desktop:open-chrome', { userDataDir, messengerUrl }),
  prepareMessengerSession: (partition, storageState) =>
    ipcRenderer.invoke('fb-desktop:prepare-webview-session', { partition, storageState }),
  exportFacebookSession: (partition, profilePath) =>
    ipcRenderer.invoke('fb-desktop:export-facebook-session', { partition, profilePath }),
  setPartitionProxy: (partition, proxyRules, proxyUsername, proxyPassword) =>
    ipcRenderer.invoke('fb-desktop:set-partition-proxy', {
      partition,
      proxyRules,
      proxyUsername,
      proxyPassword,
    }),
  prepareMessengerDiskProfile: (profilePath, proxyRules, proxyUsername, proxyPassword, storageState) =>
    ipcRenderer.invoke('fb-desktop:prepare-messenger-disk-profile', {
      profilePath,
      proxyRules,
      proxyUsername,
      proxyPassword,
      storageState: storageState || null,
    }),
  /* Открыть DevTools для <webview> с заданным id (element.id) — для диагностики embedded login. */
  openWebviewDevTools: (webviewId) =>
    ipcRenderer.invoke('fb-desktop:open-webview-devtools', { webviewId: String(webviewId || '') }),
});
