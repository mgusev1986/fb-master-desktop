# Превью-сборка FB Master (канал разработчика)

Цель: собрать и выложить **отдельную** версию для проверки **до** финального релиза клиентам. Клиенты по-прежнему получают обновления с `/api/public/desktop-update` и файлы из `static/releases/`. Разработчик — страница `/download/dev`, манифест `/api/public/desktop-update-dev`, файлы в `static/releases/dev/`.

## Промпт для ассистента (скопируйте и подставьте версию)

> Сделай **dev-превью** FB Master версии **X.Y.Z** (не трогай прод):  
> 1) обнови `desktop/fb-master-desktop/package.json` до X.Y.Z (или оставь отдельную ветку с этой версией);  
> 2) `scripts/prepare-desktop-backend-bundle.sh`;  
> 3) `cd desktop/fb-master-desktop && npm run dist:mac:arm64`;  
> 4) `scripts/zip-mac-arm64-bundle.sh X.Y.Z dev` — положит DMG и zip в `static/releases/dev/`;  
> 5) задеплой код на VPS при необходимости (`scripts/release-fb-master-desktop.sh` **без** прод-публикации или вручную rsync `backend/` + `templates/`);  
> 6) опубликуй только dev: `./deploy/publish-desktop-dev-release.sh root@СЕРВЕР X.Y.Z socmaster.pro "X.Y.Z-dev: кратко что тестируем"`;  
> 7) проверь `https://ДОМЕН/download/dev`, `https://ДОМЕН/api/public/desktop-update-dev` и HEAD на ссылки из JSON.

## Ручные шаги (кратко)

1. Соберите десктоп как обычно (`prepare-desktop-backend-bundle.sh`, `npm run dist:mac:arm64`).
2. Упакуйте zip в dev-каталог: `./scripts/zip-mac-arm64-bundle.sh ВЕРСИЯ dev`.
3. Залейте: `./deploy/publish-desktop-dev-release.sh root@HOST ВЕРСИЯ ваш-домен.ru "заметки"`.
4. Откройте в браузере **`/download/dev`** — там явная пометка «только для разработчика» и ссылки на DMG/ZIP.
5. Когда всё ок — делайте **обычный** релиз (`scripts/release-fb-master-desktop.sh …` без `dev`), чтобы обновились `FB_DESKTOP_*` и `static/releases/`.

## Переменные окружения (прод-сервер)

| Переменная | Назначение |
|------------|------------|
| `FB_DESKTOP_DEV_LATEST_VERSION` | Версия строкой для манифеста dev |
| `FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64` | URL DMG (можно не задавать — авто из `static/releases/dev/`) |
| `FB_DESKTOP_DEV_DOWNLOAD_DARWIN_ARM64_BUNDLE` | URL zip-bundle |
| `FB_DESKTOP_DEV_RELEASE_NOTES` | Текст для страницы и JSON (одна логическая строка) |
| `FB_DESKTOP_DEV_MIN_VERSION` | Опционально, для манифеста |

Скрипт `publish-desktop-dev-release.sh` при передаче домена выставляет три первых ключа автоматически.

## Подписанные ссылки

Файлы в `static/releases/dev/` отдаются через тот же `/download/release`, параметр `f` имеет вид `dev/ИмяФайла.dmg` (подпись считается по полному ключу).
