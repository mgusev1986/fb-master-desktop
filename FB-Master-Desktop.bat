@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "FB_MASTER_AUTOSTART_DESKTOP=0"
set "FB_MASTER_AUTOSTART_MESSENGER2_FRAME=0"

if not exist ".venv\Scripts\python.exe" (
  echo Нет .venv — создайте окружение и pip install -r requirements.txt
  pause
  exit /b 1
)

where npm >nul 2>&1
if errorlevel 1 (
  echo Установите Node.js LTS с https://nodejs.org
  pause
  exit /b 1
)

if not exist "desktop\fb-master-desktop\main.js" (
  echo Нет desktop\fb-master-desktop\main.js
  pause
  exit /b 1
)

for /f "delims=" %%P in ('.venv\Scripts\python.exe -c "from backend.config import BO_PORT; print(BO_PORT)"') do set "PORT=%%P"
for /f "delims=" %%H in ('.venv\Scripts\python.exe -c "from backend.config import APP_BASE_URL; u=APP_BASE_URL.rstrip('/'); print(u+'/auth/login')"') do set "LOGIN_URL=%%H"
for /f "delims=" %%A in ('.venv\Scripts\python.exe -c "from backend.config import APP_BASE_URL; u=APP_BASE_URL.rstrip('/'); print(u+'/')"') do set "FB_MASTER_APP_URL=%%A"

echo Порт %PORT%, вход %LOGIN_URL%
echo Запуск сервера...
start "FB Master Back-office" cmd /k "cd /d ""%~dp0"" && .venv\Scripts\activate.bat && set FB_MASTER_AUTOSTART_DESKTOP=0&& set FB_MASTER_AUTOSTART_MESSENGER2_FRAME=0&& set BO_PORT=%PORT%&& python main.py"

echo Ожидание сервера...
.venv\Scripts\python.exe scripts\wait_url_ready.py "%LOGIN_URL%" 60
if errorlevel 1 (
  echo Сервер не ответил. Проверьте окно Back-office.
  pause
  exit /b 1
)

cd desktop\fb-master-desktop
if not exist node_modules npm install
if not defined MESSENGER2_FRAME_HEALTH_PORT set "MESSENGER2_FRAME_HEALTH_PORT=37821"
echo Запуск FB Master Desktop... FB_MASTER_APP_URL=%FB_MASTER_APP_URL%
call npm start
cd /d "%~dp0"
echo Окно Electron закрыто. Остановите сервер в окне Back-office ^(Ctrl+C^) при необходимости.
pause
