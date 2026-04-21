"""InstagramModule — полная Beta-реализация."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest
from backend.modules.instagram.manifest import CAPABILITIES, MODULE_ID, NAVIGATION

if TYPE_CHECKING:
    from fastapi import FastAPI


class InstagramModule(INetworkModule):
    id = MODULE_ID
    display_name = "Instagram Master"
    short_description = (
        "Профили, поиск по hashtag, рассылка Direct, лайки/комменты по целевой "
        "аудитории, лайки сторисов, парсер аудитории конкурентов, AI-автоответчик."
    )
    icon = "brand-instagram"

    def status(self) -> ModuleStatus:
        return ModuleStatus.BETA

    def navigation(self) -> NavigationManifest:
        return NAVIGATION

    def capabilities(self) -> CapabilityManifest:
        return CAPABILITIES

    def register_routers(self, app: "FastAPI") -> None:
        return None


instagram_module = InstagramModule()


__all__ = ["InstagramModule", "instagram_module"]
