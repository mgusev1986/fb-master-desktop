"""Реестр сетевых модулей.

Singleton `module_registry`, в который модули регистрируются при старте
процесса. Используется launcher'ом (список карточек workspaces),
workspace switcher'ом (dropdown в header), sidebar'ом (навигация),
app_factory (подключение роутеров).

В M1 реестр **пустой** и нигде не используется. В M3 FB-модуль
зарегистрируется как тонкая обёртка, в M4 — Reddit.
"""

from __future__ import annotations

from collections.abc import Iterable

from backend.core.modules.base import INetworkModule, ModuleStatus


class ModuleRegistry:
    """Простой in-memory реестр сетевых модулей.

    Потокобезопасен для read-only after-boot сценариев: регистрация
    происходит в lifespan/create_app, дальше только чтение.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, INetworkModule] = {}
        self._order: list[str] = []
        self._sealed: bool = False

    # ── регистрация ──────────────────────────────────────

    def register(self, module: INetworkModule) -> None:
        """Добавить модуль в реестр. Повторная регистрация id запрещена."""
        if self._sealed:
            raise RuntimeError("ModuleRegistry sealed; register before create_app() returns")
        mid = module.id
        if not mid or not isinstance(mid, str):
            raise ValueError(f"module.id must be non-empty str, got {mid!r}")
        if mid in self._by_id:
            raise ValueError(f"module '{mid}' already registered")
        self._by_id[mid] = module
        self._order.append(mid)

    def seal(self) -> None:
        """Запретить дальнейшую регистрацию (вызывается после create_app)."""
        self._sealed = True

    # ── чтение ───────────────────────────────────────────

    def get(self, module_id: str) -> INetworkModule | None:
        return self._by_id.get(module_id)

    def require(self, module_id: str) -> INetworkModule:
        mod = self._by_id.get(module_id)
        if mod is None:
            raise KeyError(f"module '{module_id}' is not registered")
        return mod

    def all(self) -> Iterable[INetworkModule]:
        """Модули в порядке регистрации."""
        return tuple(self._by_id[mid] for mid in self._order)

    def visible(self) -> Iterable[INetworkModule]:
        """Модули, которые должны показываться в launcher (любые, кроме maintenance).

        Карточки с `COMING_SOON` и `DISABLED_FLAG` показываются как disabled,
        но видимы — чтобы пользователь знал о грядущих модулях.
        """
        visible_statuses = {
            ModuleStatus.READY,
            ModuleStatus.BETA,
            ModuleStatus.COMING_SOON,
            ModuleStatus.DISABLED_FLAG,
        }
        return tuple(m for m in self.all() if m.status() in visible_statuses)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    def __contains__(self, module_id: str) -> bool:
        return module_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    # ── сервис ───────────────────────────────────────────

    def reset_for_tests(self) -> None:
        """Только для тестов — полный сброс состояния."""
        self._by_id.clear()
        self._order.clear()
        self._sealed = False


# singleton на процесс
module_registry = ModuleRegistry()


__all__ = ["ModuleRegistry", "module_registry"]
