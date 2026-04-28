'use strict';

const { app, BrowserWindow, Menu, clipboard, ipcMain, session, dialog, shell, screen } = require('electron');

// Нативное контекстное меню (правый клик): Cut / Copy / Paste / Select All
// + «Открыть ссылку», если клик пришёлся на <a>. Без этого Electron по
// умолчанию ничего не показывает на правый клик и пользователь не может
// скопировать выделенный текст в textarea (например, в редакторе шаблонов).
function attachContextMenu(wc) {
  if (!wc || typeof wc.on !== 'function' || wc.__ctxMenuAttached) return;
  wc.__ctxMenuAttached = true;
  wc.on('context-menu', (_event, params) => {
    const items = [];
    const isEditable = !!params.isEditable;
    const hasSelection = !!(params.selectionText && params.selectionText.length);
    const linkURL = (params.linkURL || '').trim();

    if (linkURL) {
      items.push(
        { label: 'Открыть ссылку в браузере', click: () => { try { shell.openExternal(linkURL); } catch (_) {} } },
        { label: 'Скопировать ссылку', click: () => { try { clipboard.writeText(linkURL); } catch (_) {} } },
        { type: 'separator' },
      );
    }
    if (isEditable) {
      items.push(
        { role: 'cut', label: 'Вырезать', enabled: hasSelection },
        { role: 'copy', label: 'Копировать', enabled: hasSelection },
        { role: 'paste', label: 'Вставить' },
        { type: 'separator' },
        { role: 'selectAll', label: 'Выделить всё' },
      );
    } else if (hasSelection) {
      items.push({ role: 'copy', label: 'Копировать' });
    }
    if (!items.length) return;
    try {
      Menu.buildFromTemplate(items).popup({ window: BrowserWindow.fromWebContents(wc) || undefined });
    } catch (_) {}
  });
}
const {
  startEmbeddedBackend,
  stopEmbeddedBackend,
  initLauncherLog,
  getLauncherLogPath,
} = require('./local-backend-launcher');

/** Если задан — Electron грузит UI с локального uvicorn (прогрев/Playwright на ПК клиента). */
let embeddedBackendBaseUrl = null;

function requireNodeMachineId() {
  try {
    return require('node-machine-id');
  } catch (_e) {
    return null;
  }
}

let _cachedHardwareDeviceHex = '';

/**
 * Стабильный идентификатор машины (SHA256), общий для оплаты и активации ключа в десктопе.
 * Без node-machine-id — запасной вариант от hostname + путь userData (слабее).
 */
function ensureHardwareDeviceIdHex() {
  if (_cachedHardwareDeviceHex) return _cachedHardwareDeviceHex;
  const salt = 'fbm_hw_v2|pro.socmaster.fbmaster';
  const midMod = requireNodeMachineId();
  try {
    if (midMod && typeof midMod.machineIdSync === 'function') {
      const raw = String(midMod.machineIdSync(true) || '').trim();
      if (raw.length >= 4) {
        _cachedHardwareDeviceHex = crypto
          .createHash('sha256')
          .update(`${salt}|${raw}`, 'utf8')
          .digest('hex');
        return _cachedHardwareDeviceHex;
      }
    }
  } catch (_e) {
    /* ignore */
  }
  const fallback = `${os.hostname()}|${userDataPath}|${salt}`;
  _cachedHardwareDeviceHex = crypto.createHash('sha256').update(fallback, 'utf8').digest('hex');
  return _cachedHardwareDeviceHex;
}
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const http = require('http');
const { spawn } = require('child_process');

/* Упакованное приложение: __dirname внутри .app/app.asar — только для чтения; userData там ломает запуск (мгновенный выход). */
let userDataPath;
if (app.isPackaged) {
  userDataPath = app.getPath('userData');
} else {
  userDataPath = path.join(__dirname, '.electron-app-data');
  try {
    fs.mkdirSync(userDataPath, { recursive: true });
  } catch (_e) {
    /* ignore */
  }
  app.setPath('userData', userDataPath);
}

let desktopPackageVersion = '1.0.0';
try {
  desktopPackageVersion = app.isPackaged
    ? String(app.getVersion() || '').trim()
    : String(require('./package.json').version || '1.0.0').trim();
} catch (_e) {
  /* ignore */
}
if (!desktopPackageVersion) {
  try {
    desktopPackageVersion = String(require('./package.json').version || '1.0.0').trim() || '1.0.0';
  } catch (_e2) {
    desktopPackageVersion = '1.0.0';
  }
}

/** Версия для UI/UA/сравнение с API: 2.14.0 → 2.14; 2.14.1 без изменений (electron-builder требует полный semver в package.json). */
function desktopMarketingVersion(raw) {
  const s = String(raw || '').trim();
  const m = /^(\d+)\.(\d+)\.0$/.exec(s);
  if (m) return `${m[1]}.${m[2]}`;
  return s;
}
desktopPackageVersion = desktopMarketingVersion(desktopPackageVersion);

function withDesktopClientVersionQuery(urlStr) {
  const v = String(desktopPackageVersion || '').trim();
  if (!v) return urlStr;
  try {
    const u = new URL(urlStr);
    u.searchParams.set('fbm_app', v);
    return u.toString();
  } catch (_e) {
    return urlStr;
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

let mainWindow = null;

/** Активное окно кастомного мастера обновления (не максимизировать как всплывающие). */
let updateDialogCtx = null;

/* У Session в Electron нет события login; прокси Basic Auth шлёт только app.on('login'). */
const proxyCredentialsBySession = new WeakMap();
/* Путь user-data для Messenger: не сбрасывать после первого will-attach (Electron может дергать attach больше одного раза). */
let messengerDiskProfileForWebview = null;

(function registerAppProxyLoginHandler() {
  app.on('login', (event, webContents, _authenticationResponseDetails, authInfo, callback) => {
    try {
      if (!webContents || !authInfo || !authInfo.isProxy) return;
      const creds = proxyCredentialsBySession.get(webContents.session);
      if (!creds) return;
      event.preventDefault();
      callback(creds.username || '', creds.password || '');
    } catch (_e) {
      /* ignore */
    }
  });
})();

function desktopMessengerUserAgent() {
  if (process.platform === 'darwin') {
    return 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
  }
  if (process.platform === 'win32') {
    return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
  }
  return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36';
}

function mapElectronSameSite(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'strict') return 'strict';
  if (raw === 'lax') return 'lax';
  if (raw === 'none' || raw === 'no_restriction' || raw === 'no-restriction') return 'no_restriction';
  return undefined;
}

/**
 * URL для cookies.set: host-only на «голом» facebook.com не уходит на www.facebook.com (Messenger).
 * Маркетплейсы дают domain «.facebook.com» → строим https://www.facebook.com + path.
 */
function electronCookieUrlForDomain(hostRaw, pathPart, secure) {
  const host = String(hostRaw || '')
    .trim()
    .replace(/^\./, '')
    .toLowerCase();
  if (!host) return null;
  const scheme = secure !== false ? 'https' : 'http';
  let h = host;
  if (h === 'facebook.com') {
    h = 'www.facebook.com';
  } else if (h === 'messenger.com') {
    h = 'www.messenger.com';
  }
  return `${scheme}://${h}${pathPart}`;
}

function normalizeElectronCookieExpiry(cookie) {
  let raw = cookie.expires;
  if (raw == null || raw === -1) {
    raw = cookie.expirationDate;
  }
  if (raw == null || raw === -1) return null;
  let exp = Number(raw);
  if (!Number.isFinite(exp) || exp <= 0) return null;
  /* Windows FILETIME (100-нс тики с 1601-01-01 UTC) — так отдаёт Chrome Cookie-Editor в ряде версий.
   * Раньше мы отбрасывали такие значения → cookies становились session-only → FB показывал
   * «Продолжить»-picker вместо автологина. */
  if (exp > 1e15) {
    const sec = (exp / 1e7) - 11_644_473_600;
    const nowSec = Date.now() / 1000;
    if (sec > nowSec - 86400 && sec < nowSec + 10 * 365 * 86400) {
      return Math.floor(sec);
    }
    /* Sentinel / за пределами разумного — ставим 2 года вперёд, чтобы cookie был persistent. */
    return Math.floor(nowSec + 2 * 365 * 86400);
  }
  if (exp > 1e12) exp = Math.floor(exp / 1000);
  /* разумный верх (~2100) */
  if (exp > 4102444800) exp = 4102444800;
  return exp;
}

function sanitizeCookieForElectron(cookie) {
  if (!cookie || typeof cookie !== 'object') return null;
  const name = cookie.name;
  const value = cookie.value;
  if (name == null || value == null) return null;

  const out = {
    name: String(name),
    value: String(value),
  };

  const secure = cookie.secure !== false;
  const rawPath = String(cookie.path || '/').trim() || '/';
  const pathPart = rawPath.startsWith('/') ? rawPath : `/${rawPath}`;
  const rawUrl = String(cookie.url || '').trim();
  const rawDomain = String(cookie.domain || '').trim();
  const host = rawDomain.replace(/^\./, '');

  if (rawUrl) {
    try {
      const u = new URL(rawUrl);
      const hn = (u.hostname || '').replace(/^\./, '').toLowerCase();
      /* Явный URL на apex FB — подменим на www, иначе host-only не покроет /messages/ на www. */
      if ((hn === 'facebook.com' || hn === 'm.facebook.com') && u.protocol === 'https:') {
        out.url = `https://www.facebook.com${pathPart}`;
      } else if (hn === 'messenger.com' && u.protocol === 'https:') {
        out.url = `https://www.messenger.com${pathPart}`;
      } else {
        out.url = rawUrl;
      }
    } catch (_e) {
      out.url = rawUrl;
    }
  } else if (host) {
    const built = electronCookieUrlForDomain(host, pathPart, secure);
    if (!built) return null;
    out.url = built;
  } else {
    return null;
  }

  if (rawDomain) out.domain = rawDomain;
  out.path = pathPart;
  out.secure = secure;
  if (cookie.httpOnly != null) out.httpOnly = !!cookie.httpOnly;
  /* FB auth cookies (c_user, xs, fr, datr, sb) в реальности ставятся с SameSite=None; Secure.
   * Купленные аккаунты часто приходят в JSON без поля sameSite — Electron по умолчанию
   * присваивает Lax, и FB потом ломается на «Saved login → Продолжить» (cross-site fetch
   * с Lax cookies не проходит). Секьюрные cookies без явного sameSite → SameSite=None. */
  let sameSite = mapElectronSameSite(cookie.sameSite);
  if (!sameSite && secure) {
    sameSite = 'no_restriction';
  }
  if (sameSite) out.sameSite = sameSite;
  const expNorm = normalizeElectronCookieExpiry(cookie);
  if (expNorm != null) {
    out.expirationDate = expNorm;
  } else {
    /* FB auth cookies без expirationDate → Electron сохраняет как session-only,
     * они не доживают до перезапуска webview, и FB показывает «Saved login»-picker.
     * Для критичных cookie в домене facebook.com/messenger.com ставим 2 года вперёд. */
    const hostLower = String(host || '').toLowerCase();
    const nameLower = String(out.name || '').toLowerCase();
    const isFbDomain =
      hostLower === 'facebook.com' ||
      hostLower === 'messenger.com' ||
      hostLower.endsWith('.facebook.com') ||
      hostLower.endsWith('.messenger.com');
    const isCritical =
      nameLower === 'c_user' ||
      nameLower === 'xs' ||
      nameLower === 'fr' ||
      nameLower === 'datr' ||
      nameLower === 'sb' ||
      nameLower === 'locale' ||
      nameLower === 'wd' ||
      nameLower === 'dpr' ||
      nameLower === 'presence';
    if (isFbDomain && isCritical) {
      out.expirationDate = Math.floor(Date.now() / 1000 + 2 * 365 * 86400);
    }
  }
  /* Сохраняем domain (`.facebook.com`) — важно: host-only cookie на www.facebook.com
   * FB потом перезаписывает своим `.facebook.com` Set-Cookie, получается дубликат с
   * разными value → session «расходится», FB показывает «Saved login» gate. */
  return out;
}

function cookieHost(cookie) {
  if (!cookie) return '';
  const domain = String(cookie.domain || '').trim().replace(/^\./, '');
  if (domain) return domain.toLowerCase();
  try {
    return new URL(String(cookie.url || '')).hostname.toLowerCase();
  } catch (_e) {
    return '';
  }
}

/** Домены сессии Meta: Playwright часто даёт www., graph., m. — плюс fbcdn / facebook.net для части токенов. */
function isFacebookOrMessengerCookieHost(host) {
  const h = String(host || '').toLowerCase();
  return (
    h === 'facebook.com' ||
    h === 'messenger.com' ||
    h === 'fb.com' ||
    h === 'facebook.net' ||
    h === 'fbcdn.net' ||
    h.endsWith('.facebook.com') ||
    h.endsWith('.messenger.com') ||
    h.endsWith('.fb.com') ||
    h.endsWith('.facebook.net') ||
    h.endsWith('.fbcdn.net')
  );
}

const FB_MESSENGER_COOKIE_CLEAR_ORIGINS = [
  'https://www.facebook.com',
  'https://facebook.com',
  'https://m.facebook.com',
  'https://mbasic.facebook.com',
  'https://business.facebook.com',
  'https://www.messenger.com',
  'https://messenger.com',
];

/**
 * Есть ли в Electron-session уже валидный живой вход в Facebook (c_user + xs).
 * Используем, чтобы не затирать свежие cookies из disk profile старыми из БД.
 */
async function sessionHasLiveFacebookAuth(ses) {
  try {
    const all = await ses.cookies.get({ domain: '.facebook.com' });
    if (!Array.isArray(all) || all.length === 0) return false;
    const byName = {};
    for (const c of all) {
      if (c && typeof c.name === 'string') byName[c.name] = String(c.value || '');
    }
    const cUser = (byName.c_user || '').trim();
    const xs = (byName.xs || '').trim();
    return Boolean(cUser && /^\d{6,}$/.test(cUser) && xs);
  } catch (_e) {
    return false;
  }
}

/**
 * Подставить cookies из Playwright storage_state (БД) в Electron Session.
 * @param {{ replaceFacebookCookies?: boolean, skipIfLive?: boolean }} opts
 *   - replaceFacebookCookies: для persist:partition очищаем FB-origins; для session.fromPath(диск с Playwright) только merge.
 *   - skipIfLive: если в session уже есть c_user+xs — пропустить инжект (не затирать свежие cookies, которые FB записал после ручного логина).
 */
async function applyFacebookStorageStateCookies(ses, storageState, opts) {
  const replaceFacebookCookies = !!(opts && opts.replaceFacebookCookies);
  const skipIfLive = !!(opts && opts.skipIfLive);

  if (skipIfLive && (await sessionHasLiveFacebookAuth(ses))) {
    return { applied: 0, rawCount: 0, filteredCount: 0, firstSetErr: '', skippedLive: true };
  }

  if (replaceFacebookCookies) {
    await ses.clearStorageData({
      storages: ['cookies'],
      origins: FB_MESSENGER_COOKIE_CLEAR_ORIGINS,
    });
    /* clearStorageData по origins не всегда удаляет domain-wide cookies (.facebook.com).
     * Проходимся поштучно: берём все FB/Messenger cookies и удаляем по точным URL,
     * чтобы не было зависших host-only или domain дубликатов перед новой заливкой. */
    try {
      const existing = await ses.cookies.get({});
      for (const c of existing) {
        const host = cookieHost(c);
        if (!isFacebookOrMessengerCookieHost(host)) continue;
        const scheme = c.secure === false ? 'http' : 'https';
        const rmHost = String(c.domain || host || '').replace(/^\./, '');
        if (!rmHost) continue;
        const url = `${scheme}://${rmHost === 'facebook.com' ? 'www.facebook.com' : rmHost === 'messenger.com' ? 'www.messenger.com' : rmHost}${c.path || '/'}`;
        try { await ses.cookies.remove(url, c.name); } catch (_e) {}
      }
    } catch (_e) { /* ignore cleanup errors */ }
  }

  const rawCookies = Array.isArray(storageState && storageState.cookies) ? storageState.cookies : [];
  const cookies = rawCookies
    .map((item) => sanitizeCookieForElectron(item))
    .filter((item) => {
      if (!item) return false;
      const host = cookieHost(item);
      return isFacebookOrMessengerCookieHost(host);
    });

  let applied = 0;
  let firstSetErr = '';
  for (const cookie of cookies) {
    try {
      await ses.cookies.set(cookie);
      applied += 1;
    } catch (e) {
      if (!firstSetErr) firstSetErr = String(e && e.message ? e.message : e);
      /* Фоллбек для Electron 33.x: если set упал из-за одновременного url+domain —
       * пробуем БЕЗ url с тем же domain (Electron построит url из domain). */
      if (cookie.url && cookie.domain) {
        const noUrl = Object.assign({}, cookie);
        delete noUrl.url;
        try {
          await ses.cookies.set(noUrl);
          applied += 1;
          continue;
        } catch (_e2) { /* ignore */ }
      }
      /* Второй фоллбек: БЕЗ domain, только url (host-only) — хуже чем domain, но хоть что-то. */
      if (cookie.url && cookie.domain) {
        const noDomain = Object.assign({}, cookie);
        delete noDomain.domain;
        try {
          await ses.cookies.set(noDomain);
          applied += 1;
        } catch (_e3) { /* ignore */ }
      }
    }
  }
  if (rawCookies.length > 0 && applied === 0) {
    console.error(
      '[fb-master-desktop] applyFacebookStorageStateCookies: 0 применено (raw=',
      rawCookies.length,
      'filtered=',
      cookies.length,
      firstSetErr ? ', err: ' + firstSetErr : '',
      ')'
    );
  }
  return { applied, rawCount: rawCookies.length, filteredCount: cookies.length, firstSetErr, skippedLive: false };
}

function mapPlaywrightSameSiteFromElectron(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'strict') return 'Strict';
  if (raw === 'lax') return 'Lax';
  if (raw === 'no_restriction' || raw === 'none' || raw === 'no-restriction') return 'None';
  return undefined;
}

async function exportFacebookStorageStateFromSession(ses) {
  const rawCookies = await ses.cookies.get({});
  const cookies = rawCookies
    .filter((item) => isFacebookOrMessengerCookieHost(cookieHost(item)))
    .map((item) => {
      const out = {
        name: String(item.name || ''),
        value: String(item.value || ''),
        path: String(item.path || '/').trim() || '/',
      };
      const domain = String(item.domain || '').trim();
      if (domain) out.domain = domain;
      if (item.secure != null) out.secure = !!item.secure;
      if (item.httpOnly != null) out.httpOnly = !!item.httpOnly;
      const exp = normalizeElectronCookieExpiry(item);
      if (exp != null) out.expires = exp;
      const sameSite = mapPlaywrightSameSiteFromElectron(item.sameSite);
      if (sameSite) out.sameSite = sameSite;
      return out;
    })
    .filter((item) => item.name && item.value);
  const cUser = (cookies.find((item) => item.name === 'c_user') || {}).value || '';
  return {
    cookies,
    origins: [],
    meta: {
      cookieCount: cookies.length,
      c_user: cUser || '',
    },
  };
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

/** Продакшен-хост (упакованное приложение). Локальная разработка: FB_MASTER_APP_URL или localhost. */
const PACKAGED_APP_BASE_URL = 'https://socmaster.pro/';

/** Кабинет в десктопе — только экран ключа, не корень сайта (иначе редирект на /auth/login). */
function authUnlockUrlFromBase(baseLike) {
  const raw = String(baseLike || '').trim();
  const withSlash = raw.endsWith('/') ? raw : `${raw}/`;
  try {
    return new URL('auth/unlock', withSlash).toString();
  } catch (_e) {
    return `${withSlash.replace(/\/?$/, '/')}auth/unlock`;
  }
}

function workspacesLauncherUrlFromBase(baseUrl) {
  // На embedded-backend (desktop) стартуем сразу с launcher'а multi-workspace
  // shell — cookie workspace сбрасывается параметром reset=1. auth_guard сам
  // перенаправит на /auth/unlock, если сессия ещё не валидна.
  const withSlash = String(baseUrl || '').trim() || '/';
  return `${withSlash.replace(/\/?$/, '/')}workspaces?reset=1`;
}

function defaultStartUrl() {
  const env = (process.env.FB_MASTER_APP_URL || '').trim();
  if (env) {
    return authUnlockUrlFromBase(env);
  }
  if (embeddedBackendBaseUrl) {
    // Desktop-сборка: сразу на launcher, каждый запуск = свежий выбор workspace.
    return workspacesLauncherUrlFromBase(embeddedBackendBaseUrl);
  }
  if (app.isPackaged) {
    // Fallback без embedded backend (socmaster.pro) — там multi-workspace не
    // включён, оставляем исторический старт на /auth/unlock.
    return authUnlockUrlFromBase(PACKAGED_APP_BASE_URL);
  }
  return authUnlockUrlFromBase('http://localhost:8000/');
}

function appServerOrigin() {
  try {
    return new URL(defaultStartUrl()).origin;
  } catch (_e) {
    if (embeddedBackendBaseUrl) {
      try {
        return new URL(embeddedBackendBaseUrl).origin;
      } catch (_e2) {
        /* ignore */
      }
    }
    return app.isPackaged ? 'https://socmaster.pro' : 'http://localhost:8000';
  }
}

function parseDesktopTargetUrl(urlStr) {
  const raw = String(urlStr || '').trim();
  if (!raw) return null;
  try {
    return new URL(raw);
  } catch (_e) {
    return null;
  }
}

function isDesktopOauthSameWindowUrl(urlStr) {
  const u = String(urlStr || '').trim();
  return (
    u.startsWith('https://accounts.google.com') ||
    u.startsWith('https://oauth2.googleapis.com') ||
    /\/supabase\.co\/auth\//i.test(u) ||
    u.startsWith('https://login.microsoftonline.com') ||
    (u.startsWith('https://github.com') && u.includes('/login/oauth'))
  );
}

function isDesktopLoopbackUrl(urlStr) {
  const u = parseDesktopTargetUrl(urlStr);
  if (!u) return false;
  const proto = String(u.protocol || '').toLowerCase();
  const host = String(u.hostname || '').toLowerCase();
  return (proto === 'http:' || proto === 'https:') && (host === '127.0.0.1' || host === 'localhost');
}

function isDesktopAppSameOriginUrl(urlStr) {
  const u = parseDesktopTargetUrl(urlStr);
  if (!u) return false;
  try {
    return u.origin === appServerOrigin();
  } catch (_e) {
    return false;
  }
}

function shouldOpenDesktopUrlExternally(urlStr) {
  const u = parseDesktopTargetUrl(urlStr);
  if (!u) return false;
  const proto = String(u.protocol || '').toLowerCase();
  if (proto !== 'http:' && proto !== 'https:') return false;
  if (isDesktopOauthSameWindowUrl(urlStr)) return false;
  if (isDesktopAppSameOriginUrl(urlStr)) return false;
  if (isDesktopLoopbackUrl(urlStr)) return false;
  return true;
}

/**
 * Манифест обновлений всегда с лицензионного хоста (VPS), не с локального uvicorn —
 * иначе /api/public/desktop-update отдаёт enabled:false (нет FB_DESKTOP_* в локальном .env).
 */
function desktopUpdateManifestOrigin() {
  const envRaw = String(process.env.FB_MASTER_LICENSE_API_BASE || '').trim();
  if (envRaw) {
    try {
      return new URL(envRaw).origin;
    } catch (_e) {
      /* ignore */
    }
  }
  try {
    const resourcesPath = process.resourcesPath;
    if (resourcesPath) {
      const cfgPath = path.join(resourcesPath, 'fb-master-backend', 'launch.json');
      if (fs.existsSync(cfgPath)) {
        const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
        const b = typeof cfg.licenseApiBase === 'string' ? cfg.licenseApiBase.trim() : '';
        if (b) {
          return new URL(b).origin;
        }
      }
    }
  } catch (_e) {
    /* ignore */
  }
  if (app.isPackaged) {
    try {
      return new URL(PACKAGED_APP_BASE_URL).origin;
    } catch (_e) {
      return 'https://socmaster.pro';
    }
  }
  return '';
}

/** Сравнение версий приложения по целым сегментам: «2.14», «2.15», также «2.1.3» (2.1.3 < 2.14). */
function compareSemver(a, b) {
  const pa = String(a || '0')
    .split('.')
    .map((n) => parseInt(n, 10) || 0);
  const pb = String(b || '0')
    .split('.')
    .map((n) => parseInt(n, 10) || 0);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const da = pa[i] || 0;
    const db = pb[i] || 0;
    if (da < db) return -1;
    if (da > db) return 1;
  }
  return 0;
}

function pickDesktopDownloadUrl(urls) {
  if (!urls || typeof urls !== 'object') return null;
  const plat = process.platform;
  const arch = process.arch;
  const key = `${plat}_${arch}`;
  /* Apple Silicon: тот же zip с DMG + инструкцией, что и после оплаты. */
  if (plat === 'darwin' && arch === 'arm64' && urls.darwin_arm64_bundle) {
    return urls.darwin_arm64_bundle;
  }
  if (urls[key]) return urls[key];
  if (plat === 'darwin') {
    if (arch === 'arm64' && urls.darwin_arm64) return urls.darwin_arm64;
    if (urls.darwin_x64) return urls.darwin_x64;
    if (urls.darwin_arm64) return urls.darwin_arm64;
  }
  if (plat === 'win32' && urls.win32_x64) return urls.win32_x64;
  if (plat === 'linux' && urls.linux_x64) return urls.linux_x64;
  const vals = Object.values(urls).filter((x) => typeof x === 'string' && x.trim());
  return vals[0] || null;
}

function parseContentDispositionFilename(header) {
  const cd = String(header || '').trim();
  if (!cd) return '';
  const star = /filename\*\s*=\s*([^']*)''([^;\s]+)/i.exec(cd);
  if (star) {
    try {
      return decodeURIComponent(star[2].replace(/(^"|"$)/g, ''));
    } catch (_e) {
      return star[2];
    }
  }
  const m = /filename\s*=\s*"([^"]+)"/i.exec(cd);
  if (m) return m[1];
  const m2 = /filename\s*=\s*([^;\s]+)/i.exec(cd);
  if (m2) return m2[1].replace(/(^"|"$)/g, '');
  return '';
}

function suggestedFilenameFromDownloadUrl(urlStr) {
  try {
    const u = new URL(urlStr);
    const f = u.searchParams.get('f');
    if (f) {
      const base = path.basename(f.trim());
      if (base) return base;
    }
    const seg = decodeURIComponent(u.pathname.split('/').pop() || '');
    if (seg && seg !== 'release') return seg;
  } catch (_e) {
    /* ignore */
  }
  return '';
}

function downloadArtifactKind(filePath) {
  const lower = String(filePath || '').toLowerCase();
  if (lower.endsWith('.zip')) return 'zip';
  if (lower.endsWith('.dmg')) return 'dmg';
  if (lower.endsWith('.exe')) return 'exe';
  return 'other';
}

/**
 * Скачивание релиза в «Загрузки» с прогрессом.
 * Имя файла: Content-Disposition → query ?f= → иначе тип по Content-Type (путь /download/release без f давал файл «release»).
 */
async function downloadReleaseWithProgress(urlStr, onProgress) {
  const u = String(urlStr || '').trim();
  if (!u) throw new Error('Пустой URL');
  const res = await fetch(u, { redirect: 'follow' });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  const cdName = parseContentDispositionFilename(res.headers.get('content-disposition'));
  let filename = cdName || suggestedFilenameFromDownloadUrl(u);
  if (!filename || filename === 'release') {
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('zip')) filename = 'FbMaster-update.zip';
    else if (ct.includes('x-apple-diskimage') || ct.includes('octet-stream')) {
      filename = 'FbMaster-update.dmg';
    } else filename = 'FbMaster-update.bin';
  }
  filename = path.basename(filename.replace(/[/\\]/g, ''));
  const dest = path.join(app.getPath('downloads'), filename);
  const total = parseInt(res.headers.get('content-length') || '0', 10);
  const body = res.body;
  if (!body) {
    const buf = Buffer.from(await res.arrayBuffer());
    fs.writeFileSync(dest, buf);
    onProgress?.(100);
    return { dest, filename };
  }
  const reader = body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value && value.length) {
      chunks.push(Buffer.from(value));
      received += value.length;
    }
    if (total > 0) {
      onProgress?.(Math.min(99, Math.round((received / total) * 100)));
    } else {
      onProgress?.(null);
    }
  }
  fs.writeFileSync(dest, Buffer.concat(chunks));
  onProgress?.(100);
  return { dest, filename };
}

function openPremiumDesktopUpdateDialog(parentWin, init) {
  return new Promise((resolve) => {
    const dlg = new BrowserWindow({
      width: 620,
      height: 580,
      minWidth: 480,
      minHeight: 460,
      parent: parentWin && !parentWin.isDestroyed() ? parentWin : undefined,
      modal: !!(parentWin && !parentWin.isDestroyed()),
      show: false,
      backgroundColor: '#0c0d10',
      title: 'Обновление SOCMASTER',
      autoHideMenuBar: true,
      maximizable: false,
      webPreferences: {
        preload: path.join(__dirname, 'update-dialog-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    dlg.fbmIsUpdateDialog = true;
    updateDialogCtx = {
      win: dlg,
      url: init.downloadUrl,
      belowMin: init.belowMin,
      latest: init.latest,
      resolve,
    };
    dlg.once('ready-to-show', () => dlg.show());
    dlg.loadFile(path.join(__dirname, 'update-dialog.html'));
    dlg.webContents.once('did-finish-load', () => {
      if (!dlg.isDestroyed()) {
        dlg.webContents.send('update-dialog-init', {
          current: init.current,
          latest: init.latest,
          notes: init.notes,
          belowMin: init.belowMin,
          minVersion: init.minVersion || '',
        });
      }
    });
    dlg.on('closed', () => {
      if (updateDialogCtx && updateDialogCtx.win === dlg) {
        updateDialogCtx.resolve?.({ action: 'closed' });
        updateDialogCtx = null;
      }
    });
  });
}

const DESKTOP_UPDATE_LAST_OFFER_FILE = path.join(userDataPath, 'desktop-update-last-download-offer.json');

function desktopUpdateLastOfferRead() {
  try {
    const raw = fs.readFileSync(DESKTOP_UPDATE_LAST_OFFER_FILE, 'utf8');
    const j = JSON.parse(raw);
    if (j && typeof j.latest === 'string') return j.latest.trim();
  } catch (_e) {
    /* ignore */
  }
  return '';
}

function desktopUpdateLastOfferWrite(latest) {
  const s = String(latest || '').trim();
  if (!s) return;
  try {
    fs.writeFileSync(DESKTOP_UPDATE_LAST_OFFER_FILE, JSON.stringify({ latest: s }), 'utf8');
  } catch (_e) {
    /* ignore */
  }
}

function desktopUpdateLastOfferClear() {
  try {
    fs.unlinkSync(DESKTOP_UPDATE_LAST_OFFER_FILE);
  } catch (_e) {
    /* ignore */
  }
}

/**
 * Централизованное обновление: прод-сервер отдаёт /api/public/desktop-update из .env.
 */
async function maybeCheckDesktopUpdate() {
  let manifestOrigin = desktopUpdateManifestOrigin();
  if (!manifestOrigin) {
    manifestOrigin = appServerOrigin();
  }
  if (/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(manifestOrigin)) {
    if (String(process.env.FB_MASTER_CHECK_LOCAL_UPDATES || '').trim() !== '1') {
      return;
    }
  }
  const current = desktopPackageVersion;
  const manifestUrl = `${manifestOrigin}/api/public/desktop-update`;
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), 15000);
  let res;
  try {
    res = await fetch(manifestUrl, {
      signal: ctrl.signal,
      headers: { Accept: 'application/json' },
    });
  } catch (_e) {
    clearTimeout(tid);
    return;
  }
  clearTimeout(tid);
  if (!res.ok) return;
  let data;
  try {
    data = await res.json();
  } catch (_e) {
    return;
  }
  if (!data || !data.enabled || !data.latest_version) return;
  const latest = String(data.latest_version).trim();
  if (compareSemver(current, latest) >= 0) {
    desktopUpdateLastOfferClear();
    return;
  }
  const lastOffer = desktopUpdateLastOfferRead();
  if (lastOffer && lastOffer === latest) {
    return;
  }
  const dl = pickDesktopDownloadUrl(data.urls);
  if (!dl) return;

  const minV = data.min_version ? String(data.min_version).trim() : '';
  const belowMin = minV && compareSemver(current, minV) < 0;
  const notes = data.release_notes ? String(data.release_notes).slice(0, 800) : '';

  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  await openPremiumDesktopUpdateDialog(win, {
    current,
    latest,
    notes,
    belowMin,
    minVersion: minV || '',
    downloadUrl: dl,
  });
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

/**
 * К запросам на origin сервера приложения добавляем метку SOCMASTER Desktop и железный отпечаток
 * (сервер читает X-FB-Master-Device-Id только вместе с FBMasterDesktop в User-Agent).
 */
function attachDesktopIdentityHeaders(ses) {
  const targetOrigin = appServerOrigin();
  ses.webRequest.onBeforeSendHeaders((details, callback) => {
    try {
      let reqOrigin;
      try {
        reqOrigin = new URL(details.url).origin;
      } catch (_e) {
        return callback({ requestHeaders: details.requestHeaders });
      }
      if (reqOrigin !== targetOrigin) {
        return callback({ requestHeaders: details.requestHeaders });
      }
      const headers = { ...details.requestHeaders };
      const ua = headers['User-Agent'] || '';
      if (ua && !/\bFBMasterDesktop\//i.test(ua)) {
        headers['User-Agent'] = `${ua} FBMasterDesktop/${desktopPackageVersion}`;
      }
      const fp = ensureHardwareDeviceIdHex();
      if (fp) {
        headers['X-FB-Master-Device-Id'] = fp;
      }
      callback({ requestHeaders: headers });
    } catch (_e) {
      callback({ requestHeaders: details.requestHeaders });
    }
  });
}

function startHealthServer() {
  const port = parseInt(process.env.MESSENGER2_FRAME_HEALTH_PORT || '37821', 10);
  const server = http.createServer((req, res) => {
    const p = req.url || '';
    if (p === '/health' || p.startsWith('/health?')) {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ ok: true, app: 'fb-master-desktop' }));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  server.on('error', (err) => {
    console.error('[fb-master-desktop] health server:', err.message);
  });
  server.listen(port, '127.0.0.1', () => {
    console.log('[fb-master-desktop] health http://127.0.0.1:' + port + '/health');
  });
}

ipcMain.handle('fb-desktop:hardware-device-id', () => ({
  ok: true,
  id: ensureHardwareDeviceIdHex(),
}));

ipcMain.handle('update-dialog:secondary', (event) => {
  const ctx = updateDialogCtx;
  if (!ctx || event.sender !== ctx.win.webContents) return { ok: false };
  if (!ctx.win.isDestroyed()) ctx.win.close();
  return { ok: true };
});

ipcMain.handle('update-dialog:close', (event) => {
  const ctx = updateDialogCtx;
  if (!ctx || event.sender !== ctx.win.webContents) return { ok: false };
  if (!ctx.win.isDestroyed()) ctx.win.close();
  return { ok: true };
});

ipcMain.handle('update-dialog:download', (event) => {
  const ctx = updateDialogCtx;
  if (!ctx || event.sender !== ctx.win.webContents) return { ok: false };
  const { win, url } = ctx;
  const offerLatest = ctx.latest;
  void (async () => {
    const sendProgress = (pct) => {
      if (!win.isDestroyed()) {
        win.webContents.send('update-download-progress', { percent: pct });
      }
    };
    try {
      const { dest } = await downloadReleaseWithProgress(url, sendProgress);
      const artifact = downloadArtifactKind(dest);
      if (!win.isDestroyed()) {
        if (process.platform === 'darwin' && artifact === 'dmg') {
          await shell.openPath(dest);
        } else {
          shell.showItemInFolder(dest);
        }
        win.webContents.send('update-download-done', { path: dest, artifact });
        if (offerLatest) {
          desktopUpdateLastOfferWrite(offerLatest);
        }
      }
    } catch (e) {
      console.error('[fb-master-desktop] download update:', e);
      if (!win.isDestroyed()) {
        win.webContents.send('update-download-error', {
          message: String(e && e.message ? e.message : e),
        });
      }
      try {
        await shell.openExternal(url);
      } catch (_e2) {
        /* ignore */
      }
    }
  })();
  return { ok: true };
});


ipcMain.handle('fb-desktop:open-chrome', (_e, { userDataDir, messengerUrl }) => {
  const chrome = defaultChromePath();
  const url = (messengerUrl || 'https://www.facebook.com/messages/e2ee/t').trim();
  if (!userDataDir || typeof userDataDir !== 'string') {
    return { ok: false, error: 'Не указан путь к профилю' };
  }
  const expanded = userDataDir.replace(/^~/, process.env.HOME || '');
  if (!fs.existsSync(expanded)) {
    return { ok: false, error: 'Папка профиля не найдена: ' + expanded };
  }
  try {
    const child = spawn(
      chrome,
      [`--user-data-dir=${expanded}`, url],
      { detached: true, stdio: 'ignore' }
    );
    child.unref();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

/**
 * Chromium session.setProxy ожидает вид вроде http=host:port;https=host:port или socks5=host:port,
 * а не URL с user:pass (даёт ERR_NO_SUPPORTED_PROXIES -336).
 */
function chromiumPartitionProxyRules(rulesUrl) {
  const s = String(rulesUrl || '').trim();
  try {
    const u = new URL(s);
    const host = u.hostname;
    if (!host) return s;
    let port = u.port;
    if (!port) {
      port = u.protocol === 'socks5:' || u.protocol === 'socks4:' ? '1080' : '80';
    }
    const hp = `${host}:${port}`;
    if (u.protocol === 'socks5:') return `socks5=${hp}`;
    if (u.protocol === 'socks4:') return `socks4=${hp}`;
    if (u.protocol === 'http:' || u.protocol === 'https:') {
      return `http=${hp};https=${hp}`;
    }
  } catch (_e) {
    /* ignore */
  }
  return s;
}

ipcMain.handle(
  'fb-desktop:prepare-messenger-disk-profile',
  async (_e, { profilePath, proxyRules, proxyUsername, proxyPassword, storageState }) => {
    try {
      const expanded = String(profilePath || '')
        .trim()
        .replace(/^~/, process.env.HOME || '');
      if (!expanded || !fs.existsSync(expanded)) {
        return { ok: false, error: 'profile_path_not_found' };
      }
      const resolved = path.resolve(expanded);
      if (!fs.statSync(resolved).isDirectory()) {
        return { ok: false, error: 'profile_not_a_directory' };
      }
      const ses = session.fromPath(resolved);
      const u = String(proxyUsername || '').trim();
      const p = String(proxyPassword || '').trim();
      if (u || p) {
        proxyCredentialsBySession.set(ses, { username: u, password: p });
      } else {
        proxyCredentialsBySession.delete(ses);
      }
      const raw = String(proxyRules || '').trim();
      if (raw) {
        const rules = chromiumPartitionProxyRules(raw);
        await ses.setProxy({ proxyRules: rules, proxyBypassRules: '<local>,<-loopback>' });
      } else {
        await ses.setProxy({ mode: 'direct' });
      }
      /*
       * Дисковый режим: подмешиваем снимок из БД через cookies.set (Electron не всегда читает
       * cookies из SQLite, записанные Playwright из‑за шифрования). НО — если в session.fromPath
       * уже есть живой вход (c_user + xs), пропускаем инжект, иначе затрём свежий xs (после
       * ручного логина в окне мессенджера) старым мёртвым из БД и поймаем бесконечный
       * Continue‑gate. clearStorageData не делаем — это общий профиль на диске.
       */
      const cookieMerge = await applyFacebookStorageStateCookies(ses, storageState, {
        replaceFacebookCookies: false,
        skipIfLive: true,
      });
      messengerDiskProfileForWebview = resolved;
      console.log(
        '[fb-master-desktop] prepareMessengerDiskProfile ok',
        resolved,
        cookieMerge.skippedLive
          ? '(disk has live c_user+xs — skipping DB merge)'
          : 'cookies_merged_from_db=' + cookieMerge.applied + '/' + cookieMerge.rawCount
      );
      return {
        ok: true,
        path: resolved,
        cookiesMerged: cookieMerge.applied,
        diskHasLiveAuth: !!cookieMerge.skippedLive,
      };
    } catch (e) {
      messengerDiskProfileForWebview = null;
      return { ok: false, error: String(e && e.message ? e.message : e) };
    }
  }
);

ipcMain.handle(
  'fb-desktop:set-partition-proxy',
  async (_e, { partition, proxyRules, proxyUsername, proxyPassword }) => {
    try {
      const part = String(partition || '').trim();
      const raw = String(proxyRules || '').trim();
      if (!part || !raw) {
        return { ok: false, error: 'empty_partition_or_rules' };
      }
      const ses = session.fromPartition(part);
      const u = String(proxyUsername || '').trim();
      const p = String(proxyPassword || '').trim();
      if (u || p) {
        proxyCredentialsBySession.set(ses, { username: u, password: p });
      } else {
        proxyCredentialsBySession.delete(ses);
      }
      const rules = chromiumPartitionProxyRules(raw);
      await ses.setProxy({
        proxyRules: rules,
        proxyBypassRules: '<local>,<-loopback>',
      });
      return { ok: true, proxyRules: rules };
    } catch (e) {
      return { ok: false, error: String(e && e.message ? e.message : e) };
    }
  }
);

/* Открыть DevTools для конкретного <webview> из renderer — диагностика embedded login. */
ipcMain.handle('fb-desktop:open-webview-devtools', async (event, { webviewId }) => {
  try {
    const parentWc = event.sender;
    if (!parentWc) return { ok: false, error: 'no_sender' };
    const id = String(webviewId || '').trim();
    if (!id) return { ok: false, error: 'no_webview_id' };
    /* Пробуем найти webview через getOwnerBrowserWindow → webContents.getAllWebContents()
     * отфильтруем по parent-owner. WebContents для <webview> в Electron доступны через
     * webContents.hostWebContents === parentWc. */
    const wcAll = require('electron').webContents.getAllWebContents();
    for (const wc of wcAll) {
      try {
        if (typeof wc.hostWebContents !== 'undefined' && wc.hostWebContents && wc.hostWebContents.id === parentWc.id) {
          wc.openDevTools({ mode: 'detach' });
          return { ok: true };
        }
      } catch (_e) { /* skip */ }
    }
    return { ok: false, error: 'webview_not_found' };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

ipcMain.handle('fb-desktop:prepare-webview-session', async (_e, { partition, storageState }) => {
  try {
    const part = String(partition || '').trim();
    if (!part) return { ok: false, error: 'empty_partition' };

    const ses = session.fromPartition(part);
    const { applied } = await applyFacebookStorageStateCookies(ses, storageState, {
      replaceFacebookCookies: true,
    });
    return { ok: true, cookies: applied };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

ipcMain.handle('fb-desktop:export-facebook-session', async (_e, { partition, profilePath }) => {
  try {
    let ses = null;
    const profile = String(profilePath || '')
      .trim()
      .replace(/^~/, process.env.HOME || '');
    const part = String(partition || '').trim();
    if (profile) {
      if (!fs.existsSync(profile)) {
        return { ok: false, error: 'profile_path_not_found' };
      }
      ses = session.fromPath(path.resolve(profile));
    } else if (part) {
      ses = session.fromPartition(part);
    } else {
      return { ok: false, error: 'no_partition_or_profile' };
    }
    const storageState = await exportFacebookStorageStateFromSession(ses);
    return {
      ok: true,
      storage_state: storageState,
      cookie_count: storageState.meta.cookieCount || 0,
      c_user: storageState.meta.c_user || '',
    };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

/** Размер всплывающего окна (оплата / buy) — почти на весь рабочий стол до maximize. */
function largeChildWindowBounds() {
  try {
    const { width, height } = screen.getPrimaryDisplay().workAreaSize;
    return {
      width: Math.max(1100, Math.floor(width * 0.94)),
      height: Math.max(760, Math.floor(height * 0.92)),
    };
  } catch (_e) {
    return { width: 1440, height: 920 };
  }
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: '#f3f6fb',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      webviewTag: true,
      preload: path.join(__dirname, 'preload.js'),
      /* Явный persist: PKCE Supabase OAuth хранит code_verifier в localStorage; должен переживать перезапуск. */
      partition: 'persist:fb-master-backoffice',
    },
    title: 'SOCMASTER',
  });

  attachContextMenu(mainWindow.webContents);

  mainWindow.webContents.on('will-attach-webview', (_event, webPreferences, params) => {
    delete webPreferences.preload;
    webPreferences.nodeIntegration = false;
    webPreferences.contextIsolation = true;
    webPreferences.spellcheck = false;
    const diskPath = messengerDiskProfileForWebview;
    const src = String((params && params.src) || '');
    const explicitPartition = String((params && params.partition) || '').trim();
    const looksMessenger =
      !src ||
      src === 'about:blank' ||
      /facebook\.com|messenger\.com/i.test(src);
    /* Embedded login (Вариант C) задаёт partition="persist:fbm-login-*" — не подменяем session.fromPath,
     * чтобы webview работал в собственном Electron partition и сохранял E2EE ключи у себя. */
    const isEmbeddedLoginPartition = /^persist:fbm-login-/i.test(explicitPartition);
    if (diskPath && looksMessenger && !isEmbeddedLoginPartition && fs.existsSync(diskPath)) {
      try {
        webPreferences.session = session.fromPath(diskPath);
        console.log('[fb-master-desktop] will-attach-webview session.fromPath', diskPath, 'src=', src.slice(0, 80));
      } catch (e) {
        console.error('[fb-master-desktop] session.fromPath (messenger)', e);
      }
    }
    /* UA с тега <webview useragent> — совпадение с карточкой аккаунта; иначе Meta режет /messages/ при тех же cookies. */
    const fromTag = String(params.useragent || '').trim();
    params.useragent = fromTag || desktopMessengerUserAgent();
  });

  mainWindow.webContents.on('did-attach-webview', (_event, guestContents) => {
    try { attachContextMenu(guestContents); } catch (_) {}
    const skipMessengerCookieAutoclick = () => {
      try {
        const u = (guestContents.getURL() || '').toLowerCase();
        /* На /messages/ скрипт с MutationObserver и массовыми кликами ломает React Meta → пустое окно. */
        if (!u || u.startsWith('about:')) return true;
        if (u.includes('/messages')) return true;
        if (/(^|\/\/)((www|m|business)\.)?messenger\.com/i.test(u)) return true;
        return false;
      } catch (_e) {
        return true;
      }
    };
    const normalizeGuest = () => {
      try {
        guestContents.setZoomFactor(1);
      } catch (_e) {
        /* ignore */
      }
      /* Messenger — полная цепочка высоты для flex root; cookie-autoclick только вне чатов. */
      guestContents
        .insertCSS(
          'html, body { width: 100% !important; min-width: 0 !important; height: 100% !important; min-height: 0 !important; margin: 0 !important; }'
        )
        .catch(() => {});
      if (!skipMessengerCookieAutoclick()) {
        guestContents.executeJavaScript(cookieConsentAutoclickScript(), true).catch(() => {});
      }
    };
    guestContents.on('dom-ready', normalizeGuest);
    guestContents.on('did-finish-load', normalizeGuest);
  });

  mainWindow.webContents.on('did-navigate', (_event, url) => {
    try {
      if (!String(url || '').includes('/messenger2')) {
        messengerDiskProfileForWebview = null;
      }
    } catch (_e) {
      /* ignore */
    }
  });

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!shouldOpenDesktopUrlExternally(url)) return;
    event.preventDefault();
    shell.openExternal(String(url || '')).catch(() => {});
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    const u = String(url || '');
    /*
     * OAuth (Google / Supabase) в новом окне: cookie сессии кабинета выставляется не в том webContents —
     * после входа снова редирект на /auth/login. Грузим цепочку в главном окне.
     */
    if (isDesktopOauthSameWindowUrl(u) && mainWindow) {
      mainWindow.loadURL(u);
      return { action: 'deny' };
    }
    if (shouldOpenDesktopUrlExternally(u)) {
      shell.openExternal(u).catch(() => {});
      return { action: 'deny' };
    }
    const allow = isDesktopLoopbackUrl(u) || isDesktopAppSameOriginUrl(u);
    if (!allow) {
      return { action: 'deny' };
    }
    const b = largeChildWindowBounds();
    return {
      action: 'allow',
      overrideBrowserWindowOptions: {
        width: b.width,
        height: b.height,
        minWidth: 1024,
        minHeight: 680,
        backgroundColor: '#f3f6fb',
        webPreferences: {
          nodeIntegration: false,
          contextIsolation: true,
        },
      },
    };
  });

  mainWindow.loadURL(withDesktopClientVersionQuery(defaultStartUrl()));

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

app.on('before-quit', () => {
  stopEmbeddedBackend();
});

/**
 * Упакованное приложение: только локальный uvicorn (127.0.0.1) — Chromium и Playwright на ПК клиента.
 * Без него раньше тихо открывался сайт с VPS → автоматизация на сервере и путь /home/fbmaster/.cache/...
 * Явный отказ: FB_MASTER_ALLOW_REMOTE_FALLBACK=1 (или FB_MASTER_FORCE_REMOTE=1 — без встроенного старта).
 */
async function ensureLocalEmbeddedBackendOrAbort() {
  if (!app.isPackaged) {
    return;
  }
  if (String(process.env.FB_MASTER_FORCE_REMOTE || '').trim() === '1') {
    return;
  }
  try {
    initLauncherLog(userDataPath);
  } catch (_e) {
    /* ignore */
  }
  // Дублируем доп. диагностику в launcher.log — чтобы при тихом провале было что анализировать.
  const logLine = (msg, extra) => {
    try {
      const p = getLauncherLogPath();
      if (!p) return;
      const line =
        '[' + new Date().toISOString() + '] ' + msg +
        (extra !== undefined ? ' ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : '') +
        '\n';
      require('fs').appendFileSync(p, line, { encoding: 'utf8' });
    } catch (_e) {
      /* ignore */
    }
  };
  logLine('main: resourcesPath', process.resourcesPath);
  logLine('main: userDataPath', userDataPath);
  const allowRemote = String(process.env.FB_MASTER_ALLOW_REMOTE_FALLBACK || '').trim() === '1';

  const tryStart = async () => {
    try {
      return await startEmbeddedBackend(process.resourcesPath, { userDataPath });
    } catch (e) {
      console.error('[fb-master] встроенный бэкенд:', e);
      logLine('main tryStart EXCEPTION', {
        message: e && (e.message || String(e)),
        code: e && e.code,
        stack: e && e.stack ? String(e.stack).split('\n').slice(0, 8).join(' | ') : undefined,
      });
      return null;
    }
  };

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (attempt > 1) {
      stopEmbeddedBackend();
    }
    embeddedBackendBaseUrl = await tryStart();
    if (embeddedBackendBaseUrl) {
      return;
    }
    if (allowRemote) {
      console.warn(
        '[fb-master] локальный движок не запущен; откроется сайт (FB_MASTER_ALLOW_REMOTE_FALLBACK=1) — автоматизация может идти на сервере.'
      );
      return;
    }
    const logPath = getLauncherLogPath();
    const hintWin =
      process.platform === 'win32'
        ? '\n\nНа Windows наиболее частая причина — Защитник/антивирус карантинит python.exe или chromium.\n' +
          'Добавьте в исключения папку:\n%LOCALAPPDATA%\\Programs\\socmaster\n' +
          'Если нет Visual C++ Redistributable — установите: https://aka.ms/vs/17/release/vc_redist.x64.exe'
        : '';
    const hintMac =
      process.platform === 'darwin'
        ? '\n\nНа Mac снимите карантин приложения (запустите в Terminal):\nxattr -cr "/Applications/SOCMASTER.app"'
        : '';
    const choice = dialog.showMessageBoxSync({
      type: 'error',
      title: 'SOCMASTER',
      message: 'Не удалось запустить локальный модуль',
      detail:
        'SOCMASTER должен работать на вашем компьютере: Chromium, парсер и рассылка запускаются локально.' +
        hintWin +
        hintMac +
        (logPath ? '\n\nЛог старта:\n' + logPath : '') +
        `\n\nПопытка ${attempt} из 3.`,
      buttons: logPath ? ['Повторить', 'Открыть папку с логами', 'Выход'] : ['Повторить', 'Выход'],
      defaultId: 0,
      cancelId: logPath ? 2 : 1,
    });
    if (logPath && choice === 1) {
      try {
        shell.showItemInFolder(logPath);
      } catch (_e) {
        /* ignore */
      }
      // Показать тот же диалог снова вместо повтора старта
      continue;
    }
    if ((logPath && choice === 2) || (!logPath && choice === 1)) {
      app.quit();
      process.exit(0);
    }
  }
  dialog.showErrorBox(
    'SOCMASTER',
    'Локальный модуль не отвечает. Переустановите приложение из актуального установщика или обратитесь в поддержку.'
  );
  app.quit();
  process.exit(1);
}

app.whenReady().then(async () => {
  /* Всплывающие окна (оплата, внешние ссылки) — не оставляем дефолтный «щелчок» 400×300. */
  app.on('browser-window-created', (_event, win) => {
    queueMicrotask(() => {
      if (!mainWindow || win === mainWindow) return;
      if (win.fbmIsUpdateDialog) return;
      const maximizeOnce = () => {
        try {
          if (!win.isDestroyed()) win.maximize();
        } catch (_e) {
          /* ignore */
        }
      };
      win.once('ready-to-show', maximizeOnce);
      setTimeout(() => {
        try {
          if (!win.isDestroyed() && !win.isMaximized()) maximizeOnce();
        } catch (_e) {
          /* ignore */
        }
      }, 500);
    });
  });

  await ensureLocalEmbeddedBackendOrAbort();
  const backofficePartition = 'persist:fb-master-backoffice';
  attachDesktopIdentityHeaders(session.fromPartition(backofficePartition));
  attachDesktopIdentityHeaders(session.defaultSession);
  startHealthServer();
  createMainWindow();

  // 3.0+: License watcher trigger при появлении интернета и при старте.
  // Best-effort POST на loopback /internal/license/recheck-now — будит
  // фоновый watcher, чтобы не ждать 30-секундный цикл.
  function triggerLicenseRecheck(reason) {
    if (!embeddedBackendBaseUrl) return;
    try {
      const url = embeddedBackendBaseUrl.replace(/\/$/, '') + '/internal/license/recheck-now';
      const http = require('http');
      const u = new URL(url);
      const req = http.request(
        {
          method: 'POST',
          hostname: u.hostname,
          port: u.port,
          path: u.pathname,
          headers: { 'content-type': 'application/json', 'content-length': '0' },
          timeout: 3000,
        },
        (res) => { res.on('data', () => {}); res.on('end', () => {}); }
      );
      req.on('error', () => {});
      req.end();
      console.log('[fb-master] license recheck triggered:', reason || 'startup');
    } catch (_e) { /* ignore */ }
  }
  // Старт.
  setTimeout(() => triggerLicenseRecheck('startup'), 2000);
  // Вернулся интернет (опрос net.isOnline каждые 15 сек).
  try {
    const { net, powerMonitor } = require('electron');
    if (net && typeof net.isOnline === 'function') {
      let wasOnline = false;
      setInterval(() => {
        try {
          const online = net.isOnline();
          if (online && !wasOnline) {
            triggerLicenseRecheck('back_online');
          }
          wasOnline = online;
        } catch (_e) { /* ignore */ }
      }, 15000);
    }
    if (powerMonitor && powerMonitor.on) {
      powerMonitor.on('resume', () => triggerLicenseRecheck('resume_from_sleep'));
    }
  } catch (_e) { /* ignore */ }

  setTimeout(() => {
    maybeCheckDesktopUpdate().catch(() => {});
  }, 4000);
});

app.on('window-all-closed', () => app.quit());
