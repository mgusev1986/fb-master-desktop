"""LinkedInModule — полная Beta-реализация.

Активируется при FB_MASTER_LINKEDIN_MODULE_ENABLED=1. На VPS флаг по
умолчанию выключен; в desktop-сборке `local-backend-launcher.js`
выставляет его в `1`.

Этот класс — тонкая обёртка вокруг манифестов: контракт
`INetworkModule` сообщает launcher / sidebar / compliance UI всё, что
нужно для отрисовки модуля. Реальные роутеры подключаются в
`backend/app_factory.py` рядом с reddit-блоком (см. include_router'ы).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest
from backend.modules.linkedin.manifest import CAPABILITIES, MODULE_ID, NAVIGATION

if TYPE_CHECKING:
    from fastapi import FastAPI


class LinkedInModule(INetworkModule):
    id = MODULE_ID
    display_name = "LinkedIn Master"
    short_description = (
        "Профили, компании, поиск аудитории, цепочки касаний, AI-помощник "
        "и безопасный outreach для LinkedIn."
    )
    icon = "brand-linkedin"

    def status(self) -> ModuleStatus:
        # Beta — карточка кликабельна и показывает badge "β".
        return ModuleStatus.BETA

    def navigation(self) -> NavigationManifest:
        return NAVIGATION

    def capabilities(self) -> CapabilityManifest:
        return CAPABILITIES

    def register_routers(self, app: "FastAPI") -> None:
        # Роуты подключаются в app_factory.create_app() напрямую через
        # `app.include_router(linkedin_router.router)`, как и Reddit-роуты.
        # Метод оставлен как hook для будущих sub-mount'ов.
        return None


linkedin_module = LinkedInModule()


__all__ = ["LinkedInModule", "linkedin_module"]
