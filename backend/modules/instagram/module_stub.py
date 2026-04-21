"""Instagram module stub — COMING_SOON карточка."""

from __future__ import annotations

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest

STUB_MODULE_ID = "instagram"


class InstagramComingSoonModule(INetworkModule):
    id = STUB_MODULE_ID
    display_name = "Instagram Master"
    short_description = (
        "Профили, поиск по hashtag, рассылка Direct, лайки/комменты по целевой "
        "аудитории, лайки сторисов, парсер аудитории конкурентов, AI-автоответчик."
    )
    icon = "brand-instagram"

    def status(self) -> ModuleStatus:
        return ModuleStatus.COMING_SOON

    def navigation(self) -> NavigationManifest:
        return NavigationManifest(module_id=self.id, groups=(), default_route="/instagram")

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(module_id=self.id, capabilities=())


instagram_coming_soon_module = InstagramComingSoonModule()

__all__ = ["InstagramComingSoonModule", "STUB_MODULE_ID", "instagram_coming_soon_module"]
