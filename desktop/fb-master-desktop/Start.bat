@echo off
cd /d "%~dp0"
if not exist node_modules npm install
if not defined FB_MASTER_APP_URL set FB_MASTER_APP_URL=http://127.0.0.1:8000/
npm start
