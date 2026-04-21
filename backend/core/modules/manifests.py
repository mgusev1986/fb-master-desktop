"""Манифесты для сетевых модулей: навигация + capabilities.

Навигационный манифест — источник правды для sidebar конкретного workspace
(заменяет хардкоженный HTML в будущем `_shell/base.html`). Capability-
манифест описывает поддержку действий, чтобы UI честно показывал
supported / partial / restricted / unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class NavigationItem:
    """Один пункт sidebar внутри workspace."""

    id: str  # стабильный идентификатор (для page_id)
    label: str
    href: str  # absolute url, напр. "/fb-accounts" или "/reddit/accounts"
    icon: str  # tabler-icons name
    hidden: bool = False  # пункт существует, но скрыт (как текущие /donors, /messenger)
    badge: str | None = None  # текст badge: "β", "new", "beta" и т.п.
    requires_capability: str | None = None  # id capability, которая должна быть available


@dataclass(frozen=True)
class NavigationGroup:
    """Группа пунктов в sidebar (например, 'Коммуникации')."""

    id: str
    label: str
    items: tuple[NavigationItem, ...]


@dataclass(frozen=True)
class NavigationManifest:
    """Полная навигационная карта workspace."""

    module_id: str
    groups: tuple[NavigationGroup, ...]
    default_route: str  # куда открывать модуль (например, "/" для fb, "/reddit" для reddit)


class CapabilityLevel(str, Enum):
    """Уровень поддержки действия на платформе.

    Важно: UI должен честно показывать эти уровни. Никаких фейковых
    "supported", когда платформа на самом деле блокирует / ограничивает.
    """

    SUPPORTED = "supported"
    PARTIAL = "partial"  # работает, но с ограничениями
    RESTRICTED = "restricted"  # работает только в ручном/review-режиме
    UNSUPPORTED = "unsupported"  # недоступно на платформе


@dataclass(frozen=True)
class Capability:
    """Описание одного действия модуля."""

    id: str  # напр. "send_dm", "publish_comment", "recent_activity_scan"
    label: str
    level: CapabilityLevel
    notes: str = ""  # пояснение ограничения (показывается в Compliance Center)


@dataclass(frozen=True)
class CapabilityManifest:
    """Все capabilities модуля."""

    module_id: str
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)

    def by_level(self, level: CapabilityLevel) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if c.level == level)


__all__ = [
    "Capability",
    "CapabilityLevel",
    "CapabilityManifest",
    "NavigationGroup",
    "NavigationItem",
    "NavigationManifest",
]
