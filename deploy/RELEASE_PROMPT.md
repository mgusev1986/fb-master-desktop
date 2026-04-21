# Промт для полного релиза SOCMASTER (macOS + Windows)

Скопируй текст ниже в Claude/нейросеть, впиши свою версию вместо `X.Y.Z` и при желании — тезисы «что нового». Нейросеть сделает mac-сборку, запустит Windows-сборку в GitHub Actions, скачает оба артефакта и задеплоит их на socmaster.pro (включая обновление `.env` и рестарт FastAPI).

---

Сделай полный релиз SOCMASTER версии **X.Y.Z** для macOS и Windows.

Что нового в этой версии (пойдёт в окно обновления десктопа): **«краткий текст в одну строку»**

План:

1. **macOS-сборка локально**
   - `./scripts/prepare-desktop-backend-bundle.sh`
   - В `desktop/fb-master-desktop/package.json` версию → **X.Y.Z**
   - `cd desktop/fb-master-desktop && npm run dist:mac:arm64 && cd ../..`
   - `./scripts/zip-mac-arm64-bundle.sh X.Y.Z`
   - Проверь, что в `static/releases/` появились `SOCMASTER-X.Y.Z-mac-arm64.dmg` и `SOCMASTER-X.Y.Z-mac-arm64-bundle.zip`.

2. **Windows-сборка в GitHub Actions**
   - Запушь версию в репо `mgusev1986/fb-master-desktop`: `git add -A && git commit -m "release X.Y.Z" && git push`
   - `gh workflow run "Build Windows SOCMASTER" --repo mgusev1986/fb-master-desktop --ref main -f version=X.Y.Z`
   - Дождись успеха (`gh run watch --repo mgusev1986/fb-master-desktop`)
   - `gh run download <runId> --repo mgusev1986/fb-master-desktop --name SOCMASTER-X.Y.Z-win --dir static/releases/_win_tmp`
   - Перенеси `.exe` из `_win_tmp/` в `static/releases/` и удали временную папку.

3. **Публикация на VPS**
   - `FB_DESKTOP_RELEASE_NOTES='<текст-что-нового>' ./deploy/publish-desktop-release.sh root@46.62.230.106 X.Y.Z socmaster.pro`
   - Скрипт сам: зальёт `dmg + bundle.zip + exe`, удалит старые сборки, обновит `/opt/fb-master/.env` (`FB_DESKTOP_LATEST_VERSION`, `FB_DESKTOP_DOWNLOAD_DARWIN_ARM64`, `FB_DESKTOP_DOWNLOAD_DARWIN_ARM64_BUNDLE`, `FB_DESKTOP_DOWNLOAD_WIN32_X64`, `FB_DESKTOP_RELEASE_NOTES`) и перезапустит `fb-master.service`.

4. **Если изменились шаблоны/бэкенд** — отдельно rsync через:
   ```
   rsync -avz <файлы> root@46.62.230.106:/opt/fb-master/_codeupd/
   ssh root@46.62.230.106 'cp -r _codeupd/* <в_нужные_пути>/ && chown -R fbmaster:fbmaster ... && rm -rf _codeupd && systemctl restart fb-master'
   ```

5. **Проверка**
   - `curl -sI https://socmaster.pro/static/releases/SOCMASTER-X.Y.Z-mac-arm64.dmg` → 200
   - `curl -sI https://socmaster.pro/static/releases/SOCMASTER-X.Y.Z-win-x64.exe` → 200
   - `curl -sL https://socmaster.pro/auth/unlock | grep premium-os-btn` — должен быть результат (две кнопки)
   - `ssh root@46.62.230.106 'systemctl is-active fb-master'` → `active`

После релиза — обнови `Checkpoint` (запись что версия X.Y.Z выложена для mac+win).
