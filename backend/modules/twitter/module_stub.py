"""Twitter/X module stub — COMING_SOON карточка в launcher."""

from __future__ import annotations

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest

STUB_MODULE_ID = "twitter"


class TwitterComingSoonModule(INetworkModule):
    id = STUB_MODULE_ID
    display_name = "Twitter / X Master"
    short_description = (
        "Профили, поиск аудитории, рассылка DM, реплаи, AI-помощник и "
        "AI-автоответчик для X (Twitter)."
    )
    icon = "brand-twitter"

    def status(self) -> ModuleStatus:
        return ModuleStatus.COMING_SOON

    def navigation(self) -> NavigationManifest:
        return NavigationManifest(module_id=self.id, groups=(), default_route="/twitter")

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(module_id=self.id, capabilities=())


twitter_coming_soon_module = TwitterComingSoonModule()

__all__ = ["TwitterComingSoonModule", "STUB_MODULE_ID", "twitter_coming_soon_module"]
