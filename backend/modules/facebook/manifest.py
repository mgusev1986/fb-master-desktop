"""Манифесты Facebook Master — навигация (sidebar 1:1) + capabilities.

Источник правды для нового `_shell/_sidebar_workspace.html`. Сейчас
sidebar FB отрендерен хардкодом в `templates/base.html` — этот манифест
живёт параллельно и используется для lauser / switcher. В M7 рендер
FB sidebar'а можно переключить на манифест, это не блокирует релиз.
"""

from __future__ import annotations

from backend.core.modules.manifests import (
    Capability,
    CapabilityLevel,
    CapabilityManifest,
    NavigationGroup,
    NavigationItem,
    NavigationManifest,
)

MODULE_ID = "facebook"


def _nav() -> NavigationManifest:
    groups = (
        NavigationGroup(
            id="fb_accounts_group",
            label="Рабочие аккаунты",
            items=(
                NavigationItem(id="fb_accounts", label="Аккаунты", href="/fb-accounts", icon="brand-facebook"),
                NavigationItem(id="account_branding", label="Оформление аккаунтов", href="/account-branding", icon="palette"),
                NavigationItem(id="account_natural_warmup", label="Прогрев аккаунтов", href="/account-warming", icon="flame"),
            ),
        ),
        NavigationGroup(
            id="fb_data_group",
            label="Данные",
            items=(
                NavigationItem(id="donors", label="Доноры", href="/donors", icon="users-group", hidden=True),
                NavigationItem(id="discovery", label="Автопоиск", href="/discovery", icon="radar-2"),
                NavigationItem(id="parser", label="Парсер", href="/parser", icon="radar"),
                NavigationItem(id="import", label="Импорт базы", href="/import", icon="file-import"),
                NavigationItem(id="people", label="База контактов", href="/people", icon="address-book"),
            ),
        ),
        NavigationGroup(
            id="fb_comms_group",
            label="Коммуникации",
            items=(
                NavigationItem(id="templates", label="Шаблоны", href="/templates", icon="template"),
                NavigationItem(id="outreach", label="Рассылка", href="/outreach", icon="send"),
                NavigationItem(id="outreach_magic", label="Magic-агент", href="/outreach?focus=agent", icon="sparkles", hidden=True),
                NavigationItem(id="scenarios", label="Режим агента", href="/scenarios", icon="brain"),
                NavigationItem(id="messenger", label="Messenger Old", href="/messenger", icon="message-circle", hidden=True),
                NavigationItem(id="messenger2", label="Мессенджер", href="/messenger2", icon="message-circle"),
            ),
        ),
        NavigationGroup(
            id="fb_crm_group",
            label="CRM",
            items=(
                NavigationItem(id="crm_funnel", label="Воронка", href="/crm/funnel", icon="chart-funnel"),
            ),
        ),
        NavigationGroup(
            id="fb_system_group",
            label="Система",
            items=(
                NavigationItem(id="system_speed", label="Скорость и паузы", href="/system/speed", icon="gauge"),
                NavigationItem(id="system_logs", label="Журнал и отчёты", href="/system/logs", icon="report-analytics"),
                NavigationItem(id="system_settings", label="Настройки", href="/system/settings", icon="settings"),
                NavigationItem(id="faq", label="FAQ", href="/faq", icon="help-circle"),
            ),
        ),
    )
    return NavigationManifest(module_id=MODULE_ID, groups=groups, default_route="/")


def _caps() -> CapabilityManifest:
    caps = (
        Capability(id="account_connect", label="Подключение FB-аккаунта", level=CapabilityLevel.SUPPORTED,
                   notes="Импорт cookies / TOTP, автологин, Playwright-сессия."),
        Capability(id="audience_discovery", label="Автопоиск доноров", level=CapabilityLevel.SUPPORTED),
        Capability(id="friends_parse", label="Парсинг friends/followers/following/members", level=CapabilityLevel.SUPPORTED),
        Capability(id="send_dm", label="Отправка ЛС", level=CapabilityLevel.PARTIAL,
                   notes="Рейт-лимиты Meta, anti-spam; throttle + send-window."),
        Capability(id="publish_comment", label="Публикация комментария", level=CapabilityLevel.PARTIAL,
                   notes="Учитывает anti-spam. Используется review-слой в кампаниях."),
        Capability(id="sequence_automation", label="Режим агента / сценарии", level=CapabilityLevel.SUPPORTED),
        Capability(id="messenger_inbox", label="Встроенный Messenger (webview)", level=CapabilityLevel.SUPPORTED),
        Capability(id="recent_activity", label="Недавняя видимая активность", level=CapabilityLevel.PARTIAL,
                   notes="По постам/лайкам; online-статус Meta не раскрывает стабильно."),
    )
    return CapabilityManifest(module_id=MODULE_ID, capabilities=caps)


NAVIGATION: NavigationManifest = _nav()
CAPABILITIES: CapabilityManifest = _caps()


__all__ = ["CAPABILITIES", "MODULE_ID", "NAVIGATION"]
