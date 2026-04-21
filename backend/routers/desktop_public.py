"""Публичные эндпоинты для настольного приложения (без сессии и ключа доступа)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend import config as app_config
from backend.services.release_download import sign_url_dict

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/desktop-update-dev")
def desktop_update_manifest_developer():
    """
    Манифест обновления для тестовой сборки (канал разработчика).
    Файлы — в static/releases/dev/; прод-клиенты ходят на /api/public/desktop-update.
    """
    latest = app_config.desktop_dev_update_latest_version()
    if not latest:
        return JSONResponse(
            {
                "enabled": False,
                "channel": "developer",
                "message": "Канал разработчика не настроен (FB_DESKTOP_DEV_LATEST_VERSION пусто).",
            }
        )

    urls = sign_url_dict(
        app_config.desktop_dev_update_download_urls(),
        app_config.RELEASE_DOWNLOAD_TTL_MANIFEST,
    )
    return JSONResponse(
        {
            "enabled": True,
            "channel": "developer",
            "latest_version": latest,
            "min_version": app_config.desktop_dev_update_min_version() or None,
            "urls": urls,
            "release_notes": app_config.desktop_dev_update_release_notes() or None,
        }
    )


@router.get("/desktop-update")
def desktop_update_manifest():
    """
    Манифест обновления для Electron: клиент сравнивает package.json version с latest_version.
    Задаётся на прод-сервере через .env — один источник правды для всех пользователей.
    """
    latest = app_config.desktop_update_latest_version()
    if not latest:
        return JSONResponse(
            {
                "enabled": False,
                "message": "Обновления через сервер не настроены (FB_DESKTOP_LATEST_VERSION пусто).",
            }
        )

    urls = sign_url_dict(
        app_config.desktop_update_download_urls(),
        app_config.RELEASE_DOWNLOAD_TTL_MANIFEST,
    )
    return JSONResponse(
        {
            "enabled": True,
            "latest_version": latest,
            "min_version": app_config.desktop_update_min_version() or None,
            "urls": urls,
            "release_notes": app_config.desktop_update_release_notes() or None,
        }
    )
