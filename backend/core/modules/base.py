"""Абстрактный контракт сетевого модуля (Facebook, Reddit, Twitter/X, LinkedIn, …).

Каждый модуль — это:
- идентичность (`id`, `display_name`, `icon`, статус),
- навигационный манифест (что показывать в sidebar для этого workspace),
- манифест capabilities (что модуль реально умеет — чтобы UI честно
  показывал «partial»/«unsupported» состояния),
- хуки на старт приложения (register_routers, on_startup),
- health-репорт для диагностики.

Контракт намеренно тонкий: реальные `IAccountConnector`/`IConversationProvider`/
и т.д. живут рядом в `interfaces.py`. Модуль регистрирует нужные провайдеры
сам — не все модули поддерживают все контракты.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from backend.core.modules.manifests import (
        CapabilityManifest,
        NavigationManifest,
    )


class ModuleStatus(str, Enum):
    """Статус модуля для launcher-карточки.

    READY         — рабочий, доступен для открытия.
    BETA          — рабочий, но пользователь видит badge "β".
    COMING_SOON   — в разработке, карточка disabled с пояснением.
    DISABLED_FLAG — выключен feature-флагом / лицензией.
    MAINTENANCE   — временно недоступен (напр., миграция схемы).
    """

    READY = "ready"
    BETA = "beta"
    COMING_SOON = "coming_soon"
    DISABLED_FLAG = "disabled_flag"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class HealthReport:
    """Краткий отчёт о здоровье модуля. Не ошибка = ok=True."""

    ok: bool
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class INetworkModule(ABC):
    """Абстрактный сетевой модуль multi-workspace shell.

    Реализация живёт в `backend/modules/<name>/module.py`. Модуль
    регистрирует себя в `ModuleRegistry` и предоставляет навигацию,
    capabilities и хуки. FB-модуль = тонкая обёртка над существующими
    роутерами/сервисами, без переезда кода. Reddit-модуль — новый, свой.
    """

    id: str
    display_name: str
    short_description: str
    icon: str  # tabler-icons name, например "brand-facebook" / "brand-reddit"

    @abstractmethod
    def status(self) -> ModuleStatus:
        """Текущий статус модуля (может зависеть от флагов/лицензии)."""

    @abstractmethod
    def navigation(self) -> "NavigationManifest":
        """Навигационная карта модуля для sidebar."""

    @abstractmethod
    def capabilities(self) -> "CapabilityManifest":
        """Что модуль умеет / частично / не умеет делать.

        Используется Compliance Center и UI, чтобы честно обозначать
        ограничения платформы.
        """

    def register_routers(self, app: "FastAPI") -> None:
        """Подключить свои FastAPI-роутеры.

        Default: no-op. FB-модуль использует default, потому что его
        роутеры уже подключены в `app_factory.create_app()` напрямую.
        Reddit-модуль переопределяет и подключает свои.
        """
        return None

    def on_startup(self, app: "FastAPI") -> None:
        """Хук на lifespan startup (фоновые потоки, прогрев кеша, и т.п.).

        Default: no-op.
        """
        return None

    def health(self) -> HealthReport:
        """Быстрая проверка доступности модуля (default: ok)."""
        return HealthReport(ok=True, summary="ok")


__all__ = ["HealthReport", "INetworkModule", "ModuleStatus"]
