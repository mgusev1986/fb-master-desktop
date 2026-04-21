'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

// Стабильное хранилище для partition persist:* (cookies Facebook переживают перезапуск).
const userDataPath = path.join(__dirname, '.electron-app-data');
try {
  fs.mkdirSync(userDataPath, { recursive: true });
} catch (_e) {
  /* ignore */
}
app.setPath('userData', userDataPath);

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

let mainWindow = null;

function desktopMessengerUserAgent() {
  if (process.platform === 'darwin') {
    return 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
  }
  if (process.platform === 'win32') {
    return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
  }
  return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
}

function cookieConsentAutoclickScript() {
  return `
    (() => {
      const allowPhrases = [
        'разрешить использование файлов cookie',
        'разрешить использование всех файлов cookie',
        'разрешить все cookie',
        'разрешить все файлы cookie',
        'разрешить все',
        'принять все',
        'accept all',
        'allow all',
        'allow all cookies',
        'allow essential and optional cookies',
        'ok',
        'ок'
      ];
      const closePhrases = [
        'закрыть',
        'close',
        'dismiss',
        'not now',
        'не сейчас',
        'skip',
        'пропустить',
        'понятно',
        'got it'
      ];
      const denyPhrases = [
        'только обязательные',
        'only essential',
        'manage',
        'подробнее',
        'learn more',
        'delete',
        'удалить'
      ];
      const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const score = (text, aria) => {
        let total = 0;
        for (const phrase of allowPhrases) {
          if (text.includes(phrase)) total += phrase.length + 10;
        }
        for (const phrase of closePhrases) {
          if (text.includes(phrase) || aria.includes(phrase)) total += phrase.length + 30;
        }
        for (const phrase of denyPhrases) {
          if (text.includes(phrase) || aria.includes(phrase)) total -= phrase.length + 30;
        }
        return total;
      };
      const findDismissButton = (root) => {
        let best = null;
        let bestScore = 0;
        const nodes = Array.from((root || document).querySelectorAll('button, [role="button"], a[role="button"], div[role="button"]'));
        for (const node of nodes) {
          if (!visible(node)) continue;
          const text = norm(node.innerText || node.textContent);
          const aria = norm(node.getAttribute('aria-label') || node.getAttribute('title'));
          const current = score(text, aria);
          if (current > bestScore) {
            best = node;
            bestScore = current;
          }
        }
        return bestScore > 0 ? best : null;
      };
      const tryClick = () => {
        let best = null;
        const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]')).filter(visible);
        for (const dialog of dialogs) {
          const btn = findDismissButton(dialog);
          if (btn) {
            best = btn;
            break;
          }
        }
        if (!best) best = findDismissButton(document);
        if (best) {
          best.click();
          return true;
        }
        return false;
      };

      let attempts = 0;
      tryClick();
      const timer = window.setInterval(() => {
        attempts += 1;
        tryClick();
        if (attempts >= 20) window.clearInterval(timer);
      }, 700);
      const observer = new MutationObserver(() => { tryClick(); });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      window.setTimeout(() => observer.disconnect(), 15000);
      return true;
    })();
  `;
}

const CONFIG_NAME = 'accounts.json';

function loadConfig() {
  const p = path.join(__dirname, CONFIG_NAME);
  if (fs.existsSync(p)) {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  }
  const ex = path.join(__dirname, 'accounts.example.json');
  return JSON.parse(fs.readFileSync(ex, 'utf8'));
}

function defaultChromePath() {
  if (process.platform === 'darwin') {
    return '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  }
  if (process.platform === 'win32') {
    return 'C:\\Program Files\\Google Chrome\\Application\\chrome.exe';
  }
  return 'google-chrome';
}

function startHealthServer() {
  const port = parseInt(process.env.MESSENGER2_FRAME_HEALTH_PORT || '37821', 10);
  const server = http.createServer((req, res) => {
    const u = req.url || '';
    if (u === '/health' || u.startsWith('/health?')) {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: true, app: 'fb-master-messenger2-frame' }));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  server.on('error', (err) => {
    console.error('[messenger2-frame] health server:', err.message);
  });
  server.listen(port, '127.0.0.1', () => {
    console.log('[messenger2-frame] health http://127.0.0.1:' + port + '/health');
  });
}

ipcMain.handle('m2:get-config', () => loadConfig());

ipcMain.handle('m2:spawn-chrome', (_e, { userDataDir }) => {
  const cfg = loadConfig();
  const chrome = (cfg.chromeExecutable || '').trim() || defaultChromePath();
  const u = cfg.messengerUrl || 'https://www.facebook.com/messages/e2ee/t';
  if (!userDataDir || typeof userDataDir !== 'string') {
    return { ok: false, error: 'Не указан chromeUserDataDir' };
  }
  const expanded = userDataDir.replace(/^~/, process.env.HOME || '');
  if (!fs.existsSync(expanded)) {
    return { ok: false, error: 'Папка профиля не найдена: ' + expanded };
  }
  try {
    const child = spawn(
      chrome,
      [`--user-data-dir=${expanded}`, u],
      { detached: true, stdio: 'ignore' }
    );
    child.unref();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 920,
    minHeight: 620,
    show: false,
    backgroundColor: '#f3f6fb',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      webviewTag: true,
      preload: path.join(__dirname, 'preload.js'),
    },
    title: 'FB Master — Мессенджер',
  });

  mainWindow.webContents.on('will-attach-webview', (_event, webPreferences, params) => {
    delete webPreferences.preload;
    webPreferences.nodeIntegration = false;
    webPreferences.contextIsolation = true;
    webPreferences.spellcheck = false;
    params.useragent = desktopMessengerUserAgent();
  });

  mainWindow.webContents.on('did-attach-webview', (_event, guestContents) => {
    const normalizeGuest = () => {
      try {
        /* 1.1 съедал нижнюю часть Messenger в Electron; держим натуральный масштаб. */
        guestContents.setZoomFactor(1);
      } catch (_e) {
        /* ignore */
      }
      guestContents
        .insertCSS(
          'html, body { width: 100% !important; min-width: 0 !important; height: 100% !important; min-height: 0 !important; margin: 0 !important; }'
        )
        .catch(() => {});
      guestContents.executeJavaScript(cookieConsentAutoclickScript(), true).catch(() => {});
    };
    guestContents.on('dom-ready', normalizeGuest);
    guestContents.on('did-finish-load', normalizeGuest);
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));
  mainWindow.once('ready-to-show', () => {
    mainWindow.maximize();
    mainWindow.show();
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isMaximized()) mainWindow.maximize();
    mainWindow.show();
    mainWindow.focus();
  }
});

app.whenReady().then(() => {
  startHealthServer();
  createWindow();
});
app.on('window-all-closed', () => app.quit());
