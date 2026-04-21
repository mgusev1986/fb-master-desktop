"""Build-функции для launcher и workspace switcher views.

Из модулей ModuleRegistry формируется плоская структура для Jinja:
launcher (крупные карточки) и switcher (dropdown в header).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.core.modules import module_registry
from backend.core.modules.base import ModuleStatus

if TYPE_CHECKING:
    from backend.core.modules.base import INetworkModule


_STATUS_LABELS: dict[ModuleStatus, tuple[str, str, str]] = {
    # (label, tone, description)
    ModuleStatus.READY: ("Готов", "success", "Модуль готов к работе."),
    ModuleStatus.BETA: ("Beta", "info", "Модуль доступен в beta — возможны ограничения."),
    ModuleStatus.COMING_SOON: (
        "Скоро",
        "muted",
        "Модуль в разработке. Следите за обновлениями.",
    ),
    ModuleStatus.DISABLED_FLAG: (
        "Выключен",
        "muted",
        "Модуль выключен администратором или feature-флагом.",
    ),
    ModuleStatus.MAINTENANCE: (
        "На обслуживании",
        "warning",
        "Модуль временно недоступен (миграция / обновление).",
    ),
}

_STATUS_BADGES: dict[ModuleStatus, str | None] = {
    ModuleStatus.READY: None,
    ModuleStatus.BETA: "β",
    ModuleStatus.COMING_SOON: "soon",
    ModuleStatus.DISABLED_FLAG: "off",
    ModuleStatus.MAINTENANCE: "⚙",
}


@dataclass(frozen=True)
class WorkspaceCardView:
    """Плоское представление модуля для launcher / switcher."""

    id: str
    display_name: str
    short_description: str
    icon: str
    status: str  # строковое значение ModuleStatus
    status_label: str
    status_tone: str
    status_description: str
    status_badge: str | None
    default_route: str
    openable: bool


@dataclass(frozen=True)
class LauncherView:
    """Вход-страница."""

    cards: tuple[WorkspaceCardView, ...]
    current_workspace_id: str | None


def _openable(status: ModuleStatus) -> bool:
    return status in (ModuleStatus.READY, ModuleStatus.BETA)


def _card_for_module(module: "INetworkModule") -> WorkspaceCardView:
    status = module.status()
    label, tone, description = _STATUS_LABELS.get(
        status, ("—", "muted", "")
    )
    nav = module.navigation()
    return WorkspaceCardView(
        id=module.id,
        display_name=module.display_name,
        short_description=module.short_description,
        icon=module.icon,
        status=status.value,
        status_label=label,
        status_tone=tone,
        status_description=description,
        status_badge=_STATUS_BADGES.get(status),
        default_route=nav.default_route,
        openable=_openable(status),
    )


def build_launcher_view(current_workspace_id: str | None = None) -> LauncherView:
    """Собрать данные для главного /workspaces-экрана."""
    cards = tuple(_card_for_module(m) for m in module_registry.visible())
    return LauncherView(cards=cards, current_workspace_id=current_workspace_id)


def build_switcher_view(current_workspace_id: str | None) -> tuple[WorkspaceCardView, ...]:
    """Список карточек для dropdown switcher в header."""
    return tuple(_card_for_module(m) for m in module_registry.visible())


__all__ = [
    "LauncherView",
    "WorkspaceCardView",
    "build_launcher_view",
    "build_switcher_view",
]
