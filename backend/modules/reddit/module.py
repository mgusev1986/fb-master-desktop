"""RedditModule — полноценный модуль (активируется при FB_MASTER_REDDIT_MODULE_ENABLED=1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest
from backend.modules.reddit.manifest import CAPABILITIES, MODULE_ID, NAVIGATION

if TYPE_CHECKING:
    from fastapi import FastAPI


class RedditModule(INetworkModule):
    id = MODULE_ID
    display_name = "Reddit Master"
    short_description = (
        "Работа с Reddit: аккаунты, сабреддиты, поиск аудитории, "
        "недавняя активность, очереди сообщений, AI-помощник и безопасный агент."
    )
    icon = "brand-reddit"

    def status(self) -> ModuleStatus:
        # Активный модуль. В M4 — это skeleton-экраны "В разработке",
        # но статус уже READY, чтобы карточка в launcher была кликабельна.
        return ModuleStatus.BETA

    def navigation(self) -> NavigationManifest:
        return NAVIGATION

    def capabilities(self) -> CapabilityManifest:
        return CAPABILITIES

    def register_routers(self, app: "FastAPI") -> None:
        # Роуты подключаются в app_factory.create_app() напрямую через
        # `app.include_router(reddit_router.router)`, как и FB-роуты.
        # Метод оставлен как hook для будущих sub-mount'ов.
        return None


reddit_module = RedditModule()


__all__ = ["RedditModule", "reddit_module"]
