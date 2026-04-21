"""Module registry + network-module interfaces для multi-workspace shell.

Публичная поверхность:

- `INetworkModule` — абстрактный контракт сетевого модуля.
- `ModuleRegistry` — singleton, в котором модули регистрируются при старте.
- `NavigationManifest`, `CapabilityManifest`, `ModuleStatus` — описания
  модуля для launcher и sidebar.
- Прикладные интерфейсы (IAccountConnector, IAudienceDiscoveryProvider, …)
  в `interfaces.py`.

Использование будет активировано в M2+ за feature-флагом
`FB_MASTER_MULTI_WORKSPACE_ENABLED`. M1 только создаёт контуры.
"""

from backend.core.modules.base import INetworkModule, ModuleStatus
from backend.core.modules.manifests import (
    CapabilityManifest,
    NavigationGroup,
    NavigationItem,
    NavigationManifest,
)
from backend.core.modules.registry import ModuleRegistry, module_registry

__all__ = [
    "CapabilityManifest",
    "INetworkModule",
    "ModuleRegistry",
    "ModuleStatus",
    "NavigationGroup",
    "NavigationItem",
    "NavigationManifest",
    "module_registry",
]
