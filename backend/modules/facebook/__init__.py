"""Facebook Master module: тонкая обёртка над существующими роутерами/сервисами.

Модуль НЕ переносит FB-код из `backend/routers/*` и `backend/services/*`.
Он только объявляет свой `INetworkModule` с навигацией (1:1 текущего
sidebar) и capabilities. Роутеры FB Master подключены в
`backend/app_factory.py` напрямую, как и раньше.
"""

from backend.modules.facebook.module import FacebookModule, facebook_module

__all__ = ["FacebookModule", "facebook_module"]
