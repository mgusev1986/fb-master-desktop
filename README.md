# SOCMASTER — Build Infrastructure (Public)

Этот публичный репозиторий — **build trigger** для проекта SOCMASTER.
Содержит только GitHub Actions workflow-файлы для сборки macOS и Windows
дистрибутивов. Вся бизнес-логика, исходный код и приватные ассеты живут
в отдельном **приватном** репозитории `socmaster-core`, к которому workflow
обращается через PAT-секрет (`secrets.CORE_REPO_PAT`) во время билда.

## Что такое SOCMASTER

Десктоп-приложение для автоматизации работы с социальными сетями (Facebook,
Instagram, Twitter/X, Reddit, LinkedIn): парсер аудитории, рассылка в личку
с обходом E2EE-блокировок, прогрев аккаунтов, CRM-воронка с экспортом между
устройствами.

Сайт и оплата: **https://socmaster.pro**

## Сборка релиза

Релизы триггерятся вручную через `workflow_dispatch` на нужном теге:

```bash
gh workflow run build-mac-x64.yml --ref vX.Y.Z -f version=X.Y.Z
gh workflow run build-windows.yml --ref vX.Y.Z -f version=X.Y.Z
```

Workflow клонирует приватный репо `socmaster-core` (через `secrets.CORE_REPO_PAT`),
собирает DMG / EXE, и публикует в GitHub Release этого репо.

## Архитектура

```
[ public ] socmaster-builder       ← этот репо: workflows + README + LICENSE
                |
                | clone + build via PAT
                ↓
[ private ] socmaster-core         ← бизнес-код, приватный
                |
                | rsync deploy
                ↓
[ prod ] root@socmaster.pro        ← FastAPI back-office + статика клиентов
```

## Лицензия

Весь код и сборки SOCMASTER — proprietary. См. [LICENSE](LICENSE).
Любое использование, копирование, распространение или модификация
запрещены без явного письменного разрешения автора.
