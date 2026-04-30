"""Словарь переводов RU → EN для UI приложения SOCMASTER.

Архитектура:
- Один Python dict, группированный комментариями по разделам.
- Ключ = русская строка как есть в шаблоне (используется в `{{ t("...") }}`).
- Значение = английский перевод.
- Если ключа нет в `TRANSLATIONS["en"]` → `translate()` возвращает RU как есть
  (graceful degradation — приложение не падает, просто часть текста на RU).

Для placeholder-переменных используется Python `str.format`::

    "Привет, {name}": "Hello, {name}"

→ `t("Привет, {name}", name="Ivan")` = `"Hello, Ivan"`.

Workflow добавления новой строки:
1. Обернуть в шаблоне: `{{ t("Новая кнопка") }}`.
2. Добавить ключ в этот файл с EN-переводом.
3. Проверить через `python3 scripts/find_untranslated.py` (см. отдельный скрипт).

Задача — постепенно покрыть все 80+ шаблонов проекта. Этапы (variant B):
- Этап 1 (сейчас): sidebar (base.html), главная FB Master (dashboard.html).
- Этап 2: основные FB Master страницы (Аккаунты, Шаблоны, Рассылка, ...).
- Этап 3: внутренние диалоги, формы, модалки.
- Этап 4: backend flash-сообщения и errors.
- Этап 5: beta-модули (Reddit/LinkedIn/Twitter/Instagram/Telegram).
"""

from __future__ import annotations

from typing import Final

TRANSLATIONS: Final[dict[str, dict[str, str]]] = {
    "en": {
        # ─── Sidebar / Navigation (templates/base.html) ──────────────────
        "Главная": "Home",
        "Обзор": "Overview",
        "Рабочие аккаунты": "Working accounts",
        "Аккаунты": "Accounts",
        "Оформление аккаунтов": "Account profiles",
        "Прогрев аккаунтов": "Account warm-up",
        "Данные": "Data",
        "Автопоиск": "Auto-search",
        "Парсер": "Scraper",
        "Импорт базы": "Import database",
        "База контактов": "Contact database",
        "Коммуникации": "Communications",
        "Шаблоны": "Templates",
        "Рассылка": "Outreach",
        "Режим агента": "Agent mode",
        "Мессенджер": "Messenger",
        "CRM": "CRM",
        "Воронка": "Funnel",
        "Уже контактировали": "Already contacted",
        "Система": "System",
        "Настройки": "Settings",
        "Журнал и отчёты": "Logs & reports",
        "Скорость и паузы": "Speed & pauses",
        "FAQ": "FAQ",
        "База знаний": "Knowledge base",
        "Выйти": "Sign out",

        # ─── Sidebar license info ────────────────────────────────────────
        "Срок действия ключа не ограничен.": "Access key has no expiration.",
        "Срок действия ключа не ограничен": "Access key has no expiration",
        "Последний день доступа, до {until}": "Last day of access, until {until}",
        "Осталось {days} {days_word}, до {until}": "{days} {days_word} left, until {until}",
        "день": "day",
        "дня": "days",
        "дней": "days",

        # ─── Dashboard (главная FB Master) ───────────────────────────────
        "Facebook Master": "Facebook Master",
        "Готов": "Ready",
        "Готово": "Ready",
        "Browser-profile режим": "Browser-profile mode",
        "Источники (доноры)": "Sources (donors)",
        "Людей в списке": "People in list",
        "Профилей Facebook": "Facebook profiles",
        "Шаг 1 — Аккаунты": "Step 1 — Accounts",
        "Шаг 2 — Оформление": "Step 2 — Profile",
        "Шаг 3 — Прогрев": "Step 3 — Warm-up",
        "Шаг 4 — Поиск аудитории": "Step 4 — Audience search",
        "Шаг 5 — База контактов": "Step 5 — Contact database",
        "Шаг 6 — Шаблоны и AI": "Step 6 — Templates & AI",
        "Шаг 7 — Рассылка": "Step 7 — Outreach",
        "Режим агента (сценарии)": "Agent mode (scenarios)",
        "Перейти к аккаунтам": "Go to accounts",
        "Открыть мессенджер": "Open messenger",
        "Magic-агент": "Magic-agent",

        # ─── Common buttons / actions ────────────────────────────────────
        "Сохранить": "Save",
        "Отмена": "Cancel",
        "Закрыть": "Close",
        "Удалить": "Delete",
        "Редактировать": "Edit",
        "Создать": "Create",
        "Добавить": "Add",
        "Применить": "Apply",
        "Подтвердить": "Confirm",
        "Назад": "Back",
        "Далее": "Next",
        "Готово!": "Done!",
        "Получить доступ": "Get access",
        "Скачать": "Download",
        "Загрузить": "Upload",
        "Поиск": "Search",
        "Найти": "Find",
        "Сбросить": "Reset",
        "Очистить": "Clear",
        "Копировать": "Copy",
        "Скопировано": "Copied",
        "Показать": "Show",
        "Скрыть": "Hide",
        "Выбрать": "Select",
        "Выбрать всё": "Select all",
        "Снять выделение": "Deselect all",
        "Импорт": "Import",
        "Экспорт": "Export",
        "Запустить": "Start",
        "Остановить": "Stop",
        "Пауза": "Pause",
        "Продолжить": "Resume",

        # ─── Common labels / states ──────────────────────────────────────
        "Да": "Yes",
        "Нет": "No",
        "Активен": "Active",
        "активен": "active",
        "Неактивен": "Inactive",
        "неактивен": "inactive",
        "Отозван": "Revoked",
        "отозван": "revoked",
        "Истёк": "Expired",
        "истёк": "expired",
        "Ожидает": "Pending",
        "ожидает": "pending",
        "Включено": "Enabled",
        "Выключено": "Disabled",
        "Загрузка…": "Loading…",
        "Сохранение…": "Saving…",
        "Ошибка": "Error",
        "Успешно": "Success",
        "Внимание": "Warning",
        "Информация": "Info",
        "Бессрочно": "Forever",
        "бессрочно": "forever",

        # ─── Время / даты ────────────────────────────────────────────────
        "Сегодня": "Today",
        "Вчера": "Yesterday",
        "Завтра": "Tomorrow",
        "минут": "min",
        "мин": "min",
        "часов": "hours",
        "час": "hour",
        "часа": "hours",
        "секунд": "sec",
        "сек": "sec",
        "дн.": "d",

        # ─── Workspace switcher / hero ───────────────────────────────────
        "Выберите рабочее пространство": "Select a workspace",
        "Каждая соцсеть — свой модуль со своим набором аккаунтов, очередей и правил безопасности.": "Each social network is a separate module with its own accounts, queues, and safety rules.",
        "Reddit Master": "Reddit Master",
        "LinkedIn Master": "LinkedIn Master",
        "Twitter / X Master": "Twitter / X Master",
        "Instagram Master": "Instagram Master",
        "Telegram Master": "Telegram Master",
        "Beta": "Beta",
        "План": "Planned",
        "Недоступно": "Unavailable",
        "Открыть": "Open",
        "Модуль готов к работе.": "Module is ready to use.",
        "Модуль доступен в beta — возможны ограничения.": "Module is in beta — limitations may apply.",

        # ─── Language switcher ───────────────────────────────────────────
        "Выбор языка": "Language",
        "Русский": "Russian",
        "English": "English",
        "Сменить язык": "Switch language",
        "Сменить язык (скоро)": "Switch language (coming soon)",
        "Скоро будет доступен выбор языка интерфейса.": "UI language switcher is coming soon.",
        "Сменить тему": "Toggle theme",

        # ─── Sidebar header / license / ID ───────────────────────────────
        "Версия программы": "Application version",
        "Уникальный ID этой копии программы": "Unique ID of this app installation",
        "ID:": "ID:",

        # ─── Sidebar nav extras ──────────────────────────────────────────
        "Доноры": "Donors",
        "Messenger Old": "Messenger Old",
        "Админ-панель": "Admin panel",

        # ─── Modals / common dialogs ─────────────────────────────────────
        "Подтвердить действие": "Confirm action",

        # ─── Preview as user banner ──────────────────────────────────────
        "Предпросмотр клиента:": "Client preview:",
        "как у десктопа — без «Админ-панели», «Выйти» и служебных блоков в «Настройках».":
            "matches desktop view — without 'Admin panel', 'Sign out' and service blocks in 'Settings'.",
        "Выйти из предпросмотра": "Exit preview",

        # ─── Dashboard (templates/dashboard.html) ────────────────────────
        "Главная — Facebook Master": "Home — Facebook Master",
        "Аккаунты, парсер, оформление и прогрев, шаблоны, рассылка в Messenger, сценарии и CRM-воронка. Browser-profile режим на Chromium + cookies + свой прокси на каждый аккаунт.":
            "Accounts, scraper, profile setup and warm-up, templates, Messenger outreach, scenarios, and a CRM funnel. Browser-profile mode on Chromium + cookies + a dedicated proxy for each account.",
        "Подключение аккаунтов = импорт купленных FB-аккаунтов с cookies + персональный Chrome-профиль + свой прокси на каждый аккаунт. Facebook видит сессию как обычный браузерный логин. Рассылка в Messenger, прогрев и сценарии — с ручным подтверждением опасных действий по умолчанию.":
            "Connecting accounts = importing purchased FB accounts with cookies + a personal Chrome profile + a dedicated proxy for each account. Facebook sees the session as a regular browser login. Messenger outreach, warm-up, and scenarios use manual confirmation for risky actions by default.",

        "Контактов в базе": "Contacts in database",
        "FB-аккаунты": "FB accounts",

        # Step 1 — Аккаунты
        "Подключение FB-аккаунтов через browser-профиль (Chromium + cookies) и персональный прокси. Без подключённого аккаунта парсинг, прогрев и рассылка недоступны.":
            "Connecting FB accounts via browser-profile (Chromium + cookies) and a personal proxy. Without a connected account, scraping, warm-up, and outreach are unavailable.",
        # Step 2 — Оформление
        "Заполнение профилей: аватар, обложка, bio, работа, интересы. Шаблоны оформления и автозагрузка фото для «человеческого» вида аккаунта.":
            "Filling out profiles: avatar, cover, bio, work, interests. Profile templates and photo auto-upload for a 'human-looking' account.",
        # Step 3 — Прогрев
        "Natural warmup: лайки, скролл ленты, реакции, дружеские запросы по реалистичным паттернам. Снижает риск блокировки и повышает headroom по активности.":
            "Natural warm-up: likes, feed scrolling, reactions, and friend requests using realistic patterns. Reduces ban risk and increases activity headroom.",
        # Step 4 — Поиск аудитории
        "Автопоиск по ключевикам, парсинг друзей доноров, групп и комментариев. Импорт базы из CSV. Дедуп и сегментация по источнику.":
            "Keyword auto-search, scraping donor friends, groups, and comments. CSV database import. Deduplication and segmentation by source.",
        # Step 5 — База контактов
        "Единая база людей: фильтры по тегам, источникам, статусам CRM, превью профилей. Основа для рассылок и сценариев.":
            "A unified people database: filters by tags, sources, CRM stages, and profile previews. The foundation for outreach and scenarios.",
        # Step 6 — Шаблоны и AI
        "Шаблоны первого касания, follow-up, повторные касания. AI-вариации, антиспам-фильтр запрещённых фраз, превью перед отправкой.":
            "First-touch templates, follow-ups, repeat touches. AI variations, an anti-spam filter for forbidden phrases, and a preview before sending.",
        # Step 7 — Рассылка
        "Кампании outreach в Messenger с дневными/недельными капами, окнами отправки и review-queue. Magic-агент для semi-auto режима.":
            "Outreach campaigns in Messenger with daily/weekly caps, sending windows, and a review queue. Magic-agent for semi-auto mode.",
        # Сценарии
        "Сценарии": "Scenarios",
        "Конструктор цепочек: собрать → обогатить → оценить → написать → подождать → follow-up → ветка на ответ. Ручная проверка на всех send-шагах по умолчанию.":
            "Sequence builder: collect → enrich → score → write → wait → follow-up → reply branch. Manual review at every send-step by default.",
        # Мессенджер
        "Conversation workspace: список тредов, детальный вид, AI-помощник для ответов, ручная обработка сложных переписок.":
            "Conversation workspace: thread list, detailed view, an AI assistant for replies, and manual handling of complex chats.",
        # CRM — Воронка
        "CRM — Воронка": "CRM — Funnel",
        "Воронка лидов: стадии, фильтры по источнику, контактам и ответам. Быстрый переход к диалогам и карточкам людей.":
            "Lead funnel: stages, filters by source, contact, and reply. Quick navigation to chats and people cards.",

        # Recent jobs section
        "Недавние действия": "Recent activity",
        "Последние задачи": "Recent jobs",
        "Вид": "Kind",
        "Тип": "Type",
        "Статус": "Status",
        "Создано": "Created",
        "Успешно": "Success",
        "Выполняется": "Running",
        "Пока ничего не запускали": "Nothing has been launched yet",
        "Задач пока нет": "No jobs yet",
        "Когда запустите поиск людей, прогрев или рассылку, здесь появятся последние записи.":
            "When you launch people search, warm-up, or outreach, the latest records will appear here.",
        "Запустите парсинг, прогрев или рассылку — здесь появятся последние задачи.":
            "Launch scraping, warm-up, or outreach — the latest jobs will appear here.",

        # ─── FB Accounts (templates/fb_accounts/list.html) ───────────────
        "Аккаунты — SOCMASTER": "Accounts — SOCMASTER",
        "Facebook-аккаунты — SOCMASTER": "Facebook accounts — SOCMASTER",
        "Facebook-аккаунты": "Facebook accounts",

        # Page intro (client mode)
        "После входа программа запоминает его — повторно вводить данные обычно не нужно. Одновременно в работе может быть до {limit} профилей; если лимит исчерпан, у одной карточки выберите «Не в работе». Для чатов нажмите «Использовать в Мессенджере» у нужного профиля. Данные из файла импорта хранятся в зашифрованном виде.":
            "After signing in the program remembers the login — you usually don't need to re-enter credentials. Up to {limit} profiles can run simultaneously; when the limit is reached, set one card to «Not active». For chats, click «Use in Messenger» on the desired profile. Data from import files is stored encrypted.",

        # Card status badges
        "Дубликат логина": "Duplicate login",
        "Слот": "Slot",
        "В очереди": "In queue",
        "Ожидание": "Pending",
        "Вход сохранён": "Login saved",
        "Сессия ок": "Session OK",
        "Вход выполнен, сессия ок": "Signed in, session OK",
        "Окно входа": "Login window",
        "Окно входа открыто": "Login window open",
        "Нужен пароль": "Password needed",
        "Нужен пароль (Continue-gate)": "Password needed (Continue-gate)",
        "Нужно войти": "Sign-in required",
        "Нет сессии": "No session",
        "Не проверяли": "Not checked",
        "Текущий для Мессенджера": "Current for Messenger",
        "Сохранено:": "Saved:",
        "В базе:": "In database:",
        "Этот профиль может работать одновременно с другими": "This profile can run alongside others",
        "Участвует в параллельных задачах": "Participates in parallel tasks",
        "Выберите слот 1–3 ниже, чтобы профиль участвовал в задачах":
            "Choose slot 1–3 below to enable the profile in tasks",
        "Только настройка; не в парсере/Мессенджере без слота":
            "Setup only; not used in scraper/Messenger without a slot",
        "Когда последний раз сохранили вход": "Last time the login was saved",
        "Резервная копия cookies в SQLite · время Europe/Madrid":
            "Cookies backup in SQLite · Europe/Madrid timezone",
        "Открыть этот профиль во встроенном Мессенджере":
            "Open this profile in the embedded Messenger",

        # Buttons & form labels
        "Войти в Facebook": "Sign in to Facebook",
        "Использовать в Мессенджере": "Use in Messenger",
        "Не в работе": "Not active",
        "Активный": "Active",
        "Поиск по названию…": "Search by name…",
        "Развернуть или свернуть карточку аккаунта": "Expand or collapse account card",
        "Сохранить подпись для навигации": "Save label for navigation",
        "Подпись (для удобства)": "Label (for convenience)",
        "Например: Elena Vasquez — Прокси Турция": "For example: Elena Vasquez — Turkey proxy",
        "Импорт из файла": "Import from file",
        "Сохранить подпись": "Save label",
        "Сохранить": "Save",

        # Slots / bulk info
        "В работе сейчас:": "Active now:",
        "Активные слоты:": "Active slots:",
        "Чтобы освободить слот, у одной карточки выберите «Не в работе» или смените номер.":
            "To free a slot, set «Not active» on one card or change its number.",
        "Чтобы включить другой аккаунт, у кого-то из трёх снимите слот («Ожидание») или переназначьте слот на новую карточку.":
            "To enable another account, remove the slot from one of the three («Pending») or reassign the slot to a new card.",
        "{filled} из {total} занято": "{filled} of {total} in use",

        # Login screen
        "Встроенный логин: открывает Facebook внутри приложения. Messenger не попросит PIN повторно.":
            "Embedded login: opens Facebook inside the app. Messenger won't ask for the PIN again.",
        "Старый режим: отдельное окно Chromium через Playwright.":
            "Legacy mode: separate Chromium window via Playwright.",

        # Risk levels
        "Оценка надёжности по сессии, прокси, суточной нагрузке (UTC) и ошибкам задач за 24 ч":
            "Reliability score based on session, proxy, daily load (UTC), and task errors over 24h",
    }
}


def all_keys() -> list[str]:
    """Все RU-ключи (для sanity-check / lint-скрипта)."""
    return list(TRANSLATIONS.get("en", {}).keys())


def is_translated(key: str, lang: str = "en") -> bool:
    """True если для key есть перевод на lang."""
    return key in TRANSLATIONS.get(lang, {})
