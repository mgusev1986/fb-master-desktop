# Подпись установщиков FB Master (macOS / Windows)

Сейчас **подпись не вшита в репозиторий** — нужны **ваши** сертификаты. Без них сборка по-прежнему создаёт `.dmg` / `.exe`, но macOS и Windows могут показывать предупреждения.

## Тестирование без подписи (Gatekeeper)

Если macOS пишет, что файл **не проверен Apple**, или что приложение **«повреждено»** — это обычно **не поломка DMG**, а блокировка **неподписанного** приложения и флага карантина на скачанном файле.

1. Перетащите `FB Master.app` в папку «Программы».
2. В Терминале: `xattr -cr "/Applications/FB Master.app"`
3. Запустите приложение снова.

Альтернатива: в Finder **правый клик** по приложению → **Открыть** → подтвердить; либо **Системные настройки** → **Конфиденциальность и безопасность** → кнопка **«Всё равно открыть»** после первой попытки запуска.

Чтобы пользователям **не нужны были эти шаги**, выпускайте сборки с **Developer ID + нотаризацией** (раздел ниже).

## macOS (Developer ID + нотаризация)

1. Аккаунт **Apple Developer** (платная программа).
2. В Keychain Access выпустите **Developer ID Application** (не Mac App Distribution).
3. На машине, где собираете `npm run dist:mac`, сертификат должен быть в связке ключей — electron-builder подпишет приложение автоматически (`CSC_IDENTITY_AUTO_DISCOVERY=true` по умолчанию).
4. **Нотаризация** (чтобы Gatekeeper не ругался на скачанный файл):
   - Создайте [пароль приложения](https://appleid.apple.com) для Apple ID.
   - Перед сборкой в том же терминале:

```bash
export APPLE_ID="your@email.com"
export APPLE_APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"
export APPLE_TEAM_ID="XXXXXXXXXX"   # 10 символов, Team ID в developer.apple.com
npm run dist:mac
```

Без этих переменных скрипт `scripts/notarize.cjs` **тихо пропускает** нотаризацию (сборка не падает).

Отключить нотаризацию явно: `export SKIP_NOTARIZE=1`

Файл **`build/entitlements.mac.plist`** нужен для **hardened runtime** (уже подключён в `package.json`).

После того как подпись заработает, в `package.json` можно выставить **`gatekeeperAssess": true`** — electron-builder проверит приложение через `spctl` (иначе при отсутствии сертификата сборка могла бы падать).

## Windows (Authenticode)

1. Купите сертификат **Code Signing** (например у поставщика с поддержкой Authenticode).
2. Экспортируйте **.pfx** и перед сборкой:

```bash
export CSC_LINK="/absolute/path/to/certificate.pfx"
export CSC_KEY_PASSWORD="пароль_от_pfx"
npm run dist:win
```

Либо base64 файла в `CSC_LINK` (см. [electron-builder Code Signing](https://www.electron.build/code-signing)).

Без `CSC_*` установщик соберётся **без** подписи (SmartScreen может предупреждать).

## CI

Не коммитьте `.pfx` и пароли. Храните секреты в GitHub Actions / секрет-хранилище и экспортируйте переменные перед `electron-builder`.
