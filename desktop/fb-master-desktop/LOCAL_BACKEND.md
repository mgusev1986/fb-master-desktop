# Локальный бэкенд в десктопе (всё на ПК клиента)

Установщик с папкой `resources-fb-master-backend` поднимает **uvicorn на 127.0.0.1**: прогрев, сценарии, парсер, мессенджер-воркеры и Playwright работают **на компьютере пользователя**.

## VPS

- Сайт, оплата, выдача ключей.
- В `.env` на сервере: **`FB_MASTER_DISABLE_PLAYWRIGHT_WORKERS=1`** — на VPS не стартуют фоновые Playwright-задачи.
- Эндпоинт **`POST /api/public/desktop-license/activate`** — активация ключа для десктопа (привязка устройства в БД на сервере).

## Десктоп

- В `launch.json` поле **`licenseApiBase`**: ваш домен (`https://socmaster.pro`). Локальный Python получает **`FB_MASTER_LICENSE_API_BASE`** и при вводе ключа обращается к VPS, затем пишет зеркало ключа в локальную SQLite.
- Лаунчер принудительно очищает **`DATABASE_URL`** / **`SUPABASE_DATABASE_URL`**: desktop-клиент не должен подхватывать внешний Postgres из shell/.env и обязан работать на локальной SQLite.
- Chromium для Playwright кладётся в **`playwright-browsers/`** внутри бандла (`PLAYWRIGHT_BROWSERS_PATH`), чтобы не зависеть от кэша в домашней папке.
- Данные и сессия: **`DATA_DIR`** = `…/fb-master-local-data` в каталоге userData приложения (SQLite, аккаунты, выгрузки). Файл **`.fb_master_session_secret`** там же хранит постоянный **`SECRET_KEY`**, чтобы после перезапуска приложения cookie сессии оставалась действительной (иначе снова просит ключ при той же базе).
- Кнопка **«Выйти»** в сайдбаре по умолчанию скрыта (**`FB_MASTER_HIDE_LOGOUT=1`**). Вернуть для отладки: в `launch.json` → `env` задайте **`FB_MASTER_HIDE_LOGOUT=0`**.
- Если локальный uvicorn не поднялся, приложение **не открывает сайт с VPS по умолчанию** (иначе Chromium «уезжает» на сервер). Для редкой отладки: переменная окружения **`FB_MASTER_ALLOW_REMOTE_FALLBACK=1`** — тогда возможен старый сценарий с веб-интерфейсом с домена. **`FB_MASTER_FORCE_REMOTE=1`** — вообще не стартовать встроенный бэкенд.

## Сборка

**Chromium входит в установщик:** каталог `resources-fb-master-backend/playwright-browsers/` копируется в `.app` / `.exe` как `extraResources` → `fb-master-backend`. Лаунчер задаёт **`PLAYWRIGHT_BROWSERS_PATH`** на эту папку — отдельно ставить Chrome/Chromium клиенту не нужно.

**macOS:** `prepare-desktop-backend-bundle.sh` сам кладёт **переносимый Python 3.12** (релиз [python-build-standalone](https://github.com/indygreg/python-build-standalone)), ставит зависимости в `python-lib/` и **удаляет `venv`**. Старый вариант с `venv`, указывающим на `/opt/homebrew/...`, на компьютерах клиентов **ломал** локальный uvicorn (диалог «Не удалось запустить локальный модуль»).

```bash
./scripts/prepare-desktop-backend-bundle.sh   # сеть: скачивание Python + pip + Chromium
cd desktop/fb-master-desktop && npm run dist:mac:arm64
```

**Linux / сборка Windows:** по-прежнему создавайте `venv` в `resources-fb-master-backend` вручную и снова запускайте `prepare-desktop-backend-bundle.sh` (ветка без Darwin).

Для **Intel Mac**: `npm run dist:mac:x64` — запускайте **`prepare-desktop-backend-bundle.sh` на Intel Mac** (подтянется x86_64-сборка встроенного Python и Chromium).

### Windows 64-bit (x64, «Intel»-совместимые ПК)

Сборка **полностью локального** установщика делается **на Windows** (или в VM/CI с Windows): внутрь кладётся свой `venv` и свой Chromium Playwright — их нельзя перенести с macOS/Linux.

1. Клонировать репозиторий, установить **Node.js**, **Python 3.12** для Windows.
2. Из корня репозитория обновить бандл бэкенда (Git Bash / WSL / macOS/Linux):
   `./scripts/prepare-desktop-backend-bundle.sh`
   (на чистом Windows без Bash — выполните те же копирования вручную по скрипту.)
3. В PowerShell:
   ```powershell
   cd desktop\fb-master-desktop\resources-fb-master-backend
   py -3.12 -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   $env:PLAYWRIGHT_BROWSERS_PATH = "$(Get-Location)\playwright-browsers"
   New-Item -ItemType Directory -Force -Path $env:PLAYWRIGHT_BROWSERS_PATH | Out-Null
   .\venv\Scripts\playwright.exe install chromium
   cd ..\..
   npm install
   npm run dist:win
   ```
4. Готовый установщик NSIS: `desktop/fb-master-desktop/dist/FbMaster-<версия>-win-x64.exe` (имя задаётся в `package.json`, поле `artifactName`).

**С macOS:** `npm run dist:win` может собрать **оболочку** Electron под Windows, но папка `resources-fb-master-backend` с **macOS-venv и mac-Chromium** на целевом ПК с Windows работать не будет. Для продакшена Windows-билд делайте на Windows.

## Облако принудительно

Переменная **`FB_MASTER_FORCE_REMOTE=1`** — отключить встроенный бэкенд и открыть только сайт.

## Ключ доступа при старте (клиентский DMG)

Лаунчер **всегда** выставляет **`FB_MASTER_ACCESS_KEY_REQUIRED=1`** (и при необходимости **`FB_MASTER_OPERATOR_AFTER_ACCESS_KEY=1`**), кроме режима отладки **`FB_MASTER_EMBEDDED_NO_ACCESS_KEY=1`** — иначе из `process.env`/`.env` могло протечь значение `false`, и приложение открывало **`/auth/login`** вместо тёмного экрана **«Ключ доступа»**. Окно Electron грузит сразу **`/auth/unlock`**. Отключить автологин оператора после ключа: в `launch.json` → `env` задайте **`FB_MASTER_OPERATOR_AFTER_ACCESS_KEY=0`**.
