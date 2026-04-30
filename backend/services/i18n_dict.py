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
    }
}


def all_keys() -> list[str]:
    """Все RU-ключи (для sanity-check / lint-скрипта)."""
    return list(TRANSLATIONS.get("en", {}).keys())


def is_translated(key: str, lang: str = "en") -> bool:
    """True если для key есть перевод на lang."""
    return key in TRANSLATIONS.get(lang, {})
