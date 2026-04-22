'use strict';

/**
 * Запускает вложенный Python uvicorn (полный FB Master) из Resources/fb-master-backend.
 * Playwright, прогрев и сценарии выполняются на машине клиента, а не на VPS.
 * См. LOCAL_BACKEND.md и scripts/prepare-desktop-backend-bundle.sh
 */

const crypto = require('crypto');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

let child = null;
let launcherLogPath = null;

/**
 * Постоянный лог старта локального бэкенда. На упакованной сборке console.error
 * невидим клиенту — без файлового лога его невозможно диагностировать удалённо.
 * Windows: %APPDATA%\SOCMASTER\launcher.log
 * macOS:   ~/Library/Application Support/SOCMASTER/launcher.log
 */
function initLauncherLog(userDataPath) {
  try {
    const dir = userDataPath || path.join(os.homedir(), '.socmaster');
    fs.mkdirSync(dir, { recursive: true });
    launcherLogPath = path.join(dir, 'launcher.log');
    // Ротация: если файл больше 2 MB — переименовываем в .old
    try {
      const st = fs.statSync(launcherLogPath);
      if (st && st.size > 2 * 1024 * 1024) {
        fs.renameSync(launcherLogPath, launcherLogPath + '.old');
      }
    } catch (_e) {
      /* нет файла — ОК */
    }
    launchLog('==== SOCMASTER launcher start', {
      version: readDesktopAppVersion(),
      platform: process.platform,
      arch: process.arch,
      node: process.version,
      cwd: process.cwd(),
    });
  } catch (e) {
    console.error('[fb-master] cannot init launcher.log:', e && e.message);
  }
}

function launchLog(msg, extra) {
  const line =
    '[' + new Date().toISOString() + '] ' + msg +
    (extra ? ' ' + (typeof extra === 'string' ? extra : JSON.stringify(extra)) : '') +
    '\n';
  try {
    if (launcherLogPath) {
      fs.appendFileSync(launcherLogPath, line, { encoding: 'utf8' });
    }
  } catch (_e) {
    /* ignore */
  }
  try {
    process.stderr.write(line);
  } catch (_e) {
    /* ignore */
  }
}

function getLauncherLogPath() {
  return launcherLogPath;
}

/**
 * Один и тот же SECRET_KEY между запусками приложения — иначе cookie сессии недействительна,
 * пользователь снова видит ввод ключа, хотя база (аккаунты) на диске цела.
 */
function applyPersistentSessionSecret(dataDir, env) {
  const cur = String(env.SECRET_KEY || '').trim();
  if (cur && cur !== 'dev-secret-change-me') {
    return;
  }
  try {
    fs.mkdirSync(dataDir, { recursive: true });
    const secretPath = path.join(dataDir, '.fb_master_session_secret');
    let secret = '';
    if (fs.existsSync(secretPath)) {
      secret = fs.readFileSync(secretPath, 'utf8').trim();
    }
    if (secret.length < 24) {
      secret = crypto.randomBytes(32).toString('base64url');
      fs.writeFileSync(secretPath, `${secret}\n`, { encoding: 'utf8', mode: 0o600 });
    }
    env.SECRET_KEY = secret;
  } catch (e) {
    console.error('[fb-master] persistent session secret:', e.message || e);
  }
}

function readDesktopAppVersion() {
  try {
    const p = path.join(__dirname, 'package.json');
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const v = String((j && j.version) || '').trim();
    return v || '';
  } catch (_e) {
    return '';
  }
}

function readLaunchConfig(resourcesPath) {
  const root = path.join(resourcesPath, 'fb-master-backend');
  const cfgPath = path.join(root, 'launch.json');
  if (!fs.existsSync(cfgPath)) {
    try {
      launchLog('readLaunchConfig FAIL: нет launch.json', { cfgPath, root, rootExists: fs.existsSync(root) });
      if (fs.existsSync(root)) {
        try {
          const contents = fs.readdirSync(root).slice(0, 50);
          launchLog('fb-master-backend содержимое', contents);
        } catch (e) {
          launchLog('readdirSync error', e && e.message);
        }
      }
    } catch (_e) {
      /* ignore */
    }
    return null;
  }
  try {
    let raw = fs.readFileSync(cfgPath, 'utf8');
    // Windows PowerShell 5.x `Out-File -Encoding utf8` добавляет BOM (\uFEFF) — JSON.parse
    // на этом кидает SyntaxError. Срезаем BOM до парсинга на любой платформе.
    if (raw.charCodeAt(0) === 0xFEFF) {
      raw = raw.slice(1);
      launchLog('readLaunchConfig: обнаружен и удалён BOM в launch.json');
    }
    const cfg = JSON.parse(raw);
    if (!cfg || typeof cfg !== 'object') {
      launchLog('readLaunchConfig FAIL: launch.json пустой/не объект', { size: raw.length });
      return null;
    }
    launchLog('readLaunchConfig OK', { executable: cfg.executable, port: cfg.port, appModule: cfg.appModule });
    return { root, ...cfg };
  } catch (e) {
    launchLog('readLaunchConfig FAIL: JSON parse error', e && (e.message || String(e)));
    return null;
  }
}

/**
 * macOS: переносимый CPython внутри бандла (см. scripts/ensure-macos-embedded-python.sh).
 * Старый venv с симлинком на /opt/homebrew/... на машине клиента не работает.
 */
function resolvePortableMacPython(cfgRoot) {
  const marker = path.join(cfgRoot, '.embedded_python');
  if (!fs.existsSync(marker)) {
    return null;
  }
  if (process.platform === 'win32') {
    /* Windows: python-build-standalone кладёт python.exe в корень python/,
     * site-packages — в Lib\site-packages (Lib с большой буквы). */
    const pyWin = path.join(cfgRoot, 'python', 'python.exe');
    if (!fs.existsSync(pyWin)) {
      console.error('[fb-master] есть .embedded_python, но нет интерпретатора:', pyWin);
      return null;
    }
    const libSiteWin = path.join(cfgRoot, 'python-lib', 'Lib', 'site-packages');
    const rtSiteWin = path.join(cfgRoot, 'python', 'Lib', 'site-packages');
    const partsWin = [libSiteWin, rtSiteWin].filter((p) => fs.existsSync(p));
    return { python: pyWin, pythonPath: partsWin.join(path.delimiter) };
  }
  /* macOS: python в python/bin/python3.12, site-packages в lib/python3.12/site-packages */
  const py = path.join(cfgRoot, 'python', 'bin', 'python3.12');
  if (!fs.existsSync(py)) {
    console.error('[fb-master] есть .embedded_python, но нет интерпретатора:', py);
    return null;
  }
  const libSite = path.join(cfgRoot, 'python-lib', 'lib', 'python3.12', 'site-packages');
  const rtSite = path.join(cfgRoot, 'python', 'lib', 'python3.12', 'site-packages');
  const parts = [libSite, rtSite].filter((p) => fs.existsSync(p));
  return { python: py, pythonPath: parts.join(path.delimiter) };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function reserveLoopbackPort(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.on('error', () => resolve(null));
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => {
      const addr = server.address();
      const chosenPort = addr && typeof addr === 'object' ? addr.port : null;
      server.close(() => resolve(chosenPort));
    });
  });
}

async function pickEmbeddedBackendPort(preferredPort) {
  const preferred = parseInt(String(preferredPort || '8799'), 10) || 8799;
  const exact = await reserveLoopbackPort(preferred);
  if (exact === preferred) {
    return preferred;
  }
  const fallback = await reserveLoopbackPort(0);
  if (fallback && fallback > 0) {
    console.warn(
      '[fb-master] локальный порт',
      preferred,
      'уже занят; используем свободный порт',
      fallback
    );
    return fallback;
  }
  return preferred;
}

async function waitHealth(port, timeoutMs) {
  const url = `http://127.0.0.1:${port}/health`;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) {
        return true;
      }
    } catch (_e) {
      /* ещё не поднялся */
    }
    await sleep(400);
  }
  return false;
}

/**
 * @param {string} resourcesPath — process.resourcesPath (внутри .app: Contents/Resources)
 * @param {{ userDataPath: string }} opts
 * @returns {Promise<string|null>} базовый URL с завершающим / или null
 */
async function startEmbeddedBackend(resourcesPath, opts) {
  launchLog('startEmbeddedBackend called', { resourcesPath });
  if (process.env.FB_MASTER_FORCE_REMOTE === '1') {
    launchLog('FB_MASTER_FORCE_REMOTE=1 → не запускаю локальный бэкенд');
    return null;
  }
  const cfg = readLaunchConfig(resourcesPath);
  if (!cfg) {
    launchLog('startEmbeddedBackend: readLaunchConfig вернул null → выход');
    return null;
  }
  launchLog('cfg.root', cfg.root);
  const port = await pickEmbeddedBackendPort(cfg.port || '8799');
  const win = process.platform === 'win32';

  // Pre-flight checks: без нужных файлов python.exe не стартует и клиент
  // увидит generic «не удалось запустить». Лучше заранее показать, чего не хватает.
  const expectedFiles = win
    ? [
        path.join(cfg.root, 'python', 'python.exe'),
        path.join(cfg.root, 'main.py'),
      ]
    : [
        path.join(cfg.root, 'main.py'),
      ];
  const missing = expectedFiles.filter((p) => !fs.existsSync(p));
  if (missing.length) {
    launchLog('pre-flight FAIL: отсутствуют файлы', missing);
    launchLog('HINT: скорее всего антивирус удалил файлы из установки. Добавьте папку приложения в исключения и переустановите.');
    return null;
  }
  const chromiumDir = path.join(cfg.root, 'playwright-browsers');
  if (!fs.existsSync(chromiumDir)) {
    launchLog('pre-flight WARN: нет папки playwright-browsers', chromiumDir);
  }

  const portable = resolvePortableMacPython(cfg.root);
  let pythonPathExtra = '';
  let python;
  if (portable) {
    python = portable.python;
    pythonPathExtra = portable.pythonPath || '';
  } else {
    const pythonRel =
      cfg.executable || (win ? 'venv\\Scripts\\python.exe' : 'venv/bin/python3');
    python = path.isAbsolute(pythonRel) ? pythonRel : path.join(cfg.root, pythonRel);
    if (!fs.existsSync(python)) {
      const fallbackWin = path.join(cfg.root, 'venv', 'Scripts', 'python.exe');
      const fallbackUnix = path.join(cfg.root, 'venv', 'bin', 'python3');
      if (win && fs.existsSync(fallbackWin)) {
        python = fallbackWin;
      } else if (!win && fs.existsSync(fallbackUnix)) {
        python = fallbackUnix;
      }
    }
  }
  if (!fs.existsSync(python)) {
    console.error('[fb-master] локальный бэкенд: не найден интерпретатор:', python);
    return null;
  }
  const appModule = cfg.appModule || 'main:app';
  const dataDir =
    opts && opts.userDataPath
      ? path.join(opts.userDataPath, 'fb-master-local-data')
      : path.join(cfg.root, 'data');
  try {
    fs.mkdirSync(dataDir, { recursive: true });
  } catch (_e) {
    /* ignore */
  }
  const licenseRaw =
    String(process.env.FB_MASTER_LICENSE_API_BASE || '').trim() ||
    (typeof cfg.licenseApiBase === 'string' ? cfg.licenseApiBase.trim() : '') ||
    'https://socmaster.pro';
  const licenseBase = licenseRaw.replace(/\/+$/, '');
  const browsersPath = path.join(cfg.root, 'playwright-browsers');
  const desktopVer = readDesktopAppVersion();
  const env = {
    ...process.env,
    BO_HOST: '127.0.0.1',
    BO_PORT: String(port),
    PYTHONUNBUFFERED: '1',
    DATA_DIR: dataDir,
    /* Локальный режим: не подставлять прод-URL из .env сборки */
    APP_BASE_URL: `http://127.0.0.1:${port}`,
    /* Активация ключа на VPS; автоматизация — в этом локальном процессе */
    FB_MASTER_LICENSE_API_BASE: licenseBase,
    /* Chromium рядом с бэкендом (см. prepare-скрипт + playwright install) */
    PLAYWRIGHT_BROWSERS_PATH: browsersPath,
    /* Подпись версии в шаблонах (сайдбар, /auth/unlock) */
    ...(desktopVer ? { FB_DESKTOP_APP_VERSION: desktopVer } : {}),
    /*
     * Рассылка и встроенный Messenger делят один дисковый профиль Chromium: mutex в БД
     * и блокировка UI, пока job держит lock (см. outreach_shared_profile).
     */
    FB_MASTER_OUTREACH_SHARED_PROFILE: '1',
  };
  /*
   * Desktop-клиент должен жить на локальной SQLite. Если в shell/системе случайно
   * задан DATABASE_URL/SUPABASE_DATABASE_URL, backend уходит в чужой Postgres и у
   * клиента появляются машинозависимые 500/ошибки старта.
   */
  env.DATABASE_URL = '';
  env.SUPABASE_DATABASE_URL = '';
  if (cfg.env && typeof cfg.env === 'object') {
    Object.assign(env, cfg.env);
  }
  /* Всегда бандл рядом с бэкендом; не даём launch.json/.env процесса обнулить путь → ~/.cache/ms-playwright */
  env.PLAYWRIGHT_BROWSERS_PATH = browsersPath;
  if (pythonPathExtra) {
    env.PYTHONPATH = pythonPathExtra;
  }
  /*
   * DMG/EXE: ключ обязателен, иначе пользователь видит /auth/login (Google/пароль) вместо экрана ключа.
   * Раньше хватало «пустого» FB_MASTER_ACCESS_KEY_REQUIRED — но строка "false" из process.env/.env
   * не пустая, и режим ключа выключался. Всегда включаем «1», кроме явной отладки EMBEDDED_NO_ACCESS_KEY.
   */
  const embeddedNoAccessKey =
    String(process.env.FB_MASTER_EMBEDDED_NO_ACCESS_KEY || '').trim() === '1';
  if (!embeddedNoAccessKey) {
    env.FB_MASTER_ACCESS_KEY_REQUIRED = '1';
  }
  const akOn = ['1', 'true', 'yes'].includes(
    String(env.FB_MASTER_ACCESS_KEY_REQUIRED || '').trim().toLowerCase()
  );
  const opRaw = String(env.FB_MASTER_OPERATOR_AFTER_ACCESS_KEY || '').trim();
  if (akOn && !opRaw && !embeddedNoAccessKey) {
    env.FB_MASTER_OPERATOR_AFTER_ACCESS_KEY = '1';
  }
  applyPersistentSessionSecret(dataDir, env);
  /* Один пользователь на ПК — без кнопки «Выйти» (включить: FB_MASTER_HIDE_LOGOUT=0 в launch.json env). */
  if (!String(env.FB_MASTER_HIDE_LOGOUT || '').trim()) {
    env.FB_MASTER_HIDE_LOGOUT = '1';
  }
  /* Клиент не видит в «Настройках» Supabase/OAuth/пути к данным (отладка: FB_MASTER_HIDE_INFRA_SETTINGS=0). */
  if (!String(env.FB_MASTER_HIDE_INFRA_SETTINGS || '').trim()) {
    env.FB_MASTER_HIDE_INFRA_SETTINGS = '1';
  }
  /*
   * Multi-workspace shell + Reddit Master: в desktop-сборке включаем по умолчанию.
   * На VPS (socmaster.pro) флаги остаются невыставленными (default=0) — там только FB.
   * Отключить: FB_MASTER_MULTI_WORKSPACE_ENABLED=0 / FB_MASTER_REDDIT_MODULE_ENABLED=0 в launch.json env.
   */
  if (!String(env.FB_MASTER_MULTI_WORKSPACE_ENABLED || '').trim()) {
    env.FB_MASTER_MULTI_WORKSPACE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_REDDIT_MODULE_ENABLED || '').trim()) {
    env.FB_MASTER_REDDIT_MODULE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_REDDIT_OFFICIAL_API_ONLY || '').trim()) {
    env.FB_MASTER_REDDIT_OFFICIAL_API_ONLY = '1';
  }
  if (!String(env.FB_MASTER_REDDIT_SEND_APPROVAL || '').trim()) {
    env.FB_MASTER_REDDIT_SEND_APPROVAL = 'manual';
  }
  /*
   * LinkedIn Master (Beta): в desktop-сборке включаем по умолчанию.
   * На VPS флаги остаются default=0 — там только FB.
   * Отключить локально: FB_MASTER_LINKEDIN_MODULE_ENABLED=0.
   */
  if (!String(env.FB_MASTER_LINKEDIN_MODULE_ENABLED || '').trim()) {
    env.FB_MASTER_LINKEDIN_MODULE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_LINKEDIN_OFFICIAL_API_ONLY || '').trim()) {
    env.FB_MASTER_LINKEDIN_OFFICIAL_API_ONLY = '1';
  }
  if (!String(env.FB_MASTER_LINKEDIN_SEND_APPROVAL || '').trim()) {
    env.FB_MASTER_LINKEDIN_SEND_APPROVAL = 'manual';
  }
  /*
   * Twitter / X Master (Beta): в desktop-сборке включаем по умолчанию.
   * На VPS флаги остаются default=0 — там только FB.
   */
  if (!String(env.FB_MASTER_TWITTER_MODULE_ENABLED || '').trim()) {
    env.FB_MASTER_TWITTER_MODULE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_TWITTER_BROWSER_PROFILE_ENABLED || '').trim()) {
    env.FB_MASTER_TWITTER_BROWSER_PROFILE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_TWITTER_SEND_APPROVAL || '').trim()) {
    env.FB_MASTER_TWITTER_SEND_APPROVAL = 'manual';
  }
  /*
   * Instagram Master (Beta).
   */
  if (!String(env.FB_MASTER_INSTAGRAM_MODULE_ENABLED || '').trim()) {
    env.FB_MASTER_INSTAGRAM_MODULE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_INSTAGRAM_BROWSER_PROFILE_ENABLED || '').trim()) {
    env.FB_MASTER_INSTAGRAM_BROWSER_PROFILE_ENABLED = '1';
  }
  if (!String(env.FB_MASTER_INSTAGRAM_SEND_APPROVAL || '').trim()) {
    env.FB_MASTER_INSTAGRAM_SEND_APPROVAL = 'manual';
  }
  if (!String(env.FB_MASTER_LAUNCHER_DEFAULT || '').trim()) {
    env.FB_MASTER_LAUNCHER_DEFAULT = '1';
  }
  const args = ['-m', 'uvicorn', appModule, '--host', '127.0.0.1', '--port', String(port)];
  launchLog('spawn uvicorn', { python, cwd: cfg.root, port, appModule });
  child = spawn(python, args, {
    cwd: cfg.root,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const trimLog = (buf, n) => buf.toString().replace(/\s+$/, '').slice(0, n);
  child.stderr?.on('data', (buf) => {
    const s = trimLog(buf, 2000);
    if (s) {
      console.error('[fb-master-backend]', s);
      launchLog('stderr', s);
    }
  });
  child.stdout?.on('data', (buf) => {
    const s = trimLog(buf, 1000);
    if (s) {
      console.log('[fb-master-backend]', s);
      launchLog('stdout', s);
    }
  });
  child.on('error', (err) => {
    console.error('[fb-master] локальный бэкенд spawn:', err.message || err);
    launchLog('spawn error', err && (err.message || String(err)));
  });
  child.on('exit', (code, signal) => {
    launchLog('uvicorn exited', { code, signal });
  });

  const timeoutMs = parseInt(String(cfg.startTimeoutMs || '120000'), 10) || 120000;
  const ok = await waitHealth(port, timeoutMs);
  if (!ok) {
    console.error('[fb-master] локальный бэкенд не ответил на /health за', timeoutMs, 'мс');
    launchLog('FAIL: /health не ответил за ' + timeoutMs + 'мс; вероятнее всего python упал при старте (stderr выше) или antivirus блокирует.');
    try {
      child.kill(win ? undefined : 'SIGTERM');
    } catch (_e) {
      /* ignore */
    }
    child = null;
    return null;
  }
  launchLog('OK: uvicorn готов на порту ' + port);
  return `http://127.0.0.1:${port}/`;
}

function stopEmbeddedBackend() {
  if (!child) {
    return;
  }
  try {
    if (process.platform === 'win32') {
      child.kill();
    } else {
      child.kill('SIGTERM');
    }
  } catch (_e) {
    /* ignore */
  }
  child = null;
}

module.exports = {
  startEmbeddedBackend,
  stopEmbeddedBackend,
  initLauncherLog,
  getLauncherLogPath,
};
