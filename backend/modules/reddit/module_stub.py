"""Reddit module stub — COMING_SOON карточка в launcher.

Регистрируется при `FB_MASTER_REDDIT_MODULE_ENABLED=0` (default).
В M4 заменяется на полный RedditModule.
"""

from __future__ import annotations

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest

STUB_MODULE_ID = "reddit"


class RedditComingSoonModule(INetworkModule):
    id = STUB_MODULE_ID
    display_name = "Reddit Master"
    short_description = (
        "Работа с Reddit: аккаунты, сабреддиты, поиск аудитории, "
        "недавняя активность, очереди сообщений, AI-помощник и безопасный агент."
    )
    icon = "brand-reddit"

    def status(self) -> ModuleStatus:
        return ModuleStatus.COMING_SOON

    def navigation(self) -> NavigationManifest:
        # default_route используется только после "Открыть" — карточка
        # disabled, поэтому пустой sidebar пока допустим.
        return NavigationManifest(module_id=self.id, groups=(), default_route="/reddit")

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(module_id=self.id, capabilities=())


reddit_coming_soon_module = RedditComingSoonModule()

__all__ = ["RedditComingSoonModule", "STUB_MODULE_ID", "reddit_coming_soon_module"]
