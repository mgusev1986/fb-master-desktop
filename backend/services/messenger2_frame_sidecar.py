"""Автозапуск настольного приложения fb-master-desktop (Electron) при старте back-office."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from backend.config import APP_BASE_URL, AUTOSTART_MESSENGER2_FRAME, BASE_DIR, MESSENGER2_FRAME_HEALTH_PORT

logger = logging.getLogger(__name__)

_FRAME_DIR = BASE_DIR / "desktop" / "fb-master-desktop"


def try_autostart_messenger2_frame() -> None:
    if not AUTOSTART_MESSENGER2_FRAME:
        return
    main_js = _FRAME_DIR / "main.js"
    if not main_js.is_file():
        logger.warning("fb-master-desktop: пропуск — нет файла %s", main_js)
        return
    npm = shutil.which("npm")
    if not npm:
        logger.warning("fb-master-desktop: npm не найден в PATH — автозапуск пропущен")
        return
    if not (_FRAME_DIR / "node_modules").is_dir():
        logger.warning(
            "fb-master-desktop: нет node_modules — один раз выполните: cd %s && npm install",
            _FRAME_DIR,
        )
        return
    env = os.environ.copy()
    env["MESSENGER2_FRAME_HEALTH_PORT"] = str(MESSENGER2_FRAME_HEALTH_PORT)
    base = (APP_BASE_URL or "http://127.0.0.1:8000").rstrip("/") + "/"
    env.setdefault("FB_MASTER_APP_URL", base)
    cmd = [npm, "start"]
    try:
        kwargs: dict = {
            "cwd": str(_FRAME_DIR),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            cflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if cflags:
                kwargs["creationflags"] = cflags
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        logger.info(
            "fb-master-desktop: запущен npm start (health http://127.0.0.1:%s/health)",
            MESSENGER2_FRAME_HEALTH_PORT,
        )
    except Exception:
        logger.exception("fb-master-desktop: не удалось запустить npm start")
