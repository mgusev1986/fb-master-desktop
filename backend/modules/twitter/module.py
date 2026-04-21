"""TwitterModule — полная Beta-реализация.

Активируется при FB_MASTER_TWITTER_MODULE_ENABLED=1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest
from backend.modules.twitter.manifest import CAPABILITIES, MODULE_ID, NAVIGATION

if TYPE_CHECKING:
    from fastapi import FastAPI


class TwitterModule(INetworkModule):
    id = MODULE_ID
    display_name = "Twitter / X Master"
    short_description = (
        "Профили, поиск аудитории, рассылка DM, реплаи, AI-помощник и "
        "AI-автоответчик для X (Twitter)."
    )
    icon = "brand-twitter"

    def status(self) -> ModuleStatus:
        return ModuleStatus.BETA

    def navigation(self) -> NavigationManifest:
        return NAVIGATION

    def capabilities(self) -> CapabilityManifest:
        return CAPABILITIES

    def register_routers(self, app: "FastAPI") -> None:
        return None


twitter_module = TwitterModule()


__all__ = ["TwitterModule", "twitter_module"]
