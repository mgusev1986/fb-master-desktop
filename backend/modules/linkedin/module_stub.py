"""LinkedIn module stub — COMING_SOON карточка в launcher.

Регистрируется при `FB_MASTER_LINKEDIN_MODULE_ENABLED=0` (default на VPS).
Заменяется полным `LinkedInModule` при включении флага в desktop-сборке.
"""

from __future__ import annotations

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest

STUB_MODULE_ID = "linkedin"


class LinkedInComingSoonModule(INetworkModule):
    id = STUB_MODULE_ID
    display_name = "LinkedIn Master"
    short_description = (
        "Профили, компании, поиск аудитории, цепочки касаний, AI-помощник "
        "и безопасный outreach для LinkedIn."
    )
    icon = "brand-linkedin"

    def status(self) -> ModuleStatus:
        return ModuleStatus.COMING_SOON

    def navigation(self) -> NavigationManifest:
        # Disabled-карточка в launcher: sidebar пустой, default_route неактивен.
        return NavigationManifest(module_id=self.id, groups=(), default_route="/linkedin")

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(module_id=self.id, capabilities=())


linkedin_coming_soon_module = LinkedInComingSoonModule()

__all__ = ["LinkedInComingSoonModule", "STUB_MODULE_ID", "linkedin_coming_soon_module"]
