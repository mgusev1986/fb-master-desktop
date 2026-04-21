"""FacebookModule — адаптер над существующим FB Master."""

from __future__ import annotations

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import CapabilityManifest, NavigationManifest
from backend.modules.facebook.manifest import CAPABILITIES, MODULE_ID, NAVIGATION


class FacebookModule(INetworkModule):
    id = MODULE_ID
    display_name = "Facebook Master"
    short_description = (
        "Аккаунты, парсер, рассылка, сценарии, Messenger и CRM для работы с Facebook."
    )
    icon = "brand-facebook"

    def status(self) -> ModuleStatus:
        # FB Master — продуктовый модуль, всегда READY. Выключается только при
        # отсутствии лицензии на стороне платформы; это обрабатывается auth-слоем.
        return ModuleStatus.READY

    def navigation(self) -> NavigationManifest:
        return NAVIGATION

    def capabilities(self) -> CapabilityManifest:
        return CAPABILITIES

    # register_routers / on_startup — namespace FB уже подключен в
    # app_factory.create_app(), поэтому здесь no-op.


facebook_module = FacebookModule()

__all__ = ["FacebookModule", "facebook_module"]
