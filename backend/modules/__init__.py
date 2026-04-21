"""Сетевые модули multi-workspace shell.

Каждый модуль — поддиректория (`facebook`, `reddit`, …) с публичным
entry-point'ом `module.py`, экспортирующим экземпляр `INetworkModule`.

Регистрация модулей выполняется в `backend.app_factory.create_app()`
после подключения всех существующих роутеров.
"""
