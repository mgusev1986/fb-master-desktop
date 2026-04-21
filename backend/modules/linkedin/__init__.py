"""LinkedIn Master module (Beta).

В app_factory регистрируется одна из двух реализаций:
  • `linkedin_module` (полный BETA) — при FB_MASTER_LINKEDIN_MODULE_ENABLED=1;
  • `linkedin_coming_soon_module` (COMING_SOON stub) — иначе.

Модуль умышленно построен по образу Reddit Master: использует общие
контракты `INetworkModule`, `NavigationManifest`, `CapabilityManifest`
из `backend/core/modules/`. Никакой логики Facebook / Reddit не дублирует.
"""

from backend.modules.linkedin.module_stub import (
    LinkedInComingSoonModule,
    linkedin_coming_soon_module,
)

__all__ = ["LinkedInComingSoonModule", "linkedin_coming_soon_module"]
